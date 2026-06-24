"""
Planset PDF Extractor
Uses Claude Vision (claude-sonnet-4-20250514) to extract interconnection fields
from Tron Solar planset PDFs.

Part of the planset-extractor skill.
"""

import os
import re
import ssl
import json
import base64
import asyncio
import logging
import httpx
from dataclasses import dataclass, field
from typing import Optional
import fitz  # PyMuPDF

log = logging.getLogger("extractor")


CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
# Current Sonnet alias (no date suffix). The old dated snapshot claude-sonnet-4-20250514 retired
# 2026-06-15 and now 404s. Override per-env without a code change via CLAUDE_MODEL.
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")


@dataclass
class ArrayInfo:
    tilt: float
    azimuth: float
    module_count: int
    dc_size_kw: float   # calculated: module_count * module_wattage / 1000
    roof_plane: Optional[int] = None   # which physical roof plane (1,2,...) this array sits on
    strings_on_plane: Optional[int] = None  # distinct string colors confined to THIS plane (PV-3.1)


@dataclass
class PlansetData:
    """All data extracted from the planset PDF."""

    # --- Cover Sheet (PV-1) ---
    customer_name: str
    customer_address: str
    system_size_dc_kw: float
    system_size_ac_kw: float
    module_manufacturer: str
    module_model: str
    module_wattage: float
    module_quantity: int
    inverter_manufacturer: str
    inverter_model: str
    inverter_quantity: int
    battery_manufacturer: Optional[str]
    battery_model: Optional[str]
    battery_quantity: Optional[int]
    battery_kwh: Optional[float]
    has_expansion_unit: bool
    expansion_model: Optional[str]
    expansion_quantity: Optional[int]
    utility_company: str
    ahj: str
    design_date: str

    # --- Three-Line Diagram (PV-5) ---
    meter_number: Optional[str] = None
    service_type: Optional[str] = None      # "underground" or "overhead"
    nominal_voltage: Optional[float] = None
    main_panel_amperage: Optional[int] = None
    interconnection_method: Optional[str] = None  # "load side" or "line side"

    # --- Roof Plan (PV-3) ---
    arrays: list = field(default_factory=list)  # list[ArrayInfo]

    # --- String map (PV-3.1) ---
    # strings never cross roof planes; one entry per physical plane: {plane: string_count}
    strings_per_plane: dict = field(default_factory=dict)

    # --- Expansion mount kit (resolved: plans -> master note -> default wall) ---
    expansion_mount_kit: Optional[str] = None   # "stack" or "wall"

    # --- Confidence & Warnings ---
    confidence_scores: dict = field(default_factory=dict)
    extraction_warnings: list = field(default_factory=list)

    # --- Structured PV-5 electrical reads (consumed by the engine blocks in orchestrator) ---
    # ac_disconnects/dc_disconnects/buskit_breakers/csr_breakers/pw3_skus/one_line_text + meter/MSP
    # new-flag and equipment SKU + gateway_count/backup_switch/inverter_sku/remote_meter_count.
    electrical: dict = field(default_factory=dict)

    # Structured HARD/SOFT flags raised during extraction (e.g. equipment_count_mismatch). The
    # orchestrator merges these straight into FLAGS_FOR_HUMAN_REVIEW.
    extraction_flags: list = field(default_factory=list)

    # --- Racking BOM table (PV-3 / PV-3.x) — read from the text layer where present, else Vision ---
    # {format, system_type, attachment_type, module_count, roof:{...}, ground_bom_table:{pn:qty},
    #  has_enphase, source, unresolved:[...]}. Drives the orchestrator's racking block (Solar 33-90).
    racking: dict = field(default_factory=dict)


class PlansetExtractor:
    """
    Extracts interconnection data from Tron Solar planset PDFs
    using Claude Vision.
    """

    def __init__(self, api_key: Optional[str] = None, debug_pages_dir: Optional[str] = None):
        self.api_key = api_key or os.environ["ANTHROPIC_API_KEY"]
        self.headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        # DIAGNOSTIC (opt-in): when set via arg or EXTRACTOR_DEBUG_PAGES_DIR, every page rendered for
        # Vision is saved here at the SAME 2x render the model sees, plus a _manifest.txt recording
        # which page index each label resolved to. Unset in production -> nothing is written.
        self.debug_pages_dir = debug_pages_dir or os.environ.get("EXTRACTOR_DEBUG_PAGES_DIR") or None
        self._manifest = None
        if self.debug_pages_dir:
            os.makedirs(self.debug_pages_dir, exist_ok=True)
            self._manifest = os.path.join(self.debug_pages_dir, "_manifest.txt")
            with open(self._manifest, "w", encoding="utf-8") as fh:
                fh.write("Page label resolution + saved images (2x render — exactly what Vision saw)\n\n")

    def _dbg(self, line: str) -> None:
        if self._manifest:
            with open(self._manifest, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def _pdf_page_to_base64(self, pdf_path: str, page_number: int, label: Optional[str] = None) -> str:
        """Render a single PDF page to a base64 PNG. If debug dumping is on, also save the EXACT PNG
        bytes (and a manifest line) so we can eyeball what the model actually saw."""
        doc = fitz.open(pdf_path)
        page = doc[page_number]
        # Render at 3x for legibility of small breaker/disconnect labels, but cap the long edge at
        # 2576px (Sonnet 4.6's high-res ceiling) — scale to fit rather than overshoot. On an ANSI B
        # (11x17) sheet the long edge is ~1224pt, so the cap binds and the effective zoom is ~2.1x.
        MAX_LONG_EDGE_PX = 2576
        long_edge_pts = max(page.rect.width, page.rect.height) or 1.0
        zoom = min(3.0, MAX_LONG_EDGE_PX / long_edge_pts)
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        doc.close()
        if self.debug_pages_dir and label:
            safe = "".join(c if (c.isalnum() or c in "-._") else "_" for c in label)
            fn = os.path.join(self.debug_pages_dir, f"selected_{safe}.png")
            with open(fn, "wb") as fh:
                fh.write(img_bytes)
            self._dbg(f"[{label}] SAVED page index {page_number} -> {os.path.basename(fn)} "
                      f"({pix.width}x{pix.height}px @{zoom:.2f}x)")
        return base64.standard_b64encode(img_bytes).decode("utf-8")

    def extract_page_pdf(self, pdf_path: str, label: str, output_path: str) -> bool:
        """
        Extract a single labeled page (e.g. 'PV-5') from the planset and save
        as a new single-page PDF. Returns True if found, False if not found
        (falls back to page 0 in that case).
        """
        page_num = self._find_page_by_label(pdf_path, label)
        found = page_num is not None
        if page_num is None:
            page_num = 0
        doc = fitz.open(pdf_path)
        new_doc = fitz.open()
        new_doc.insert_pdf(doc, from_page=page_num, to_page=page_num)
        new_doc.save(output_path)
        doc.close()
        new_doc.close()
        return found

    def _label_in_title_block(self, page, label: str) -> bool:
        """True if `label` is the page's OWN sheet number — it appears as a standalone word in the
        bottom-right title-block region. This is what distinguishes the real sheet from the cover's
        table-of-contents (mid-left) and from in-note cross-references ('see PV-5')."""
        rect = page.rect
        w, h = rect.width, rect.height
        if not w or not h:
            return False
        target = label.replace(" ", "").upper()
        try:
            words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
        except Exception:  # noqa: BLE001
            return False
        for x0, y0, x1, y1, word, *_ in words:
            if x0 >= 0.60 * w and y0 >= 0.78 * h:           # bottom-right region
                if word.replace(" ", "").upper() == target:  # EXACT word so 'PV-3' != 'PV-3.1'
                    return True
        return False

    @staticmethod
    def _label_present(text: str, label: str) -> bool:
        """True if `label` appears as an EXACT sheet number in text — not merely as a prefix of a
        longer one. 'PV-5' matches 'PV-5' / 'PV-5 ELECTRICAL...' / 'PV-5.' but NOT 'PV-5.1' / 'PV-50',
        and 'PV-3' does not match 'PV-3.1'. (Lookahead: not followed by an optional dot then a digit —
        a sub-sheet '.1' or a trailing digit blocks it; a plain sentence period does not.)"""
        return re.search(re.escape(label) + r"(?!\.?\d)", text or "") is not None

    def _find_page_by_label(self, pdf_path: str, label: str) -> Optional[int]:
        """Resolve a sheet label (e.g. 'PV-5') to its page index.

        Anchors on the page's OWN title-block sheet number (bottom-right) so the cover's
        table-of-contents and in-note cross-references don't win. Falls back to 'fewest distinct
        PV-N labels, tie toward the later page' only when no title-block match exists. Returns the
        page index, or None if it CANNOT confidently resolve to exactly one page — the caller must
        HARD-flag and skip, never silently use page 0.
        """
        doc = fitz.open(pdf_path)
        title_hits: list[int] = []
        text_hits: list[tuple[int, int]] = []   # (distinct PV-N count on the page, page index)
        for i, page in enumerate(doc):
            text = page.get_text()
            if not self._label_present(text, label):   # exact sheet number: 'PV-5' != 'PV-5.1'
                continue
            text_hits.append((len(set(re.findall(r"PV-\d+(?:\.\d+)?", text))), i))
            if self._label_in_title_block(page, label):
                title_hits.append(i)
        doc.close()

        # 1) Title-block anchor — the reliable discriminator.
        if len(title_hits) == 1:
            i = title_hits[0]
            log.info("find_page_by_label(%r) -> page %d via title-block sheet number", label, i)
            self._dbg(f"[{label}] resolved to PAGE INDEX {i} via TITLE-BLOCK sheet number")
            return i
        if len(title_hits) > 1:
            log.warning("find_page_by_label(%r) -> title-block on multiple pages %s; AMBIGUOUS",
                        label, title_hits)
            self._dbg(f"[{label}] UNRESOLVED: title-block sheet number found on multiple pages {title_hits}")
            return None

        # 2) Fallback: the cover/TOC lists many PV-N labels; a real sheet references few. Pick the
        #    page with the fewest, tie toward the later page; accept only if it's clearly non-TOC.
        if text_hits:
            text_hits.sort(key=lambda t: (t[0], -t[1]))
            distinct, i = text_hits[0]
            if distinct <= 3:
                log.info("find_page_by_label(%r) -> page %d via fallback (distinct PV-N=%d)", label, i, distinct)
                self._dbg(f"[{label}] resolved to PAGE INDEX {i} via FALLBACK (fewest PV-N labels={distinct})")
                return i
            log.warning("find_page_by_label(%r) -> only TOC-like pages (min distinct PV-N=%d); UNRESOLVED",
                        label, distinct)
            self._dbg(f"[{label}] UNRESOLVED: only table-of-contents-like pages contain it (min PV-N={distinct})")
            return None

        log.warning("find_page_by_label(%r) -> NOT FOUND in any page text", label)
        self._dbg(f"[{label}] NOT FOUND in any page text")
        return None

    async def _call_claude(self, images_b64: list, prompt: str) -> str:
        """Call Claude Vision with one or more page images."""
        content = []
        for img_b64 in images_b64:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": img_b64,
                },
            })
        content.append({"type": "text", "text": prompt})

        payload = {
            "model": CLAUDE_MODEL,
            # 4000 (was 2000): the roof-plan JSON can list many arrays; 2000 risked truncating it
            # into invalid JSON. Output tokens are billed only as used, so the headroom is ~free.
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": content}],
        }

        # Retry transient connection corruption (e.g. SSLV3_ALERT_BAD_RECORD_MAC). Extraction makes
        # ~4 sequential Vision calls; one flaky connection should not fail the whole run. 4 attempts,
        # exponential backoff 1s/2s/4s, re-raise only on the final attempt.
        retryable = (ssl.SSLError, httpx.TransportError, httpx.ConnectError, httpx.ReadError)
        for attempt in range(4):
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        CLAUDE_API_URL, headers=self.headers, json=payload, timeout=60,
                    )
                    response.raise_for_status()
                    return response.json()["content"][0]["text"]
            except retryable as e:
                if attempt == 3:
                    raise
                delay = 2 ** attempt  # 1s, 2s, 4s
                log.warning("Vision call failed (%s: %s); retry %d/3 in %ds",
                            type(e).__name__, e, attempt + 1, delay)
                await asyncio.sleep(delay)

    @staticmethod
    def _balance_brackets(s: str) -> str:
        """Close any unclosed string / array / object in `s` (handles a response truncated by
        max_tokens). String-aware: respects escapes so a brace inside a quoted value isn't counted."""
        stack, in_str, esc = [], False, False
        for ch in s:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(ch)
            elif ch == "}" and stack and stack[-1] == "{":
                stack.pop()
            elif ch == "]" and stack and stack[-1] == "[":
                stack.pop()
        out = s + ('"' if in_str else "")
        for ch in reversed(stack):
            out += "}" if ch == "{" else "]"
        return out

    @staticmethod
    def _cut_to_last_complete(head: str) -> str:
        """Drop a dangling, incomplete top-level field: cut at the last comma seen at object depth 1
        (string-aware). Lets us salvage every field parsed BEFORE a mid-body delimiter error."""
        depth, in_str, esc, last_comma = 0, False, False, -1
        for i, ch in enumerate(head):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth -= 1
            elif ch == "," and depth == 1:
                last_comma = i
        return head[:last_comma] if last_comma != -1 else head

    def _parse_json(self, raw: str) -> dict:
        """Parse JSON from a Claude response, with tolerant recovery.

        Strips markdown code fences, then extracts the object from the first '{' to the last '}'
        (drops any prose the model wrapped around the JSON). If strict parsing fails it attempts, in
        order: (1) remove trailing commas; (2) close unbalanced brackets (truncation by max_tokens);
        (3) truncate at the parse-error position, drop the partial trailing field, and close — which
        salvages every field that parsed cleanly before a mid-body delimiter error. Each successful
        repair is logged. Raises ValueError (with the raw text) only if nothing recovers it."""
        clean = (raw or "").strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1]          # drop the opening ``` / ```json line
            if clean.rstrip().endswith("```"):
                clean = clean.rstrip()[:-3]           # drop the closing fence
            clean = clean.strip()
        start, end = clean.find("{"), clean.rfind("}")
        if start != -1 and end > start:
            clean = clean[start:end + 1]
        elif start != -1:                              # opening brace but no close -> truncated
            clean = clean[start:]

        first_err = None
        try:
            return json.loads(clean)
        except json.JSONDecodeError as e:
            first_err = e

        for label, candidate in (
            ("removed trailing comma(s)", re.sub(r",(\s*[}\]])", r"\1", clean)),
            ("closed unbalanced brackets (truncation)", self._balance_brackets(clean)),
            ("truncated at parse-error and closed brackets",
             self._balance_brackets(self._cut_to_last_complete(clean[:first_err.pos]))),
        ):
            try:
                result = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            log.warning("PV-5/JSON recovered via repair: %s (orig error: %s)", label, first_err)
            return result

        raise ValueError(
            f"Claude response was not valid JSON and could not be repaired ({first_err}). "
            f"Raw response (first 1000 chars): {(raw or '')[:1000]!r}"
        ) from first_err

    def _page_text(self, pdf_path: str, page_index: Optional[int]) -> str:
        """Raw text layer of one page (for the deterministic equipment-block parse). '' if unavailable."""
        if page_index is None:
            return ""
        doc = fitz.open(pdf_path)
        try:
            return doc[page_index].get_text() or ""
        except Exception:  # noqa: BLE001
            return ""
        finally:
            doc.close()

    @staticmethod
    def _parse_equipment_text(text: str) -> dict:
        """Parse the PROJECT-DESCRIPTION / equipment text block (PV-5 and PV-3 print it as plain text):
            61 SIRIUS ELNSM54M-HC-N 450W MONO MODULES
            21 TESLA: RSD MCI-2
            02 TESLA POWERWALL 3 (1707000-XX-Y)
            01 TESLA POWERWALL 3 EXPANSION UNIT (1807000-xx-y)
        Returns {'pw3','expansion','mci2','modules'} as ints (None if a line isn't present). The
        leading integer on each line is the authoritative count — it's typed text, not a schematic."""
        out = {"pw3": None, "expansion": None, "mci2": None, "modules": None}
        for line in (text or "").splitlines():
            m = re.match(r"\s*0*(\d+)\s+(.+)", line)
            if not m:
                continue
            n, desc = int(m.group(1)), m.group(2).upper()
            if "EXPANSION" in desc or "1807000" in desc:
                out["expansion"] = n
            elif "POWERWALL 3" in desc or "1707000" in desc:
                out["pw3"] = n
            elif "MCI-2" in desc or "MCI" in desc:
                out["mci2"] = n
            elif "MODULE" in desc:
                out["modules"] = n
        return out

    @staticmethod
    def _extract_harness_code(text: str) -> Optional[str]:
        """Find the Tesla expansion harness length code in free text. Matches '1875157-05-X',
        '1875157 05', etc. (optional spaces/hyphens, case-insensitive). Returns '05'|'20'|'40' or None."""
        if not text:
            return None
        m = re.search(r"1875157[\s\-]{0,3}(05|20|40)", str(text).upper())
        return m.group(1) if m else None

    @staticmethod
    def _parse_note_csr(text: str):
        """Parse a Master Note for an explicit CSR statement. Returns (kind, amps):
          ('none', [])   -> note explicitly says NO CSR breaker
          ('amps', [N..]) -> note states one or more CSR amperages
          (None, [])     -> note is silent on CSR (no reconciliation)."""
        t = (text or "").upper()
        if "CSR" not in t:
            return None, []
        if re.search(r"\bNO\s+CSR\b", t):
            return "none", []
        amps = []
        for m in re.finditer(r"(\d{2,3})\s*A?\s*CSR|CSR\s*(?:BREAKER\s*)?(\d{2,3})\s*A?", t):
            a = m.group(1) or m.group(2)
            if a:
                amps.append(int(a))
        return ("amps", sorted(set(amps))) if amps else (None, [])

    @staticmethod
    def _parse_fuse_amps(text: str):
        """Find FUSE amperages drawn on the one-line: '60A FUSES', '(2) 40A FUSES', '40 A FUSE'.
        Returns distinct amps in order of appearance. The leading '(N)' is a fuse COUNT, not the
        rating, so it's ignored. 'FUSED' (the disconnect adjective, e.g. '60A FUSED AC DISCONNECT')
        is deliberately NOT matched — only the noun FUSE/FUSES — so the disconnect rating is never
        mistaken for the fuse rating."""
        out = []
        for m in re.finditer(r"(?:\(\s*\d+\s*\)\s*)?(\d{1,3})\s*A\s*FUSES?\b", (text or "").upper()):
            a = int(m.group(1))
            if a not in out:
                out.append(a)
        return out

    # ---------- Racking BOM table (PV-3 / PV-3.x) ----------
    @staticmethod
    def _classify_attachment(blob: str):
        """(system_type, attachment_type) from racking-table text/desc. system_type roof|ground;
        attachment_type K2_GROUND | K2_SHINGLE | S5_PROTEABRACKET | S5_SOLARFOOT | None."""
        U = (blob or "").upper()
        if (any(t in U for t in ("CROSSRAIL 80", "CROSSRAIL80", "CR80", "GROUND MOUNT", "TOP CAP",
                                  "GROUND SCREW", "PIPE BRACKET", "PIPE - BRACKET", "N/S PIPE"))
                or any(pn in U for pn in ("4001370", "4000198", "4000175", "4001221", "4000708"))):
            return "ground", "K2_GROUND"
        if "PROTEABRACKET" in U or "PROTEA" in U or "S-5" in U or "STANDING SEAM" in U:
            return "roof", "S5_PROTEABRACKET"
        if "SOLARFOOT" in U or "SOLAR FOOT" in U:
            return "roof", "S5_SOLARFOOT"
        if any(t in U for t in ("MULTIMOUNT", "MULTI MOUNT", "MULTI-MOUNT", "SHINGLE", "4000282", "4000819")):
            return "roof", "K2_SHINGLE"
        return "roof", None

    @staticmethod
    def _parse_roof_racking_text(full_text: str):
        """Parse the inline PV-3 BILL OF MATERIALS table (EQUIPMENT | QTY | DESCRIPTION triples, generic
        labels). Returns ({attachments,rails,splice,mid_clamps,end_clamps,modules}, triples). The block
        is scoped from the BILL OF MATERIALS header so the roof-config table below it isn't read."""
        lines = [l.strip() for l in (full_text or "").splitlines()]
        try:
            start = next(i for i, l in enumerate(lines) if "BILL OF MATERIAL" in l.upper())
        except StopIteration:
            start = 0
        block = lines[start:]
        triples = []
        for j in range(1, len(block) - 1):
            if re.fullmatch(r"0*\d{1,4}", block[j]):
                triples.append((block[j - 1].upper(), int(block[j]), block[j + 1].upper()))
        out = {"attachments": None, "rails": None, "splice": None,
               "mid_clamps": None, "end_clamps": None, "modules": None}
        for label, qty, desc in triples:
            L = label + " " + desc
            if "END CLAMP" in L:
                out["end_clamps"] = qty
            elif "MODULE CLAMP" in label or ("MID" in L and "CLAMP" in L):
                out["mid_clamps"] = qty
            elif "ATTACHMENT" in label or "MULTIMOUNT" in L:
                out["attachments"] = qty
            elif label.startswith("RAIL") or "CROSSRAIL" in L:
                out["rails"] = qty
            elif "SPLICE" in L:
                out["splice"] = qty
            elif "SOLAR PV MODULE" in label or label == "MODULES":
                out["modules"] = qty
        return out, triples

    @staticmethod
    def _normalize_gm_key(part_number, description):
        """Map a K2 ground-mount BOM row to a key k2_ground_mount() recognizes — by DESCRIPTION first
        (robust to OCR'd / variant part numbers), part number as backup. Returns None if unrecognized
        (caller flags it; never guessed)."""
        U = (description or "").upper()
        P = (part_number or "").upper().strip()
        if "CROSSRAIL 80" in U or "CROSSRAIL80" in U or "CR80" in U or P in ("4001370", "4000708"):
            if "172" in U or P == "4000708":
                return "rail_172"
            return "rail_216"            # 216" default (incl. P==4001370)
        if "HEYCLIP" in U or "SUNRUNNER" in U or P == "4000382":
            return "4000382"             # rule 1: NEVER on a GM BOM
        if "CROSS CAP" in U or P == "4000372":
            return "k2_cross_cap"        # rule 3: NEVER on a GM BOM
        if "MICRO" in U or "MLPE" in U or P in ("4000629-H", "4000629"):
            return "4000629-H"           # rule 2: only with Enphase
        if "END CAP" in U or P == "4001221":
            return "end_cap"             # rule 5: COMPUTED, table value ignored
        if "WIRE" in U or ("CLIP" in U and "CROSS" not in U) or P == "4000069":
            return "wire_clip"           # rule 6: COMPUTED, table value ignored
        if "PIPE COUPLING" in U:
            return "pipe_coupling_3"     # rule 4: new SKU row 84
        if "SPLICE" in U or "CONNECTOR" in U or P == "4001196":
            return "splice"
        if "TOP CAP" in U or P == "4000198":
            return "top_cap"
        if "BRACKET" in U or P in ("4000175", "4000075"):
            return "pipe_bracket"
        if "CROSS CLAMP" in U or "MID CLAMP" in U or "END CLAMP" in U or "COMBO" in U or P in ("4000145", "4000035"):
            return "combo_clamp"
        if "GROUND LUG" in U or P == "4000006-H":
            return "ground_lug"
        if "LENGTH 10" in U or "10 FT" in U or "10FT" in U:
            return "pipe_10ft"
        if "GROUND SCREW" in U:
            return "ground_screw"
        if "N/S" in U and ("REAR" in U or "120" in U):
            return "ns_pipe_rear_120"
        if "N/S" in U and ("FRONT" in U or "60" in U):
            return "ns_pipe_front_60"
        if "DIAGONAL" in U or "BRACE" in U:
            return "diag_brace"
        return None

    def _parse_ground_racking_text(self, full_text: str):
        """Parse an inline (text-layer) K2 GM BOM table into {gm_key: qty}. Untested vs a text GM
        planset (our GM samples are images) — present so a future text GM resolves without Vision."""
        lines = [l.strip() for l in (full_text or "").splitlines()]
        table, unresolved = {}, []
        for j in range(1, len(lines) - 1):
            if re.fullmatch(r"0*\d{1,4}", lines[j]):
                pn, desc = lines[j - 1], lines[j + 1]
                key = self._normalize_gm_key(pn, desc)
                if key:
                    table[key] = table.get(key, 0) + int(lines[j])
        return table

    async def _vision_read_bom_table(self, pdf_path: str, page_indices: list):
        """Vision-read the 'Bill of materials' table from FLATTENED-IMAGE PV-3.x sheets (no text layer).
        Returns a merged list of {part_number, description, quantity} across all given pages — a two-
        array GM whose BOM spans PV-3.1 + PV-3.2 is summed downstream by key."""
        prompt = (
            "This Tron Solar racking sheet has a 'Bill of materials' / equipment table. Return ONLY "
            'JSON: {"rows":[{"part_number":"string or null","description":"string","quantity":integer}]}'
            "\nRules: one object per table row. part_number = the leftmost code (e.g. 4001370, "
            "4000006-H) or null if that cell is blank. quantity = the integer in the Quantity column "
            "(ignore List price / Total columns). Transcribe description verbatim. If there is no such "
            'table on the page, return {"rows":[]}.')
        rows = []
        for pi in page_indices:
            b64 = self._pdf_page_to_base64(pdf_path, pi, label=f"racking_p{pi}")
            raw = await self._call_claude([b64], prompt)
            try:
                data = self._parse_json(raw)
            except (ValueError, json.JSONDecodeError):
                log.error("racking BOM Vision parse failed on page %s", pi)
                continue
            for r in (data.get("rows") or []):
                rows.append(r)
        return rows

    async def _extract_racking(self, pdf_path: str, module_count, has_enphase: bool) -> dict:
        """Read the planset racking BOM table (Solar rows 33-90 source). Text layer where present
        (roof inline table), Vision fallback for rasterized PV-3.x BOM sheets. Returns the racking
        dict consumed by the orchestrator's racking block; format='absent' when nothing is found."""
        out = {"format": "absent", "system_type": None, "attachment_type": None,
               "module_count": module_count, "has_enphase": bool(has_enphase),
               "roof": None, "ground_bom_table": None, "source": None,
               "raw_rows": None, "unresolved": []}
        doc = fitz.open(pdf_path)
        try:
            pages = []
            for i in range(doc.page_count):
                full = doc[i].get_text() or ""
                wc = len(doc[i].get_text("words"))
                big = sum(1 for im in doc[i].get_images(full=True) if im[2] > 400 and im[3] > 400)
                pages.append((i, full, full.upper(), wc, big))
        finally:
            doc.close()

        # 1) INLINE TEXT table (roof or ground): only accept a page that actually PARSES into a
        # substantive table. (A sheet-INDEX "BILL OF MATERIAL" reference on the cover, or stray
        # racking words in the general notes, must NOT be mistaken for the table itself.)
        for i, full, U, wc, big in pages:
            if "BILL OF MATERIAL" not in U or wc <= 150:
                continue
            system, at = self._classify_attachment(full)
            if system == "ground":
                table = self._parse_ground_racking_text(full)
                if len(table) >= 3:
                    out.update(format="ground_text", system_type="ground", attachment_type=at,
                               source=f"page {i} (text layer)", ground_bom_table=table)
                    return out
            else:
                roof, triples = self._parse_roof_racking_text(full)
                if sum(1 for v in roof.values() if v is not None) >= 3:
                    out.update(format="roof_text", system_type=system, attachment_type=at,
                               source=f"page {i} (text layer)", roof=roof, raw_rows=triples)
                    return out

        # 2) FLATTENED-IMAGE BOM sheets ('BILL OF MATERIAL' sheet, title-block-only text + big images).
        img_pages = [i for i, full, U, wc, big in pages
                     if "BILL OF MATERIAL" in U and wc < 140 and big >= 1]
        if img_pages:
            rows = await self._vision_read_bom_table(pdf_path, img_pages)
            blob = " ".join(f"{r.get('part_number') or ''} {r.get('description') or ''}" for r in rows)
            system, at = self._classify_attachment(blob)
            out["source"] = f"pages {img_pages} (flattened image — Vision)"
            out["system_type"], out["attachment_type"] = system, at
            out["raw_rows"] = rows
            if system == "ground":
                out["format"] = "ground_image_vision"
                table = {}
                for r in rows:
                    key = self._normalize_gm_key(r.get("part_number"), r.get("description"))
                    qty = r.get("quantity")
                    if key and isinstance(qty, int):
                        table[key] = table.get(key, 0) + qty
                    elif not key and qty:
                        out["unresolved"].append(f"{r.get('part_number') or ''} {r.get('description') or ''} (qty {qty})")
                out["ground_bom_table"] = table
            else:
                out["format"] = "roof_image_vision"
                out["unresolved"].append("roof racking BOM is a flattened image; roof image parsing not "
                                         "yet implemented — populate roof racking manually.")
            return out

        return out

    def _extract_electrical_from_text(self, pdf_path: str, page_index: Optional[int]) -> dict:
        """Read bus-kit breakers, CSR, and the expansion harness P/N from the PV-5 TEXT LAYER (exact
        vector text + coordinates) — deterministic, no Vision resolution limit. Returns
        {buskit_breakers, csr_breakers, harness_pn}; a value is None when the text layer can't supply
        it (caller falls back to the Vision read).

        Method: find the 'BUS-KIT' label's position; breaker tokens ('NNA/NP') clustered near it are
        the bus-kit breakers; breaker tokens near the GATEWAY label but OUTSIDE that cluster are CSRs.
        The gateway ENCLOSURE rating ('200A,') is not an 'NNA/NP' token, so it is never miscounted.
        """
        out = {"buskit_breakers": None, "csr_breakers": None, "harness_pn": None}
        if page_index is None:
            return out
        try:
            doc = fitz.open(pdf_path)
            page = doc[page_index]
            words = page.get_text("words")     # (x0, y0, x1, y1, word, ...)
            full = page.get_text() or ""
            doc.close()
        except Exception:  # noqa: BLE001
            return out

        m = re.search(r"1875157[\s\-]{0,3}(05|20|40)", full.upper())
        if m:
            out["harness_pn"] = f"1875157-{m.group(1)}"

        def ctr(x0, y0, x1, y1):
            return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

        def dist(a, b):
            return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

        buskit_lbl = next((ctr(*w[:4]) for w in words
                           if "BUS-KIT" in w[4].upper() or "BUSKIT" in w[4].upper()), None)

        # EXISTING-breaker markers: an "(E)" token (e.g. "(E) 50A/2P" = the existing house main/PV
        # breaker) means the adjacent breaker is EXISTING — never a NEW bus-kit/CSR breaker. Exclude
        # those so a retained breaker isn't shipped as a CSR (the BOM lists NEW parts only). Breaker
        # labels are often rendered as VERTICAL text (tall, ~4pt-wide bbox), so the "(E)" marker can
        # sit either to the LEFT (horizontal text) or ABOVE (vertical text) of the breaker token.
        existing_marks = [(x0, y0, x1, y1) for x0, y0, x1, y1, w, *_ in words
                          if w.strip().upper().startswith("(E)") or w.strip().upper() == "E)"]

        def is_existing(bx0, by0, bx1, by1, w):
            if w.strip().upper().startswith("(E)"):
                return True
            bcx, bcy = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
            for ex0, ey0, ex1, ey1 in existing_marks:
                ecx, ecy = (ex0 + ex1) / 2.0, (ey0 + ey1) / 2.0
                same_row = abs(ecy - bcy) <= 6 and -5 <= bx0 - ex1 <= 45   # marker just LEFT
                same_col = abs(ecx - bcx) <= 6 and -5 <= by0 - ey1 <= 45   # marker just ABOVE
                if same_row or same_col:
                    return True
            return False

        breakers = []  # (amp, poles, center)
        for x0, y0, x1, y1, w, *_ in words:
            if is_existing(x0, y0, x1, y1, w):
                continue
            for mm in re.finditer(r"(\d{2,3})A/(\d)P", w.upper()):
                breakers.append((int(mm.group(1)), int(mm.group(2)), ctr(x0, y0, x1, y1)))

        if buskit_lbl is None:
            return out   # can't scope the bus-kit cluster from text -> let Vision handle the breakers

        # Anchor BOTH classifications on the BUS-KIT label (a single, reliable token). The GATEWAY
        # label is NOT a reliable anchor — "GATEWAY" also appears in far-away note references that
        # corrupt an averaged position. A CSR feeds the gateway right beside the bus-kit, so it sits
        # in a thin annulus just OUTSIDE the bus-kit cluster; the existing main and the enclosure
        # rating sit farther out (or are excluded as "(E)" / non-breaker tokens).
        R_BUSKIT, R_CSR = 90.0, 130.0   # proximity thresholds (pts), calibrated on PV-5 one-lines
        out["buskit_breakers"] = [{"amp": a, "poles": p}
                                  for a, p, c in breakers if dist(c, buskit_lbl) <= R_BUSKIT]
        out["csr_breakers"] = sorted({a for a, p, c in breakers
                                      if R_BUSKIT < dist(c, buskit_lbl) <= R_CSR})
        return out

    async def extract(self, pdf_path: str, coperniq_project: Optional[dict] = None,
                      master_note_form: Optional[dict] = None) -> PlansetData:
        """
        Main extraction entry point.
        Extracts data from PV-1, PV-3, PV-3.1 (string map), and PV-5.
        coperniq_project: the get_project() dict (fallback note source / other fields).
        master_note_form: the Coperniq get_form() dict for the project's "Master Note" form. The
        pipeline fetches it via list_project_forms(project_id) -> find name=="Master Note" ->
        get_form(form_id). This form (NOT project.custom) is where stack/wall wording actually lives.
        """
        warnings = []
        extraction_flags: list = []

        # --- Step 1: Extract cover sheet (PV-1, always page 0) ---
        cover_b64 = self._pdf_page_to_base64(pdf_path, 0, label="PV-1_cover")
        cover_data = await self._extract_cover_sheet(cover_b64)

        module_wattage = cover_data.get("module_wattage", 400)

        # --- Step 2: Extract three-line diagram (PV-5) — HARD-fail (no page-0 fallback) if unresolved ---
        pv5_page = self._find_page_by_label(pdf_path, "PV-5")
        if pv5_page is None:
            warnings.append("page selection failed: PV-5 — could not confidently identify the "
                            "electrical one-line sheet; PV-5 data NOT extracted (no fallback page).")
            electrical_data = {}
        else:
            pv5_b64 = self._pdf_page_to_base64(pdf_path, pv5_page, label="PV-5")
            electrical_data = await self._extract_three_line(pv5_b64)
            if electrical_data.pop("_pv5_parse_error", None):
                # The Vision structured read failed and could not be repaired. Make it LOUD — never
                # let it pass as if valid. The deterministic text-layer fields (Step 2.5) and the
                # PV-5/PV-3 equipment-text counts (Step 3.5) still fill in below; everything ELSE
                # (disconnects, gateway, meter/MSP, msp/meter flags) is missing and must be verified.
                extraction_flags.append({
                    "level": "HARD", "item": "pv5_electrical_unreliable",
                    "msg": "PV-5 Vision read could not be parsed/recovered — the structured electrical "
                           "fields (AC/DC disconnects, gateway, meter, MSP) are MISSING and unreliable. "
                           "Deterministic text-layer fields (bus-kit, CSR, harness, PW3/expansion "
                           "counts) still apply. Build the rest of the electrical BOM from PV-5 by hand."})

        # one_line_text now comes from the PV-5 TEXT LAYER (exact), not a Vision transcription — this
        # removed the ~3000-char field that was bloating/breaking the Vision JSON. It feeds the
        # supply-side-tap substring match (orchestrator rows 26-28) and the harness one_line source.
        electrical_data["one_line_text"] = (self._page_text(pdf_path, pv5_page)
                                            if pv5_page is not None else "")

        # --- Step 2.5: Read bus-kit breakers + CSR from the PV-5 TEXT LAYER (exact vector text +
        # coordinates) — deterministic, replaces the low-res Vision image read for these fields. The
        # whole-sheet Vision read is kept as a CROSS-CHECK (NOTE on disagreement). Harness P/N text-
        # layer read is folded into the multi-source resolution in Step 4.1 below. ---
        textlayer = self._extract_electrical_from_text(pdf_path, pv5_page) if pv5_page is not None else {}
        if textlayer.get("buskit_breakers") is not None:
            vis_bk = sorted((int(b["amp"]), int(b["poles"]))
                            for b in (electrical_data.get("buskit_breakers") or [])
                            if b.get("amp") and b.get("poles"))
            tl_bk = sorted((b["amp"], b["poles"]) for b in textlayer["buskit_breakers"])
            electrical_data["buskit_vision"] = electrical_data.get("buskit_breakers")
            electrical_data["buskit_breakers"] = textlayer["buskit_breakers"]
            electrical_data["buskit_source"] = "text_layer"
            if tl_bk != vis_bk:
                extraction_flags.append({
                    "level": "NOTE", "item": "buskit_text_vs_vision",
                    "msg": f"Bus-kit breakers: text-layer {tl_bk} vs Vision {vis_bk}; delivered the "
                           f"text-layer read (exact vector text)."})
        if textlayer.get("csr_breakers") is not None:
            vis_csr = sorted(int(a) for a in (electrical_data.get("csr_breakers") or []) if a)
            tl_csr = sorted(textlayer["csr_breakers"])
            electrical_data["csr_vision"] = electrical_data.get("csr_breakers")
            electrical_data["csr_breakers"] = textlayer["csr_breakers"]
            if tl_csr != vis_csr:
                extraction_flags.append({
                    "level": "NOTE", "item": "csr_text_vs_vision",
                    "msg": f"CSR: text-layer {tl_csr} vs Vision {vis_csr}; delivered the text-layer "
                           f"read (the gateway enclosure rating is not a breaker token)."})

        # --- Step 3: Extract roof plan (PV-3) — same HARD-fail policy ---
        pv3_page = self._find_page_by_label(pdf_path, "PV-3")
        if pv3_page is None:
            warnings.append("page selection failed: PV-3 — could not confidently identify the "
                            "roof-plan sheet; PV-3 array data NOT extracted (no fallback page).")
            array_data = {}
        else:
            pv3_b64 = self._pdf_page_to_base64(pdf_path, pv3_page, label="PV-3")
            array_data = await self._extract_roof_plan(pv3_b64, module_wattage)

        # --- Step 3.1: Extract string map (PV-3.1) — strings-per-plane by dashed-line color ---
        pv31_page = self._find_page_by_label(pdf_path, "PV-3.1")
        if pv31_page is None and pv3_page is not None:
            # DOCUMENTED fallback (NOT page 0): some plansets fold the string map into PV-3. Reuse the
            # already-resolved PV-3 page and NOTE it — visible, not silent.
            pv31_page = pv3_page
            warnings.append("PV-3.1 not a separate sheet; reading the string map from the resolved "
                            "PV-3 page (affects J-box counts; verify).")
        if pv31_page is None:
            warnings.append("page selection failed: PV-3.1 — string map sheet not identified and no "
                            "PV-3 page to fall back to; strings-per-plane NOT extracted.")
            string_data = {}
        else:
            pv31_b64 = self._pdf_page_to_base64(pdf_path, pv31_page, label="PV-3.1")
            string_data = await self._extract_string_map(pv31_b64)

        # --- Step 3.5: Reconcile the Powerwall-3 count across THREE sources (priority order) ---
        # 1 PRIMARY  : the PV-5 project-description / equipment TEXT block ("02 TESLA POWERWALL 3")
        # 2 SECONDARY: PW3 blocks drawn on the PV-5 one-line (Vision) — cross-check only
        # 3 TERTIARY : the PV-3 equipment/BOM table text
        # Deliver source 1; if the available sources disagree, HARD-flag (never silently pick one).
        pv5_eq = self._parse_equipment_text(self._page_text(pdf_path, pv5_page))   # PV-5 text block
        pv3_eq = self._parse_equipment_text(self._page_text(pdf_path, pv3_page))   # PV-3 table text
        src1 = pv5_eq.get("pw3")                       # PV-5 text
        src2 = electrical_data.get("pw3_drawn_count")  # drawn blocks
        src3 = pv3_eq.get("pw3")                       # PV-3 table
        sources = {"pv5_text": src1, "drawn_blocks": src2, "pv3_table": src3}
        present = [v for v in sources.values() if v is not None]
        # Deliver in priority order; fall back to the cover's battery_quantity if no text source.
        resolved_pw3 = next((v for v in (src1, src3, src2) if v is not None), None)
        if resolved_pw3 is None:
            resolved_pw3 = cover_data.get("battery_quantity")
        if present and len(set(present)) > 1:
            extraction_flags.append({
                "level": "HARD", "item": "equipment_count_mismatch",
                "msg": (f"PW3 count differs across sources {sources}; delivered {resolved_pw3} "
                        f"(PV-5 equipment text is authoritative). A disagreement means a block was "
                        f"misread — verify the Powerwall count on PV-1/PV-5/PV-3 before ordering.")})
        # Normalize the delivered count into pw3_skus (drives row 52 + per-PW3 bus-kit breakers) and
        # record the three source values for the confidence report.
        if isinstance(resolved_pw3, int) and resolved_pw3 >= 0:
            read = electrical_data.get("pw3_skus") or []
            sku = read[0] if read else "1707000"
            electrical_data["pw3_skus"] = [sku] * resolved_pw3
        electrical_data["pw3_count"] = resolved_pw3
        electrical_data["pw3_count_sources"] = sources

        # Expansion-unit count (PW3 Expansion, 1807000): PV-5 text -> PV-3 text -> drawn -> cover.
        # None means "not stated anywhere" (distinct from an explicit 0); the orchestrator flags the
        # None case for a battery system so the expansion block isn't silently omitted.
        electrical_data["expansion_count"] = next(
            (v for v in (pv5_eq.get("expansion"), pv3_eq.get("expansion"),
                         electrical_data.get("expansion_drawn_count"),
                         cover_data.get("expansion_quantity")) if v is not None), None)

        # --- Step 4: Resolve expansion mount kit (plans -> master note -> default wall) ---
        plan_mount = (string_data.get("plan_mount") or array_data.get("plan_mount")
                      or electrical_data.get("expansion_mount"))   # PV-5 Vision mount keyword
        master_notes = None
        if coperniq_project is not None or master_note_form is not None:
            try:
                from .electrical_engine import master_notes_from_coperniq
                master_notes = master_notes_from_coperniq(project=coperniq_project,
                                                          form=master_note_form)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"master-note parse failed: {e}")
        try:
            from .electrical_engine import resolve_expansion_mount
            mount_kit = resolve_expansion_mount(plan_mount=plan_mount, master_notes=master_notes)
        except Exception:
            mount_kit = "wall"
        # The RESOLVED mount (stack/wall) drives the orchestrator's tesla_expansion mount-kit row.
        electrical_data["expansion_mount"] = mount_kit
        if master_note_form is None and not plan_mount:
            warnings.append("expansion mount: no plan keyword and no Master Note form supplied; "
                            "defaulted to wall — fetch get_form(Master Note) and pass master_note_form")

        # Shared Master Note text (used by the harness + CSR + fuse cross-checks below).
        mn_text = ""
        if isinstance(master_notes, dict):
            mn_text = " ".join(str(master_notes.get(k, "") or "") for k in
                               ("design_notes", "additional_notes", "installation_notes",
                                "field_installation_notes"))

        # --- Step 4.05: FUSED-disconnect FUSE amperage — TEXT-LAYER authoritative. The fuse rating is
        # ALWAYS drawn on the one-line ("60A FUSES" / "(2) 40A FUSES"); a null fuse_amp from Vision is
        # a MISSED read, not an absent value (Vision under-reads this fine print). For EVERY fused
        # disconnect, resolve in order: (a) PRIMARY the PV-5 text layer (preferred OVER Vision);
        # (b) SECONDARY the Master Note; (c) TERMINAL leave null -> the engine raises fuse_amp_unmapped.
        # NEVER default the fuse to the disconnect rating — it must be read. ---
        discos = electrical_data.get("ac_disconnects") or []
        if any(d.get("fused") for d in discos):
            tl_fuses = self._parse_fuse_amps(electrical_data.get("one_line_text") or "")
            mn_fuses = self._parse_fuse_amps(mn_text)
            fused_i = 0
            for d in discos:
                if not d.get("fused"):
                    continue
                vision_fa = d.get("fuse_amp")
                resolved_fa, src = None, None
                if tl_fuses:                       # text layer wins (positional, else first)
                    resolved_fa = tl_fuses[fused_i] if fused_i < len(tl_fuses) else tl_fuses[0]
                    src = "text_layer"
                elif mn_fuses:
                    resolved_fa = mn_fuses[fused_i] if fused_i < len(mn_fuses) else mn_fuses[0]
                    src = "master_note"
                if src is not None:
                    d["fuse_amp"] = resolved_fa
                    d["fuse_amp_source"] = src
                    if vision_fa != resolved_fa:
                        extraction_flags.append({
                            "level": "NOTE", "item": "fuse_amp_source",
                            "msg": f"Fused {d.get('amp')}A disconnect: fuse amp resolved to "
                                   f"{resolved_fa}A from {src} (Vision read {vision_fa}). Fuse rating "
                                   f"is read off the plan, not defaulted to the disconnect size."})
                fused_i += 1

        # --- Step 4.1: Multi-source expansion HARNESS resolution (only when there is an expansion) ---
        # 1 direct Vision harness_pn field; 2 regex over the FULL one-line text; 3 Master Note text.
        # Deliver the first that yields a length code (05/20/40); conflict -> HARD; none -> leave it
        # null so tesla_expansion raises expansion_harness_pn_missing (never guess).
        if electrical_data.get("expansion_count"):
            by_source = {
                "text_layer": self._extract_harness_code(textlayer.get("harness_pn") or ""),
                "direct_field": self._extract_harness_code(electrical_data.get("harness_pn") or ""),
                "one_line_text": self._extract_harness_code(electrical_data.get("one_line_text") or ""),
                "master_note": self._extract_harness_code(mn_text),
            }
            resolved = (by_source["text_layer"] or by_source["direct_field"]
                        or by_source["one_line_text"] or by_source["master_note"])
            if resolved:
                src = next(k for k, v in by_source.items() if v == resolved)
                electrical_data["harness_pn"] = f"1875157-{resolved}"
                electrical_data["harness_source"] = src
                if len({v for v in by_source.values() if v}) > 1:
                    extraction_flags.append({
                        "level": "HARD", "item": "harness_pn_conflict",
                        "msg": f"Expansion harness P/N differs across sources {by_source}; delivered "
                               f"1875157-{resolved} ({src}). Verify the EXPANSION HARNESS callout on PV-5."})
                else:
                    extraction_flags.append({
                        "level": "NOTE", "item": "harness_pn_source",
                        "msg": f"Expansion harness resolved to 1875157-{resolved} from {src}."})
            else:
                electrical_data["harness_source"] = None

        # --- Step 4.2: CSR vs Master Note cross-check (gateway projects). The plan one-line is
        # authoritative for the delivered CSR; the note only forces a human verify when it disagrees
        # (catches a dropped or hallucinated CSR). Never overrides the plan. ---
        if electrical_data.get("gateway_count"):
            plan_csr = [int(a) for a in (electrical_data.get("csr_breakers") or []) if a]
            kind, note_amps = self._parse_note_csr(mn_text)
            electrical_data["csr_note_check"] = {"note": kind, "note_amps": note_amps, "plan_csr": plan_csr}
            if kind == "none" and plan_csr:
                extraction_flags.append({
                    "level": "HARD", "item": "csr_note_conflict",
                    "msg": f"Master Note says NO CSR breaker, but the one-line read CSR {plan_csr}A. "
                           f"Verify PV-5 — plan is authoritative, but one of these is wrong."})
            elif kind == "amps" and set(note_amps) != set(plan_csr):
                extraction_flags.append({
                    "level": "HARD", "item": "csr_note_conflict",
                    "msg": f"Master Note states CSR {note_amps}A but the one-line read CSR "
                           f"{plan_csr or 'none'}. Verify PV-5 (a CSR main may be dropped or misread). "
                           f"Plan remains authoritative; this is a hold, not an override."})

        # --- Step 5: Racking BOM table (Solar rows 33-90). Text layer where present (roof inline
        # table); Vision fallback for rasterized PV-3.x BOM sheets. Attachment type classified for
        # row routing. ---
        en_blob = " ".join([electrical_data.get("one_line_text") or "",
                            cover_data.get("inverter_model") or "",
                            cover_data.get("inverter_manufacturer") or ""]).upper()
        has_enphase = "ENPHASE" in en_blob or "IQ8" in en_blob
        module_count = cover_data.get("module_quantity") or 0
        try:
            racking = await self._extract_racking(pdf_path, module_count, has_enphase)
        except Exception as e:  # noqa: BLE001 — never fail the whole run on the racking read
            racking = {"format": "absent", "unresolved": [f"racking extraction error: {e}"]}
            warnings.append(f"racking table extraction failed: {e}")

        # --- Merge and return ---
        planset = self._merge(cover_data, electrical_data, array_data,
                              string_data, mount_kit, warnings)
        planset.extraction_flags = extraction_flags
        planset.racking = racking
        return planset

    async def _extract_string_map(self, image_b64: str) -> dict:
        """
        Extract strings-per-roof-plane from the PV-3.1 string map.

        Governing facts (Tron Solar):
          * A STRING is one dashed line of a SINGLE color. The STRING LEGEND on PV-3.1 maps each
            color to a string number (e.g. STRING #1 = red dashed, #2 = cyan dashed, ...).
          * Strings NEVER cross roof planes — every string is fully contained within one plane.
        So strings_per_plane[plane] = count of DISTINCT string colors whose dashed routing appears
        within that plane's module boundary. This count, not module count, drives J-boxes
        (max(1, ceil(strings_on_plane/4)) per plane).
        """
        prompt = """You are reading a Tron Solar STRING MAP (PV-3.1).

There is a STRING LEGEND that maps each dashed-line COLOR to a string number (STRING #1, #2, ...).
Each string is one continuously-colored dashed line. STRINGS NEVER CROSS ROOF PLANES — every
string is fully contained on a single physical roof plane.

For EACH physical roof plane (Roof #1, Roof #2, ...), determine how many DISTINCT string colors
(i.e. how many separate strings) are routed within that plane's modules. Count by color, using the
legend — do NOT infer string count from module count.

Also report whether any text near the expansion unit says "stack" or "wall mount".

Return ONLY valid JSON, no other text:
{
  "planes": [
    { "plane": 1, "string_numbers": [1,2,3,4,5], "string_count": 5 },
    { "plane": 2, "string_numbers": [6,7], "string_count": 2 }
  ],
  "total_strings": 7,
  "plan_mount": "stack or wall or null",
  "confidence": 0.0
}

Rules:
- string_count for a plane = number of distinct string colors confined to that plane
- string_numbers lists the legend string numbers found on that plane
- total_strings = sum of all planes' string_count (should equal the project string total)
- plan_mount: "stack"/"wall" only if explicitly written near the expansion unit, else null
- confidence 0.0-1.0 reflecting how clearly the colored strings could be separated per plane"""

        raw = await self._call_claude([image_b64], prompt)
        try:
            return self._parse_json(raw)
        except Exception:
            return {"planes": [], "total_strings": None, "plan_mount": None, "confidence": 0.0}

    async def _extract_cover_sheet(self, image_b64: str) -> dict:
        """Extract all fields from PV-1 cover sheet."""
        prompt = """You are extracting data from a Tron Solar planset cover sheet (PV-1).
Extract the following fields and return ONLY valid JSON, no other text.

{
  "customer_name": "string",
  "customer_address": "string",
  "system_size_dc_kw": number,
  "system_size_ac_kw": number,
  "module_manufacturer": "string",
  "module_model": "string",
  "module_wattage": number,
  "module_quantity": number,
  "inverter_manufacturer": "string",
  "inverter_model": "string",
  "inverter_quantity": number,
  "battery_manufacturer": "string or null",
  "battery_model": "string or null",
  "battery_quantity": "number or null",
  "battery_kwh": "number or null",
  "has_expansion_unit": boolean,
  "expansion_model": "string or null",
  "expansion_quantity": "number or null",
  "utility_company": "string",
  "ahj": "string",
  "design_date": "string",
  "confidence": {
    "module_info": 0.0,
    "inverter_info": 0.0,
    "battery_info": 0.0,
    "system_size": 0.0
  }
}

Rules:
- module_wattage is in Watts (W), not kW
- battery_kwh is total capacity for ALL batteries combined
- has_expansion_unit is true if a Powerwall 3 Expansion Unit or model 1807000 appears
- inverter_manufacturer must match exactly one of these ComEd options if possible:
  Tesla Inc., Enphase Energy, Inc., SolarEdge Technologies Ltd.
- battery_manufacturer must match exactly: Tesla Inc. (for Powerwall)
- Return null for missing optional fields, never omit keys
- confidence values are 0.0-1.0"""

        raw = await self._call_claude([image_b64], prompt)
        return self._parse_json(raw)

    async def _extract_three_line(self, image_b64: str) -> dict:
        """Extract electrical data from the PV-5 three-line diagram (drives the engine's electrical blocks)."""
        prompt = """You are extracting the electrical schedule from a Tron Solar three-line diagram (PV-5).
Return ONLY valid JSON, no other text:

{
  "meter_number": "string or null",
  "service_type": "underground or overhead or null",
  "nominal_voltage": number or null,
  "main_panel_amperage": integer or null,
  "interconnection_method": "load side or line side or null",

  "ac_disconnects": [ {"amp": integer, "fused": boolean, "fuse_amp": integer or null} ],
  "dc_disconnects": [ {"poles": integer} ],

  "new_meter_drawn": boolean,
  "meter_pn": "string or null",
  "new_msp_drawn": boolean,
  "msp_pn": "string or null",

  "gateway_count": integer,
  "backup_switch": boolean,
  "pw3_skus": ["string"],
  "pw3_drawn_count": integer,
  "inverter_sku": "string or null",
  "remote_meter_count": integer,
  "expansion_drawn_count": integer,
  "harness_pn": "string or null",
  "expansion_mount": "stack or wall or null",

  "buskit_breakers": [ {"amp": integer, "poles": integer} ],
  "csr_breakers": [ integer ]
}

Rules — read the LABELS, not just the symbols:
- meter_number: the meter serial/ID near the meter symbol (NOT the meter equipment SKU).
- service_type / nominal_voltage (use 240 for 120/240V) / main_panel_amperage / interconnection_method
  as before; null if not clearly visible.
- ac_disconnects: ONE entry per AC disconnect drawn. The DISCONNECT rating and the FUSE rating are
  TWO DIFFERENT numbers — do not conflate them:
  * "amp" = the DISCONNECT / switch / enclosure rating (e.g. "60A FUSED AC DISCONNECT" -> amp:60).
  * "fused" = true if the disconnect is labeled FUSED (else false; read the label, e.g.
    "FUSED"/"NON-FUSED", "PV/ESS DISCONNECT").
  * "fuse_amp" = the FUSE size INSIDE the enclosure (e.g. "(2) 40A FUSES" -> fuse_amp:40), normally
    LOWER than the disconnect rating; null if non-fused. If the disconnect rating and the fuse rating
    come out EQUAL, re-read — they are usually different (a 60A disco commonly holds 40A fuses).
  EXAMPLE: "60A FUSED AC DISCONNECT with (2) 40A FUSES" -> {"amp": 60, "fused": true, "fuse_amp": 40}.
- dc_disconnects: ONE entry per DC disconnect; "poles" = pole count (2 = single string, 4 = two strings).
- new_meter_drawn: true ONLY if the plan draws/specifies a NEW meter/socket/base (e.g. PV-1 scope or
  PV-5 note "UPGRADE METER BASE TO NEW ..."). An existing/retained meter -> false.
- meter_pn: the EXACT NEW meter equipment part number (e.g. "U9551-RXL-QG-5T9-AMS"), else null.
- new_msp_drawn: true ONLY if the plan specifies a NEW main service panel; an existing MSP that remains -> false.
- msp_pn: the EXACT NEW MSP part number, else null.
- gateway_count: number of Tesla Energy Gateway units drawn (usually 0 or 1).
- backup_switch: true if a Tesla Backup Switch is drawn (Gateway and Backup Switch rarely coexist).
- pw3_skus: one entry per Powerwall 3 unit drawn, using its 1707000-... SKU (EXCLUDE PW3 Expansion units).
- pw3_drawn_count: how many PW3 BLOCKS you actually count drawn in the schematic (a cross-check on the
  text-block count; report what you see even if it differs from the equipment list).
- inverter_sku: a standalone Tesla inverter SKU if drawn, else null.
- remote_meter_count: count of "TESLA REMOTE ENERGY METER" blocks, else 0.
- expansion_drawn_count: how many Powerwall 3 EXPANSION units (1807000) are drawn, else 0. (The
  equipment text block is the authoritative count; this is a cross-check.)
- harness_pn: find the labeled EXPANSION HARNESS callout on the one-line — it reads
  "EXPANSION HARNESS P/N 1875157-NN-X" (NN = length code 05, 20, or 40) and points at the line
  BETWEEN the Powerwalls. Return the EXACT full P/N including the -NN- (e.g. "1875157-05-X"), else
  null. The suffix matters (-05 = stack, -20/-40 = wall); transcribe it, never infer from the mount.
- expansion_mount: "stack" or "wall" only if a mount keyword is written near the expansion unit, else null.
- GATEWAY BREAKER CLASSIFICATION — read carefully, this is error-prone. The Tesla Gateway has an
  internal BUS-KIT enclosure. Put each breaker in EXACTLY ONE of the two lists, never both:
  * buskit_breakers ({"amp","poles"}): EVERY breaker SYMBOL drawn INSIDE the bus-kit box — enumerate
    them ALL from the diagram (not a fixed number, not derived from the Powerwall count). There is
    normally one 60A/2P per Powerwall 3, AND there may be ADDITIONAL breakers of other ratings
    (e.g. a 100A/2P). Read each one's amp+poles off its label and list every one. The 100A/2P, if
    drawn in the bus-kit, MUST be captured here. Example — two 60A/2P plus one 100A/2P inside the box:
      [{"amp":60,"poles":2},{"amp":60,"poles":2},{"amp":100,"poles":2}].
  * csr_breakers ([amp]): ONLY a real breaker SYMBOL drawn OUTSIDE the bus-kit box, landing into the
    gateway from the line/right side. Record amperage only. If no such external breaker is drawn,
    csr_breakers = [].
    -- The gateway's ENCLOSURE rating is NOT a breaker. A label like "TESLA GATEWAY 3, 200A, NEMA 3R,
       240/120V" is the gateway's bus/enclosure amperage — do NOT emit it as a csr_breaker.
    -- The distinction is POSITIONAL, not by rating: a CSR's amperage OFTEN EQUALS the gateway rating
       (a 200A CSR on a 200A gateway is normal), so do NOT disqualify a breaker just because its
       rating matches the gateway. A breaker SYMBOL outside the bus-kit = CSR; the enclosure's printed
       rating label = not a breaker.
  * The EXISTING house-panel main (e.g. "(E) MAIN BREAKER TO HOUSE 200A/2P") is NEITHER — exclude it.
- Use [] / 0 / false for absent items; null only for the string/number fields that say "or null"."""

        raw = await self._call_claude([image_b64], prompt)
        try:
            return self._parse_json(raw)
        except (ValueError, json.JSONDecodeError) as e:
            # The structured electrical read could not be parsed OR recovered. Do NOT silently return
            # an empty dict — that would degrade quietly into wrong downstream defaults. Surface a
            # sentinel so extract() raises a HARD flag making the unreliable read explicit. The
            # deterministic text-layer fields (bus-kit, CSR, harness, counts) still apply on top.
            log.error("PV-5 three-line parse failed (unrecoverable): %s", e)
            log.error("PV-5 raw response (first 1000 chars): %r", (raw or "")[:1000])
            return {"_pv5_parse_error": str(e)}

    async def _extract_roof_plan(self, image_b64: str, module_wattage: float) -> dict:
        """Extract array layout from PV-3 roof plan."""
        prompt = f"""You are extracting solar array data from a Tron Solar roof plan (PV-3).
The module wattage is {module_wattage}W.

Extract all arrays and return ONLY valid JSON, no other text.

{{
  "arrays": [
    {{
      "tilt": number,
      "azimuth": number,
      "module_count": integer,
      "dc_size_kw": number
    }}
  ]
}}

Rules:
- Each distinct roof face with modules is a separate array
- tilt: the roof pitch/tilt angle in degrees (e.g. 20, 25, 30)
- azimuth: compass direction in degrees (South=180, East=90, West=270, North=0/360)
- module_count: number of panels on that array
- dc_size_kw: module_count * {module_wattage} / 1000 (calculate this yourself)
- If tilt or azimuth is not labeled, make a reasonable estimate based on roof orientation
- Return at least one array even if details are unclear"""

        raw = await self._call_claude([image_b64], prompt)
        try:
            return self._parse_json(raw)
        except (ValueError, json.JSONDecodeError) as e:
            # PV-3 has been the call that returns empty / non-JSON. Log the raw response, attributed
            # to the roof-plan step, so we can see exactly what the model returned before it raises.
            log.error("PV-3 roof-plan parse failed: %s", e)
            log.error("PV-3 raw response (first 1000 chars): %r", (raw or "")[:1000])
            raise

    def _merge(
        self,
        cover: dict,
        electrical: dict,
        roof: dict,
        strings: dict,
        mount_kit: str,
        warnings: list,
    ) -> PlansetData:
        """Merge extraction results into a PlansetData instance."""
        # strings-per-plane from PV-3.1 (by dashed-line color; strings never cross planes)
        strings_per_plane = {}
        for p in strings.get("planes", []) or []:
            plane = p.get("plane")
            cnt = p.get("string_count")
            if plane is not None and cnt is not None:
                strings_per_plane[int(plane)] = int(cnt)

        arrays = []
        for idx, a in enumerate(roof.get("arrays", []), start=1):
            plane = a.get("roof_plane", idx)
            arrays.append(ArrayInfo(
                tilt=a.get("tilt", 20.0),
                azimuth=a.get("azimuth", 180.0),
                module_count=a.get("module_count", 0),
                dc_size_kw=a.get("dc_size_kw", 0.0),
                roof_plane=plane,
                strings_on_plane=strings_per_plane.get(int(plane)),
            ))

        confidence = cover.pop("confidence", {})

        return PlansetData(
            # Cover sheet
            customer_name=cover.get("customer_name", ""),
            customer_address=cover.get("customer_address", ""),
            system_size_dc_kw=cover.get("system_size_dc_kw", 0.0),
            system_size_ac_kw=cover.get("system_size_ac_kw", 0.0),
            module_manufacturer=cover.get("module_manufacturer", ""),
            module_model=cover.get("module_model", ""),
            module_wattage=cover.get("module_wattage", 0.0),
            module_quantity=cover.get("module_quantity", 0),
            inverter_manufacturer=cover.get("inverter_manufacturer", ""),
            inverter_model=cover.get("inverter_model", ""),
            inverter_quantity=cover.get("inverter_quantity", 1),
            battery_manufacturer=cover.get("battery_manufacturer"),
            battery_model=cover.get("battery_model"),
            battery_quantity=cover.get("battery_quantity"),
            battery_kwh=cover.get("battery_kwh"),
            has_expansion_unit=cover.get("has_expansion_unit", False),
            expansion_model=cover.get("expansion_model"),
            expansion_quantity=cover.get("expansion_quantity"),
            utility_company=cover.get("utility_company", ""),
            ahj=cover.get("ahj", ""),
            design_date=cover.get("design_date", ""),
            # Electrical
            meter_number=electrical.get("meter_number"),
            service_type=electrical.get("service_type"),
            nominal_voltage=electrical.get("nominal_voltage"),
            main_panel_amperage=electrical.get("main_panel_amperage"),
            interconnection_method=electrical.get("interconnection_method"),
            # Arrays
            arrays=arrays,
            # String map (PV-3.1)
            strings_per_plane=strings_per_plane,
            # Expansion mount kit (resolved)
            expansion_mount_kit=mount_kit,
            # Metadata
            confidence_scores=confidence,
            extraction_warnings=warnings,
            # Structured PV-5 electrical reads (whole dict; orchestrator reads the keys it needs)
            electrical=dict(electrical or {}),
        )

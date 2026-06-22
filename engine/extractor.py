"""
Planset PDF Extractor
Uses Claude Vision (claude-sonnet-4-20250514) to extract interconnection fields
from Tron Solar planset PDFs.

Part of the planset-extractor skill.
"""

import os
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
        # Render at 2x resolution (UNCHANGED — the diagnostic saves exactly this, not a higher res).
        mat = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=mat)
        img_bytes = pix.tobytes("png")
        doc.close()
        if self.debug_pages_dir and label:
            safe = "".join(c if (c.isalnum() or c in "-._") else "_" for c in label)
            fn = os.path.join(self.debug_pages_dir, f"selected_{safe}.png")
            with open(fn, "wb") as fh:
                fh.write(img_bytes)
            self._dbg(f"[{label}] SAVED page index {page_number} -> {os.path.basename(fn)} "
                      f"({pix.width}x{pix.height}px @2x)")
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

    def _find_page_by_label(self, pdf_path: str, label: str) -> Optional[int]:
        """
        Find page number by searching for a label (e.g. 'PV-5') in page text.
        Returns 0-indexed page number or None if not found.
        """
        doc = fitz.open(pdf_path)
        for i, page in enumerate(doc):
            text = page.get_text()
            if label in text:
                pos = text.find(label)
                snippet = " ".join(text[max(0, pos - 30):pos + 40].split())
                doc.close()
                log.info("find_page_by_label(%r) -> page index %d (matched on: %r)", label, i, snippet)
                self._dbg(f"[{label}] resolved to PAGE INDEX {i}  (matched text: {snippet!r})")
                return i
        doc.close()
        log.warning("find_page_by_label(%r) -> NOT FOUND; caller will use a fallback page", label)
        self._dbg(f"[{label}] NOT FOUND by label — caller falls back to a fixed page index")
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

    def _parse_json(self, raw: str) -> dict:
        """Parse JSON from a Claude response.

        Strips markdown code fences, then extracts the object from the first '{' to the last '}'
        (drops any prose the model wrapped around the JSON). On failure raises ValueError WITH the
        raw response text included, so the caller/logs show what the model actually returned."""
        clean = (raw or "").strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[-1]          # drop the opening ``` / ```json line
            if clean.rstrip().endswith("```"):
                clean = clean.rstrip()[:-3]           # drop the closing fence
            clean = clean.strip()
        start, end = clean.find("{"), clean.rfind("}")
        if start != -1 and end > start:
            clean = clean[start:end + 1]
        try:
            return json.loads(clean)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Claude response was not valid JSON ({e}). "
                f"Raw response (first 1000 chars): {(raw or '')[:1000]!r}"
            ) from e

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

        # --- Step 1: Extract cover sheet (PV-1, always page 0) ---
        cover_b64 = self._pdf_page_to_base64(pdf_path, 0, label="PV-1_cover")
        cover_data = await self._extract_cover_sheet(cover_b64)

        # --- Step 2: Extract three-line diagram (PV-5) ---
        pv5_page = self._find_page_by_label(pdf_path, "PV-5")
        if pv5_page is None:
            pv5_page = 4
            warnings.append("PV-5 page not found by label; using fallback page 4")
        pv5_b64 = self._pdf_page_to_base64(pdf_path, pv5_page, label="PV-5")
        electrical_data = await self._extract_three_line(pv5_b64)

        # --- Step 3: Extract roof plan (PV-3) ---
        pv3_page = self._find_page_by_label(pdf_path, "PV-3")
        if pv3_page is None:
            pv3_page = 2
            warnings.append("PV-3 page not found by label; using fallback page 2")
        pv3_b64 = self._pdf_page_to_base64(pdf_path, pv3_page, label="PV-3")
        module_wattage = cover_data.get("module_wattage", 400)
        array_data = await self._extract_roof_plan(pv3_b64, module_wattage)

        # --- Step 3.1: Extract string map (PV-3.1) — strings-per-plane by dashed-line color ---
        pv31_page = self._find_page_by_label(pdf_path, "PV-3.1")
        if pv31_page is None:
            # some plansets put the string map on PV-3 itself
            pv31_page = pv3_page
            warnings.append("PV-3.1 page not found by label; using PV-3 page for string map")
        pv31_b64 = self._pdf_page_to_base64(pdf_path, pv31_page, label="PV-3.1")
        string_data = await self._extract_string_map(pv31_b64)

        # --- Step 4: Resolve expansion mount kit (plans -> master note -> default wall) ---
        plan_mount = string_data.get("plan_mount") or array_data.get("plan_mount")
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
        if master_note_form is None and not plan_mount:
            warnings.append("expansion mount: no plan keyword and no Master Note form supplied; "
                            "defaulted to wall — fetch get_form(Master Note) and pass master_note_form")

        # --- Merge and return ---
        return self._merge(cover_data, electrical_data, array_data,
                           string_data, mount_kit, warnings)

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
  "inverter_sku": "string or null",
  "remote_meter_count": integer,

  "buskit_breakers": [ {"amp": integer, "poles": integer} ],
  "csr_breakers": [ integer ],

  "one_line_text": "string"
}

Rules — read the LABELS, not just the symbols:
- meter_number: the meter serial/ID near the meter symbol (NOT the meter equipment SKU).
- service_type / nominal_voltage (use 240 for 120/240V) / main_panel_amperage / interconnection_method
  as before; null if not clearly visible.
- ac_disconnects: ONE entry per AC disconnect drawn. "amp" = the disconnect rating; "fused" = true only
  if the disconnect is labeled FUSED (read the label, e.g. "FUSED"/"NON-FUSED" or "PV/ESS DISCONNECT");
  "fuse_amp" = the FUSE rating drawn inside the block (may differ from the disconnect amp), else null.
- dc_disconnects: ONE entry per DC disconnect; "poles" = pole count (2 = single string, 4 = two strings).
- new_meter_drawn: true ONLY if the plan draws/specifies a NEW meter/socket/base (e.g. PV-1 scope or
  PV-5 note "UPGRADE METER BASE TO NEW ..."). An existing/retained meter -> false.
- meter_pn: the EXACT NEW meter equipment part number (e.g. "U9551-RXL-QG-5T9-AMS"), else null.
- new_msp_drawn: true ONLY if the plan specifies a NEW main service panel; an existing MSP that remains -> false.
- msp_pn: the EXACT NEW MSP part number, else null.
- gateway_count: number of Tesla Energy Gateway units drawn (usually 0 or 1).
- backup_switch: true if a Tesla Backup Switch is drawn (Gateway and Backup Switch rarely coexist).
- pw3_skus: one entry per Powerwall 3 unit drawn, using its 1707000-... SKU (EXCLUDE PW3 Expansion units).
- inverter_sku: a standalone Tesla inverter SKU if drawn, else null.
- remote_meter_count: count of "TESLA REMOTE ENERGY METER" blocks, else 0.
- buskit_breakers: breakers drawn INSIDE the "GATEWAY INTERNAL BUS-KIT" enclosure (left side of the
  gateway). One entry per breaker: {"amp", "poles"} read from its label (e.g. "60A/2P","100A/2P").
- csr_breakers: amperages of rated MAIN breakers landing into the gateway from the RIGHT, OUTSIDE the
  bus-kit (e.g. [200]). Empty if none. Do NOT include service-panel mains.
- one_line_text: a verbatim transcription of the one-line's text labels/notes (so supply-side tap SKUs
  like "K4977", "NSI IT-3/0", "IT-250" can be matched). Keep it under ~1500 characters.
- Use [] / 0 / false for absent items; null only for the string/number fields that say "or null"."""

        raw = await self._call_claude([image_b64], prompt)
        try:
            return self._parse_json(raw)
        except (ValueError, json.JSONDecodeError) as e:
            # Don't fail the whole run on a PV-5 parse miss — log the raw response and return the
            # minimal shape so the orchestrator flags missing electrical detail instead of crashing.
            log.error("PV-5 three-line parse failed: %s", e)
            log.error("PV-5 raw response (first 1000 chars): %r", (raw or "")[:1000])
            return {}

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

"""TEXT-ONLY equipment reads (user rule, 2026-10-16): disconnects (AC and DC), attachments and the MSP come
from planset TEXT — the BILL OF MATERIALS / equipment schedule table, or a text call-out on the one-line.
No text call-out -> no device. Vision symbol reads never ADD a disconnect.

Source priority for disconnects:
  1. Equipment/BOM table rows (EQUIPMENT | QTY | DESCRIPTION) on any sheet — read with PyMuPDF
     find_tables() (ruled tables; handles wrapped multi-line labels like
     'PHOTOVOLTAIC/ENERGY STORAGE SYSTEM AC DISCONNECT'), with a line-order fallback.
  2. If the table names no disconnect of a kind: one-line (PV-5) text call-outs that carry a rating
     ('60A FUSED AC DISCONNECT', 'DC DISCONNECT, 32A/600V, 2 POLE'), one device per distinct call-out line.
  3. Neither -> none of that kind.
"""
from __future__ import annotations

import re

import fitz  # PyMuPDF

_INT = re.compile(r"^\s*0*(\d{1,4})\s*$")
_NONFUSED = ("NON-FUSED", "NON FUSED", "NONFUSED", "NON-FUSE", "UNFUSED", "NF ")


def _clean(s) -> str:
    return " ".join(str(s or "").replace("\n", " ").split()).upper()


def _rows_from_tables(page) -> list[tuple[str, int, str]]:
    out = []
    try:
        tabs = page.find_tables().tables
    except Exception:  # noqa: BLE001
        return out
    for tb in tabs:
        try:
            data = tb.extract()
        except Exception:  # noqa: BLE001
            continue
        if not data:
            continue
        # locate header row + QTY column
        qcol = None
        start = 0
        for hi, row in enumerate(data[:3]):
            cells = [_clean(c) for c in row]
            if "QTY" in cells or "QUANTITY" in cells:
                qcol = cells.index("QTY") if "QTY" in cells else cells.index("QUANTITY")
                start = hi + 1
                break
        if qcol is None or qcol == 0:
            continue
        for row in data[start:]:
            cells = [_clean(c) for c in row]
            if len(cells) <= qcol:
                continue
            m = _INT.match(cells[qcol] or "")
            if not m:
                continue
            equip = " ".join(c for c in cells[:qcol] if c)
            desc = " ".join(c for c in cells[qcol + 1:] if c)
            out.append((equip, int(m.group(1)), desc))
    return out


_TABLE_END = ("ROOF DESCRIPTION", "ARRAY AREA", "NOTE:", "NOTES:", "LEGEND", "SCALE", "(N) ", "ROOF ACCESS",
              "SHEET NAME", "FIRE SETBACK", "ROOF #", "STRING #")


def _rows_from_lines(text: str) -> list[tuple[str, int, str]]:
    """Fallback for unruled tables in EQUIPMENT / QTY / DESCRIPTION reading order: label line(s), then an
    integer-only QTY line, then description line(s) up to the next label. The block is BOUNDED: it ends
    at the first line that starts another drawing element (ROOF DESCRIPTION, ARRAY AREA, NOTE, (N) ...
    call-outs). Without that bound the parser ran into the ROOF DESCRIPTION table and read a plane's
    module count as a quantity (user, Moyes #971010: ProteaBracket 61 + 13 -> 74)."""
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    try:
        i0 = next(i for i, l in enumerate(lines) if l.upper() == "EQUIPMENT")
    except StopIteration:
        return []
    body = []
    for l in lines[i0 + 1:]:
        U = l.upper()
        if U in ("QTY", "QUANTITY", "DESCRIPTION"):
            continue
        if any(U.startswith(t) for t in _TABLE_END):
            break
        body.append(l)
    lines = body
    qidx = [i for i, l in enumerate(lines) if _INT.match(l)]
    out = []
    prev_end = 0
    for k, qi in enumerate(qidx):
        nxt = qidx[k + 1] if k + 1 < len(qidx) else len(lines)
        label_lines = lines[prev_end:qi][-3:]
        desc_lines = lines[qi + 1: nxt]
        cut = len(desc_lines)
        if k + 1 < len(qidx):
            # the next row's label = trailing short all-caps lines (case-insensitive: "MCI's")
            for j in range(len(desc_lines) - 1, 0, -1):
                d = desc_lines[j]
                if d.upper() == d.upper().strip() and d == d.upper() or (len(d) <= 22 and not
                                                                         re.search(r"\d", d)):
                    if len(d) <= 30:
                        cut = j
                        continue
                break
        desc_lines = desc_lines[:cut][:3]                  # a description never spans > 3 lines
        if not label_lines:
            prev_end = qi + 1 + cut
            continue                                       # no EQUIPMENT label -> not a table row
        out.append((_clean(" ".join(label_lines)), int(_INT.match(lines[qi]).group(1)),
                    _clean(" ".join(desc_lines))))
        prev_end = qi + 1 + cut
    return out


def equipment_rows(pdf_path: str) -> tuple[list[tuple[str, int, str]], int | None]:
    """All equipment/BOM table rows (equipment, qty, description) from the sheet that has the most
    rows. Returns (rows, page_index)."""
    best: tuple[list, int | None] = ([], None)
    doc = fitz.open(pdf_path)
    try:
        for i in range(doc.page_count):
            page = doc[i]
            U = (page.get_text() or "").upper()
            if not ("QTY" in U and ("EQUIPMENT" in U or "BILL OF MATERIAL" in U)):
                continue
            rows = _rows_from_tables(page) or _rows_from_lines(page.get_text())
            if len(rows) > len(best[0]):
                best = (rows, i)
    finally:
        doc.close()
    return best


# ---------------- disconnects ----------------

def _is_existing_or_service(blob: str) -> bool:
    """(E)/EXISTING equipment and the utility SERVICE disconnect / MSD are never BOM disconnects
    (user, Latham #916365: existing 200A MSD read as a new 200A disconnect)."""
    b = " " + blob + " "
    return ("(E)" in b or " EXISTING " in b or "SERVICE DISCONNECT" in b or " MSD " in b
            or "MAIN DISCONNECT" in b or "SERVICE ENTRANCE" in b)


def _is_fused(blob: str) -> bool:
    b = blob + " "
    return "FUSE" in b and not any(n in b for n in _NONFUSED)


def _fuse_amp(blob: str):
    m = (re.search(r"\(\s*\d\s*\)\s*(\d{2,3})\s*A\s*FUSE", blob)
         or re.search(r"(\d{2,3})\s*A\s*FUSES", blob)
         or re.search(r"FUSED\s*(?:AT|@|WITH|W/)\s*(\d{2,3})\s*A", blob))
    return int(m.group(1)) if m else None


def _disco_amp(blob: str):
    # disconnect rating = first NNA that isn't a fuse rating and isn't a voltage
    # '(2) 40A FUSES' is a fuse size, '60A FUSED' is the disconnect rating -> exclude only FUSE/FUSES.
    # Skip voltage-paired ratings like '32A/600V' only for DC (handled by poles); AC takes the first.
    for m in re.finditer(r"(\d{2,3})\s*A\b(?!\s*FUSES?\b)", blob):
        return int(m.group(1))
    return None


def _poles(blob: str):
    m = re.search(r"(\d)\s*-?\s*POLE", blob) or re.search(r"\b(\d)P\b", blob)
    return int(m.group(1)) if m else None


def disconnects_from_rows(rows) -> tuple[list[dict], list[dict]]:
    ac, dc = [], []
    for equip, qty, desc in rows:
        E, D = _clean(equip), _clean(desc)
        both = E + " " + D
        if "DISCONNECT" not in both or qty <= 0 or _is_existing_or_service(both):
            continue
        is_dc = ("DC DISCONNECT" in E) or ("DC DISCONNECT" in D and "AC DISCONNECT" not in both)
        is_ac = not is_dc and ("AC DISCONNECT" in both or "DISCONNECT" in E)
        if is_dc:
            p = _poles(D) or _poles(E)
            if p:
                dc += [{"poles": p, "source": "table"}] * qty
        elif is_ac:
            amp = _disco_amp(D) or _disco_amp(E)
            if amp:
                fused = _is_fused(both)
                ac += [{"amp": amp, "fused": fused, "fuse_amp": _fuse_amp(both) if fused else None,
                        "source": "table"}] * qty
    return ac, dc


def disconnects_from_callouts(text: str) -> tuple[list[dict], list[dict]]:
    """One-line text call-outs. A call-out must carry its rating on the same line (AC: amps; DC: poles)
    so general notes ('AC DISCONNECT SHALL BE LOCKABLE') never count. Identical call-out lines are one
    device each only if they appear on distinct lines; exact duplicates of a 'TYP.'/legend line are
    ignored by de-duping lines that end in 'TYP'."""
    ac, dc = [], []
    for raw in (text or "").splitlines():
        L = _clean(raw)
        if ("DISCONNECT" not in L or "TYP" in L or "SHALL" in L or "NOTE" in L
                or _is_existing_or_service(L)):
            continue
        if "DC DISCONNECT" in L:
            p = _poles(L)
            if p:
                dc.append({"poles": p, "source": "callout"})
        elif "AC DISCONNECT" in L or "DISCONNECT" in L:
            amp = _disco_amp(L)
            if amp:
                fused = _is_fused(L)
                ac.append({"amp": amp, "fused": fused, "fuse_amp": _fuse_amp(L) if fused else None,
                           "source": "callout"})
    return ac, dc


# ---------------- attachments (per type) ----------------

def attachments_from_rows(rows) -> dict[str, int]:
    """Per-type attachment quantities from the BOM table — a project can mix roof types (K2 Multimount
    on the shingle structure + S-5! ProteaBracket on the metal structure). Returns {attachment_type: qty}."""
    out: dict[str, int] = {}
    for equip, qty, desc in rows:
        E = _clean(equip)
        if not E or not ("ATTACHMENT" in E or "PROTEA" in E or "SOLARFOOT" in E or "MULTIMOUNT" in E):
            continue                                   # only real ATTACHMENT table rows
        b = E + " " + _clean(desc)
        if "L FOOT" in b and "PROTEA" not in b and "SOLARFOOT" not in b and "SOLAR FOOT" not in b:
            continue
        if "SOLARFOOT" in b or "SOLAR FOOT" in b:
            t = "S5_SOLARFOOT"
        elif "PROTEA" in b:
            t = "S5_PROTEABRACKET"
        elif "MULTIMOUNT" in b or "MULTI MOUNT" in b or "MULTI-MOUNT" in b:
            t = "K2_SHINGLE"
        elif "ATTACHMENT" in _clean(equip):
            t = "K2_SHINGLE" if "SHINGLE" in b else "UNKNOWN"
        else:
            continue
        out[t] = out.get(t, 0) + qty
    return out


def k2_attach_mounting(rows) -> dict[str, int]:
    """K2 Multimount deck vs rafter split from the BOM description ('(DECK MOUNTED)' / '(RAFTER ...)')."""
    out = {"deck": 0, "rafter": 0}
    for equip, qty, desc in rows:
        b = _clean(equip) + " " + _clean(desc)
        if "MULTIMOUNT" in b or ("ATTACHMENT" in _clean(equip) and "PROTEA" not in b and "SOLARFOOT" not in b):
            if "RAFTER" in b or "TRUSS" in b:
                out["rafter"] += qty
            elif "DECK" in b:
                out["deck"] += qty
    return out


# ---------------- roof planes by roof type (per structure) ----------------

def _type_of(blob: str) -> str | None:
    if "SHINGLE" in blob or "ASPHALT" in blob:
        return "shingle"
    if "METAL" in blob or "STANDING SEAM" in blob or "R-PANEL" in blob or "CORRUGATED" in blob:
        return "metal"
    if "TILE" in blob:
        return "other"
    return None


def roof_planes_by_type(pdf_path: str, page_index: int | None) -> list[dict]:
    """Per-plane roof type + module count from the PV-3 ROOF DESCRIPTION table(s). A planset with two
    structures of different roof types prints ONE table per structure (user, 2026-10-16: shingle house
    #1 + metal garage #1/#2). Returns [{"table": k, "roof_type": "shingle"|"metal"|..., "plane": "#1",
    "modules": 12}, ...]; [] if no table is readable."""
    if page_index is None:
        return []
    out: list[dict] = []
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        try:
            tabs = page.find_tables().tables
        except Exception:  # noqa: BLE001
            tabs = []
        k = 0
        for tb in tabs:
            data = [[_clean(c) for c in r] for r in (tb.extract() or [])]
            flat = " ".join(" ".join(r) for r in data)
            if "ROOF" not in flat or "MODULES" not in flat:
                continue
            rtype = _type_of(flat)
            if rtype is None:
                continue
            k += 1
            # modules column = the header cell containing 'MODULES'
            mcol = None
            for r in data[:4]:
                for ci, c in enumerate(r):
                    if "MODULES" in c:
                        mcol = ci
            for r in data:
                if r and re.match(r"^#\s*\d+$", r[0] or ""):
                    cell = r[mcol] if mcol is not None and mcol < len(r) else (r[1] if len(r) > 1 else "")
                    m = _INT.match(cell or "")
                    if m:
                        out.append({"table": k, "roof_type": rtype, "plane": r[0], "modules": int(m.group(1))})
        if out:
            return out
        # line fallback: type sticky from the most recent 'ASPHALT SHINGLE'/'METAL ROOF' header;
        # plane rows '#n <modules> ...' on one line.
        cur, k = None, 0
        for raw in (page.get_text() or "").splitlines():
            L = _clean(raw)
            t = _type_of(L) if ("ROOF" in L or "SHINGLE" in L) else None
            if t:
                cur, k = t, k + 1
                continue
            m = re.match(r"^#\s*(\d+)\s+(\d{1,3})\b", L)
            if m and cur:
                out.append({"table": k, "roof_type": cur, "plane": f"#{m.group(1)}", "modules": int(m.group(2))})
    finally:
        doc.close()
    return out


def attachments_from_callouts(text: str) -> dict[str, int]:
    """Roof-plan call-outs '(112) S-5! PROTEA BRACKET ATTACHMENTS' / '(32) K2 MULTIMOUNT ATTACHMENTS
    (DECK MOUNTED)' — the source when there is no BOM table. Call-outs can wrap, so the text is
    flattened first. Returns {attachment_type: qty} summed over call-outs."""
    flat = " ".join(str(text or "").upper().split())
    out: dict[str, int] = {}
    for m in re.finditer(r"\((\d{1,4})\)\s*([^()]{0,60}?ATTACHMENTS?)", flat):
        rows = [("ATTACHMENT", int(m.group(1)), m.group(2))]
        for k, v in attachments_from_rows(rows).items():
            out[k] = out.get(k, 0) + v
    return out

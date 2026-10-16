"""
electrical_engine.py — Tron Solar Electrical BOM, every block as a STRUCTURAL function.

Companion to racking_engine.py. The rules here were validated across Eroh, Fickas, Sams,
Bennett, Ebright, Williford, Woroszylo, Dare. Previously they lived only as prose in
Tron_Solar_BOM_Engine_Complete_Record.md and were applied via per-project scripts — which is
exactly the failure mode the standing instructions warn against (hand-selected lines miss whole
blocks). This module makes each block a function that DECIDES ITS OWN APPLICABILITY and returns
{electrical_row: qty} plus flags, so the consolidated builder can run every block unconditionally.

Row map (Electrical BOM sheet) is the single source of truth:
  5  DU221RB 30A non-fused      6  DU222RB 60A non-fused   7 DU323RB 100A nf   8 DU324RB 200A nf
  10 D222RB 60A FUSED          11 D223NRB 100A fused      12 D224NRB 200A fused
  13 B075 (30/60A hub)         14 B125 (100A hub)         15 B200 (200A hub)
  17 EN-EP200G 2-POLE DC       18 SI32 4-POLE DC
  19 PE69-3020 RSD
  21 PB2-300  22 PK23GTACP groundbar  23 LK100ANCP  24 SGB-386CL
  26 K4977-INT  27 NSI IT-3/0  28 NSI IT-250        (taps, flat qty 3 if present)
  51-57 Tesla PW3 / Gateway / Inverter / Remote meter / Backup switch
  59 expansion 1807000  61 stack kit  62 wall-mount kit  63 -05 harness  64 -20 harness  65 -40 harness
  98-110 BR breakers  112-114 CSR  116-128 HOM (128 fallback)
  130-144 FRN-R fuses
"""
from __future__ import annotations
import math
import re


# ---------- WAREHOUSE ZONE (header B2) — user, Nelson REVA #860742 ----------
#
# The warehouse zone lives in TWO Coperniq places that must agree:
#   1. project.custom["zone"]  -> e.g. ["Zone 3"]   (AUTHORITATIVE: clean single value)
#   2. the FIRST line of project.description HTML, e.g. "<strong><u>Zone 3</u></strong>"  (cross-check)
# Hardcoded so the engine fills the header headlessly with NO inference. The custom property wins;
# the description is parsed only to cross-check and to backfill if the property is somehow empty.
def warehouse_zone(project):
    """Resolve the warehouse zone for header B2. Returns (zone_str, flags).

    project: the Coperniq:get_project dict (include_virtual_properties=True).
    Precedence: custom['zone'] (truth) -> description first-line "Zone N" (fallback + cross-check).
    Conflict between the two -> NOTE flag (deliver the custom-property value). Neither present ->
    HARD flag + empty string (do not guess).
    """
    flags: list = []

    def _zone_from_text(s):
        m = re.search(r"zone\s*([0-9]+)", str(s or ""), re.IGNORECASE)
        return f"Zone {m.group(1)}" if m else None

    custom = (project or {}).get("custom", {}) or {}
    cv = custom.get("zone")
    prop_zone = None
    if isinstance(cv, list) and cv:
        prop_zone = _zone_from_text(cv[0]) or str(cv[0]).strip()
    elif isinstance(cv, str) and cv.strip():
        prop_zone = _zone_from_text(cv) or cv.strip()

    desc_zone = _zone_from_text((project or {}).get("description", ""))

    if prop_zone:
        if desc_zone and desc_zone.strip().upper() != prop_zone.strip().upper():
            flags.append({"level": "NOTE", "item": "warehouse_zone_prop_vs_description",
                          "msg": f"Zone: custom property '{prop_zone}' vs description '{desc_zone}'. "
                                 f"Delivering custom-property value."})
        return prop_zone, flags

    if desc_zone:
        flags.append({"level": "NOTE", "item": "warehouse_zone_from_description_only",
                      "msg": f"Zone custom property empty; used description '{desc_zone}'."})
        return desc_zone, flags

    flags.append({"level": "HARD", "item": "warehouse_zone_missing",
                  "msg": "No warehouse zone in custom['zone'] or description. Header B2 left blank; "
                         "supply the zone."})
    return "", flags


# ---------- AC DISCONNECTS + HUBS (rows 5-15) ----------

_NONFUSED_ROW = {30: 5, 60: 6, 100: 7, 200: 8}
_FUSED_ROW    = {60: 10, 100: 11, 200: 12}  # no 30A fused row


def ac_disconnects(disconnects):
    """
    disconnects: list of dicts, one per AC disconnect drawn on the one-line:
        {"amp": 60, "fused": True|False, "fuse_amp": 60 (only if fused)}
    Returns ({row: qty}, flags). Hubs = 2 per disconnect, bucketed by SIZE across fused+non-fused:
        B075(13) = 30A+60A discos, B125(14) = 100A, B200(15) = 200A.
    Fuses: 2 per FUSED disconnect, keyed off the FUSE rating (not the disco rating).
    """
    rows: dict[int, int] = {}
    flags: list = []
    bucket = {"075": 0, "125": 0, "200": 0}  # disconnect COUNT per hub bucket
    fuse_rows: dict[int, int] = {}

    for d in disconnects:
        amp = d["amp"]
        fused = d.get("fused", False)
        table = _FUSED_ROW if fused else _NONFUSED_ROW
        if amp not in table:
            flags.append({"level": "HARD", "item": "disconnect_amp_unmapped",
                          "detail": f"{'fused' if fused else 'non-fused'} {amp}A has no template row"})
            continue
        row = table[amp]
        rows[row] = rows.get(row, 0) + 1

        # hub bucket by disconnect rating
        if amp in (30, 60):
            bucket["075"] += 1
        elif amp == 100:
            bucket["125"] += 1
        elif amp == 200:
            bucket["200"] += 1

        # fuses for fused disconnects (2 each, by the FUSE rating drawn inside). The fuse amp must be
        # READ (extractor resolves it from the PV-5 text layer / Master Note) — never defaulted to the
        # disconnect rating. A null/unmapped fuse_amp is a HARD flag, not a silent substitution.
        if fused:
            f_amp = d.get("fuse_amp")
            f_row = _FRN_ROW.get(f_amp)
            if f_row is None:
                flags.append({"level": "HARD", "item": "fuse_amp_unmapped",
                              "detail": f"{f_amp}A fuse has no FRN-R row (fuse rating must be read "
                                        f"from the plan; not defaulted to the {amp}A disconnect size)"})
            else:
                fuse_rows[f_row] = fuse_rows.get(f_row, 0) + 2

    if bucket["075"]:
        rows[13] = 2 * bucket["075"]
    if bucket["125"]:
        rows[14] = 2 * bucket["125"]
    if bucket["200"]:
        rows[15] = 2 * bucket["200"]

    rows.update(fuse_rows)
    return rows, flags


# ---------- DC DISCONNECTS (rows 17-18) — by pole count ----------

def dc_disconnects(dc_list):
    """
    dc_list: list of dicts, one per DC disconnect drawn on the one-line:
        {"poles": 2}  -> single string  -> row 17 (EN-EP200G)
        {"poles": 4}  -> two strings     -> row 18 (SI32)
    Returns ({row: qty}, flags).
    """
    rows: dict[int, int] = {}
    flags: list = []
    for d in dc_list:
        p = d["poles"]
        if p == 2:
            rows[17] = rows.get(17, 0) + 1
        elif p == 4:
            rows[18] = rows.get(18, 0) + 1
        else:
            flags.append({"level": "HARD", "item": "dc_disconnect_poles_unmapped",
                          "detail": f"{p}-pole DC disconnect: only 2-pole(r17)/4-pole(r18) defined"})
    return rows, flags


# ---------- RSD device (row 19) ----------

def rsd_device(battery_count, one_line_notes_force=None):
    """
    PE69-3020. = ceil(batteries/3), min 1 if batteries exist. PW3 EXPANSION EXCLUDED from
    battery_count (caller's responsibility). 0 if no batteries unless notes force one.
    """
    if battery_count <= 0:
        qty = 1 if one_line_notes_force else 0
    else:
        qty = max(1, math.ceil(battery_count / 3))
    return ({19: qty} if qty else {}), []


# ---------- Tesla MCI-2 RSD modules (Solar row 20) ----------

def solar_mci(count):
    """Tesla MCI-2 (RSD) count -> Solar row 20 (TSL-MCI-GEN2 / 1879359). The count is READ from the
    planset (PV-3 BOM = truth, PV-1 SCOPE = cross-check, reconciled in the extractor) — NEVER derived
    from module count. 0/None -> no line."""
    c = int(count or 0)
    return ({20: c} if c > 0 else {}), []


# ---------- Ground bar (row 22) — GATEWAY ONLY ----------

def ground_bar(gateway_count):
    """22 PK23GTACP = 1 PER Tesla Energy Gateway. No gateway -> NO ground bar.
    (Backup-switch / non-gateway jobs do not get this line.)"""
    return ({22: gateway_count} if gateway_count else {}), []


# ---------- Supply-side taps (rows 26-28) — flat qty 3 if present ----------

def supply_side_taps(one_line_text):
    """
    Substring match anywhere in the one-line. Each present SKU -> flat qty 3.
      'K4977' -> row 26 (Milbank dual slide-in lugs)
      'IT-3/0' or 'IT3/0' or 'NSI IT-3/0' -> row 27
      'IT-250' -> row 28
    These are the supply-side interconnection lugs for line-side connections
    (Tesla Backup Switch / meter collar).
    """
    rows: dict[int, int] = {}
    t = one_line_text.upper()
    if "K4977" in t:
        rows[26] = 3
    if re.search(r"IT[\-\s]?3/0", t):
        rows[27] = 3
    if re.search(r"IT[\-\s]?250", t):
        rows[28] = 3
    return rows, []


# ---------- Tesla PW3 / Gateway / Backup switch (rows 51-57) ----------

def tesla_core(pw3_skus, gateway_count=0, backup_switch=False,
               inverter_sku=None, remote_meter_count=0):
    """
    pw3_skus: list of the 1707000 SKU strings drawn (one per PW3 unit). Precedence per unit:
        exact 1707000-60-M-LR-2025 -> row 51; any other 1707000 -> row 52 (Domestic default);
        1707000-11 (non-domestic) -> row 53 NOT filled, WARN.
    Returns ({row: qty}, flags).
    """
    rows: dict[int, int] = {}
    flags: list = []
    for sku in pw3_skus:
        s = sku.upper()
        if "1707000-60-M-LR-2025" in s:
            rows[51] = rows.get(51, 0) + 1
        elif "1707000-11" in s:
            flags.append({"level": "WARN", "item": "pw3_non_domestic",
                          "detail": f"{sku}: non-domestic PW3 (row 53) — confirm before ordering"})
        else:
            rows[52] = rows.get(52, 0) + 1  # Domestic default
    if gateway_count:
        rows[54] = gateway_count
    if inverter_sku:
        rows[55] = 1
    if remote_meter_count:
        rows[56] = remote_meter_count
    if backup_switch:
        rows[57] = 1
    if gateway_count and backup_switch:
        flags.append({"level": "WARN", "item": "gateway_and_backup_switch",
                      "detail": "Gateway and Backup Switch normally never coexist — verify."})
    return rows, flags


# ---------- Tesla Expansion block (rows 59-65) ----------

def master_notes_from_coperniq(project=None, form=None):
    """
    Extract the master-note text for mount resolution. The master note is a FORM on the project
    (name "Master Note"), NOT the project's flat custom.design_notes field (which is usually empty).
    The pipeline must fetch it with the Coperniq MCP:
        forms = Coperniq:list_project_forms(project_id)         # find the one named "Master Note"
        form  = Coperniq:get_form(form_id)                      # pass that dict here as `form=`
    This function walks the form's field layout and returns a dict of the relevant plain-text
    sections: {design_notes, additional_notes} (the two TEXT fields that carry mount/stack wording).

    Back-compat: if a `project` dict is passed and its custom block carries the note fields, those
    are merged in too (covers any project where the note lives in custom).
    Returns a dict ready for resolve_expansion_mount(master_notes=).
    """
    import re as _re

    def _clean(s):
        s = _re.sub(r"<[^>]+>", " ", str(s or ""))
        s = s.replace("&nbsp;", " ").replace("&amp;", "&")
        return _re.sub(r"\s+", " ", s).strip()

    out = {"design_notes": "", "additional_notes": "",
           "installation_notes": "", "field_installation_notes": ""}

    # 1) FORM (authoritative) — walk formLayouts -> fields, match by field name
    if form and isinstance(form, dict):
        groups = form.get("formLayouts", []) or []
        for g in groups:
            for fld in (g.get("fields", []) or g.get("properties", []) or []):
                name = str(fld.get("name", "")).lower().rstrip(":").strip()
                val = fld.get("value")
                if isinstance(val, (list, dict)):
                    continue  # mount wording lives in TEXT fields only
                if name in ("design notes", "design note"):
                    out["design_notes"] = _clean(val)
                elif name in ("additional notes", "additional note"):
                    out["additional_notes"] = _clean(val)
                elif name in ("installation notes", "field installation notes"):
                    out["installation_notes"] = _clean(val)

    # 2) project custom block (fallback / merge)
    if project and isinstance(project, dict):
        custom = project.get("custom", {}) or {}
        for key in ("design_notes", "installation_notes", "field_installation_notes"):
            if not out.get(key):
                out[key] = _clean(custom.get(key))

    return out


def _note_says_stack(t: str) -> bool:
    """Stack kit only on an EXPLICIT call-out: the stack kit P/N 1978070, or the words 'stack kit' /
    'stacked' / 'stack mount'. A bare 'stack' substring elsewhere in the note (user, Fickas #857566)
    must not flip the default WALL kit."""
    t = (t or "").lower()
    return ("1978070" in t.replace(" ", "") or "stack kit" in t or "stacked" in t
            or "stack mount" in t or "stack-mount" in t)


def resolve_expansion_mount(plan_mount=None, master_notes=None):
    """
    Determine expansion mount kit (stack vs wall) by PRECEDENCE (user, Lackey):
      1. PLANS FIRST (truth): if the planset description near the expansion says "stack"/"stacked"
         or "wall mount" -> use that. plan_mount: "stack" | "wall" | None.
      2. MASTER NOTE (Coperniq): if the plans say neither, check the master-note form sections —
         Coperniq project custom fields `design_notes`, `installation_notes`,
         `field_installation_notes` (passed as master_notes dict or one concatenated string). If any
         mentions "stack"/"stack kit" -> stack; else if "wall mount"/"wall-mount" -> wall.
      3. DEFAULT: neither plans nor master note mention it -> WALL MOUNT.
    Returns "stack" or "wall".
    """
    # 1) plans (truth)
    if plan_mount:
        pm = str(plan_mount).lower()
        if "stack" in pm:
            return "stack"
        if "wall" in pm:
            return "wall"
    # 2) master note (Coperniq Master Note FORM: design_notes / additional_notes / install notes)
    if master_notes:
        if isinstance(master_notes, dict):
            text = " ".join(str(master_notes.get(k, "") or "") for k in
                            ("design_notes", "additional_notes", "installation_notes",
                             "field_installation_notes"))
        else:
            text = str(master_notes)
        t = text.lower()
        if _note_says_stack(t):
            return "stack"
        if "wall mount" in t or "wall-mount" in t or "wallmount" in t:
            return "wall"
    # 3) default
    return "wall"


# ---------- Solar module (Solar BOM rows 5-9) — user, Carmen Meyer #877571 ----------
#
# The module model on the planset (PV-1/PV-3/PV-5/PV-7) is matched by EXACT SKU against the Solar
# template's module rows 5-9. If the plan's module is NOT in the table, the engine ALWAYS FLAGS it
# (never silent, never a blind substitution). A flagged module either (a) maps via a CONFIRMED
# substitution the user has authorized, in which case it is placed on the substitute row WITH a NOTE
# recording the swap, or (b) has no confirmed substitute -> HARD hold (a human must add the SKU).
# Tables transcribed verbatim from BOM_TEMPLATE.xlsx Solar BOM rows 5-9.
_MODULE_SKU_ROW = {
    "ELNSM54M-HC-N-450":          5,   # 450W Sirius
    "Q.PEAK DUO BLK ML-G10+ 410W": 6,  # 410W QCELL
    "HIS-T440NF(BK)":             7,   # 440W Hyundai
    "DNA-120-BF10-460W":          8,   # 460W Aptos
    "LR5-54HPB-415M":             9,   # 415W LonGi
    "HIN-T440NF(BK)":             7,   # 440W Hyundai — the ONLY Hyundai 440 SKU Tron sources (user, 2026-10-16)
    "Q.TRON BLK M-G2.C+ 435W":   10,   # 435W Qcells Q.TRON (user, 2026-10-16) — template row 10 is blank
}
# Row SKU/description stamped by the engine (B, C). Row 7: plans may say HiS-T440NF(BK) or HiN-T440NF(BK);
# Tron only sources HiN, so the delivered line always reads HiN. Row 10 is blank in the template.
_MODULE_ROW_STAMP = {
    7:  ("HiN-T440NF(BK)", "440W HYUNDAI MODULES"),
    10: ("Q.TRON BLK M-G2.C+ 435W", "QCELLS 435W MODULES"),
}
# CONFIRMED substitutions (user-authorized): plan SKU -> (template row, substitute SKU, reason).
# Carmen Meyer #877571: the Hyundai HiS-T430NF(BK) 430W is flipped to the 440W version of the SAME
# module SKU (row 7). User confirmed this substitution explicitly. Add future confirmed swaps here.
_MODULE_CONFIRMED_SUB = {
    "HIS-T430NF(BK)": (7, "HiN-T440NF(BK)", "430W flipped to the 440W HiN-T440NF(BK) (user-confirmed)"),
}


def _norm_module_sku(s) -> str:
    """Normalize a module SKU for matching: uppercase, drop a TRAILING wattage token (handles every
    form the table uses — ' 450W', '-450', '-460W', '410W'), then remove all spaces and hyphens.

    This makes a format-only difference like the planset's 'ELNSM54M-HC-N 450W' compare equal to the
    table key 'ELNSM54M-HC-N-450', turning a false HARD hold into a clean match. Only a *trailing*
    digit run is treated as wattage, so embedded model digits (T440NF, BF10, G10) stay intact and
    genuinely different modules don't collide. Real cross-model wattage swaps (e.g. 430->440, where
    the digits are mid-token) still miss the table and fall through to the confirmed-substitution path.
    """
    s = str(s or "").upper().strip()
    s = re.sub(r"[\s_\-]*\d{2,4}\s*W?\s*$", "", s)   # trailing wattage token (W optional)
    s = re.sub(r"[\s\-]+", "", s)                     # remaining spaces / hyphens
    return s


def _module_row(key):
    """Exact normalized match, else the planset string CONTAINS a table SKU (e.g. 'QCELLS Q.TRON BLK
    M-G2.C+ 435W' or 'HYUNDAI HIN-T440NF(BK)'). Only SKUs >= 8 normalized chars are used for containment so
    a short token can't match by accident; the longest contained SKU wins."""
    table = {_norm_module_sku(k): v for k, v in _MODULE_SKU_ROW.items()}
    if key in table:
        return table[key]
    hits = [(len(k), v) for k, v in table.items() if len(k) >= 8 and k in key]
    return max(hits)[1] if hits else None


def module_note_crosscheck(module_pn, note_text):
    """Master Note vs plan module (user, Q.TRON rule: 'if it matches the plans and master note'). Returns a
    flag list: HARD if the note names a DIFFERENT template module than the plan; [] if it agrees or is
    silent."""
    if not note_text or not module_pn:
        return []
    plan_row = _module_row(_norm_module_sku(module_pn))
    T = re.sub(r"[\s\-]+", "", str(note_text).upper())
    note_rows = {v for k, v in _MODULE_SKU_ROW.items() if _norm_module_sku(k) in T}
    if note_rows and plan_row is not None and plan_row not in note_rows:
        # plan is the FIRST source of truth (user, 2026-10-16) -> review note only, never a hold
        return [{"level": "NOTE", "item": "module_plan_vs_master_note",
                 "detail": f"Plan module {module_pn!r} (row {plan_row}) differs from the Master Note "
                           f"module (row(s) {sorted(note_rows)}). Confirm which module is being installed."}]
    return []


def solar_module(module_pn, module_count, note_text=""):
    """Solar module line (Solar BOM rows 5-10). Returns (rows, flags, special).

    module_pn    : EXACT module model from the planset (e.g. "HiS-T440NF(BK)").
    module_count : module quantity (from the PV-3 BOM table / count).
    note_text    : Master Note text, cross-checked against the plan module.

    Exact (or contained) table match -> {row: count}; rows 7 and 10 also stamp their SKU/description
    (row 7 always HiN-T440NF(BK); row 10 Q.TRON). Confirmed substitution -> {sub_row: count} + NOTE.
    Unmapped with no confirmed sub -> HARD flag, no row placed. NEVER a blind family/wattage guess.
    """
    rows: dict[int, int] = {}
    flags: list = []
    special: dict = {}
    if not module_pn:
        flags.append({"level": "HARD", "item": "module_pn_missing",
                      "detail": "No module model read from the planset. Transcribe the exact module "
                                "SKU from PV-3 / PV-7; do not guess."})
        return rows, flags, special

    key = _norm_module_sku(module_pn)
    row = _module_row(key)
    if row is None:
        subs = {_norm_module_sku(k): v for k, v in _MODULE_CONFIRMED_SUB.items()}
        if key in subs:
            sub_row, sub_sku, reason = subs[key]
            row = sub_row
            flags.append({"level": "NOTE", "item": "module_substitution",
                          "msg": (f"Planset module {module_pn!r} is not a template SKU; placed on row "
                                  f"{sub_row} ({sub_sku}) per confirmed substitution: {reason}.")})
    if row is None:
        flags.append({"level": "HARD", "item": "module_pn_unmapped",
                      "detail": (f"Planset module {module_pn!r} is not in the module table (rows 5-10) and "
                                 f"has no confirmed substitution. HOLD — a human must add the SKU/row or "
                                 f"authorize a substitution. NOT placed on a close-wattage row.")})
        return rows, flags, special

    rows[row] = int(module_count)
    if row in _MODULE_ROW_STAMP:
        special[row] = _MODULE_ROW_STAMP[row]
    if row == 7 and "HIS" in key and not any(f.get("item") == "module_substitution" for f in flags):
        flags.append({"level": "NOTE", "item": "module_his_to_hin",
                      "msg": f"Plan module {module_pn!r} delivered as HiN-T440NF(BK) (the only Hyundai 440 SKU sourced)."})
    flags += module_note_crosscheck(module_pn, note_text)
    return rows, flags, special


# ---------- Meter socket / base (Electrical rows 81-96) ----------
#
# RULE (user, Nelson REVA #860742): a meter line is ordered ONLY when the planset draws a NEW
# meter / socket / base (PV-1 scope + PV-5 one-line: e.g. "UPGRADE METER BASE TO NEW MILBANK
# U9551-RXL-QG-5T9-AMS"). An EXISTING meter -> NO meter line. The SKU is matched by EXACT part
# number against the template's meter rows 81-96 — NEVER inferred or guessed. If the planset's
# meter P/N is not in this table, emit a HARD flag and route to the Special Order row (96); do not
# substitute a "close" SKU. This table is transcribed verbatim from BOM_TEMPLATE.xlsx Electrical BOM
# col B (rows 81-96) so the engine resolves the row headlessly, with no Excel read at runtime.
_METER_SKU_ROW = {
    "U8949-RL-TG-KK-CECHA":   81,   # ComEd METER BASE MILBANK
    "U8436-O-CECHA":          82,   # ComEd Meter
    "NU8980-O-200-KK-CECHA":  83,   # COMED METER PEDESTAL MILBANK
    "U5168-XTL-100-KK-CECHA": 84,   # COMED Meter Main 100A
    "U5168-XTL-200-KK-CECHA": 85,   # COMED Meter Main 200A
    "U1773-XL-TG-KK":         87,   # WE Single Position Meter Socket 200A
    "U9551-RXL-QG-5T9-AMS":   89,   # Ameren OH lever bypass (no service disconnect)  <- Nelson
    "S40405-02QG":            90,   # Ameren OH lever bypass (Siemens sub for U9551)
    "U6281-XL-100-5T6-AMS":   91,   # Ameren Meter/Main 100A
    "U6281-XL-100-5T9":       92,   # Ameren Meter/Main 100A
    "U6281-XL-200-5T6-AMS":   93,   # Ameren meter/main 200A
    "U6281-XL-200-5T9":       94,   # Ameren meter/main 200A
}
_METER_SPECIAL_ORDER_ROW = 96      # "Special Order Meter: (Part Number)"


def meter_socket(new_meter_drawn, meter_pn=None):
    """Meter line (Electrical rows 81-96). Returns (rows, flags, special_order).

    new_meter_drawn : bool — True ONLY if the planset draws/specifies a NEW meter/socket/base
                      (PV-1 scope line "UPGRADE METER BASE TO NEW ..." or a NEW meter on PV-5).
                      Existing meter -> pass False -> NO meter line (empty rows).
    meter_pn        : the EXACT meter part number transcribed from the planset (e.g.
                      "U9551-RXL-QG-5T9-AMS"). Matched verbatim (case-insensitive, hyphen/space
                      normalized) against _METER_SKU_ROW. NEVER guessed.

    Match -> rows {row: 1}. No match -> Special Order row 96 {96:1} AND special_order {96: <pn>} so
    the writer stamps the ACTUAL meter P/N into the special-order line (user, Meyer #877571), plus a
    HARD flag. No P/N but new_meter_drawn True -> HARD flag (read the P/N off the planset).

    special_order: {row: "<verbatim P/N>"} — the writer overwrites that row's SKU/description cell
    with this text so the special-order line names the exact part to procure.
    """
    rows: dict[int, int] = {}
    flags: list = []
    special_order: dict[int, str] = {}
    if not new_meter_drawn:
        return rows, flags, special_order   # existing meter -> no line

    if not meter_pn:
        flags.append({"level": "HARD", "item": "meter_pn_missing",
                      "detail": "Planset draws a NEW meter but no meter P/N was read. Transcribe the "
                                "exact meter SKU from PV-1 scope / PV-5 one-line; do not guess."})
        return rows, flags, special_order

    # normalize: uppercase, strip surrounding space, collapse internal whitespace. Keep hyphens.
    key = " ".join(str(meter_pn).upper().split()).strip()
    norm_table = {" ".join(k.upper().split()).strip(): v for k, v in _METER_SKU_ROW.items()}
    row = norm_table.get(key)
    # U6281 family ONLY: the plan writes the -5T9- token but the catalog SKU is -5T6- (e.g.
    # U6281-XL-200-5T9-AMS -> U6281-XL-200-5T6-AMS). If the verbatim P/N didn't match, retry with
    # 5T9->5T6 so this resolves to the real meter-table row instead of Special Order. Applied only
    # after a direct miss, so genuine -5T9 table entries (rows 92/94) keep matching unchanged.
    if row is None and key.startswith("U6281") and "5T9" in key:
        alt = key.replace("5T9", "5T6")
        alt_row = norm_table.get(alt)
        if alt_row is not None:
            row = alt_row
            flags.append({"level": "NOTE", "item": "meter_pn_normalized",
                          "detail": f"U6281 meter P/N {str(meter_pn).strip()!r} normalized -5T9- -> "
                                    f"-5T6- (catalog SKU) to match meter row {row}."})
    # U9551 family (user, Wells #873390 / Bolin #858290 / Alderson #953390 / Wyer #915365): plansets
    # often omit the -5T9- token ("U9551-RXL-QG-AMS"). Tron's ONLY U9551 SKU is the Ameren lever-bypass
    # U9551-RXL-QG-5T9-AMS (row 89), so a U9551 P/N that misses verbatim resolves to row 89 by FAMILY,
    # never to Special Order. Applied only after a direct miss.
    if row is None and key.replace(" ", "").startswith("U9551"):
        row = norm_table.get("U9551-RXL-QG-5T9-AMS")
        flags.append({"level": "NOTE", "item": "meter_pn_normalized",
                      "detail": f"U9551 meter P/N {str(meter_pn).strip()!r} resolved to the Ameren "
                                f"lever-bypass U9551-RXL-QG-5T9-AMS (row {row}); plan omitted -5T9-."})
    if row is None:
        pn = str(meter_pn).strip()
        rows[_METER_SPECIAL_ORDER_ROW] = 1
        special_order[_METER_SPECIAL_ORDER_ROW] = f"Special Order Meter: {pn}"
        flags.append({"level": "HARD", "item": "meter_pn_unmapped",
                      "detail": f"Meter P/N {pn!r} not in the meter table (rows 81-95). Routed to "
                                f"Special Order row {_METER_SPECIAL_ORDER_ROW} with the P/N stamped "
                                f"into the line; a human must confirm/add the SKU. NOT substituted."})
    else:
        rows[row] = 1
    return rows, flags, special_order


# ---------- Main Service Panel (Electrical rows 73-79) — user, Meyer #877571 ----------
#
# A NEW MSP is ordered only when the planset specifies one (PV-1 scope / PV-5 one-line shows a NEW
# panel; an EXISTING MSP that "can remain" -> NO MSP line). Matched by EXACT P/N against the
# template's MSP rows 74-78 (Square D Homeline family) — NEVER inferred. Row 73 (HOM48L125GRB sub-
# panel/load center) is included for exact match but is a load center, not a service main. Unmapped
# P/N -> Special Order MSP row 79 with the ACTUAL SKU stamped into the line + HARD flag. Table is
# transcribed verbatim from BOM_TEMPLATE.xlsx so the engine resolves headlessly.
_MSP_SKU_ROW = {
    "HOM48L125GRB":     73,   # Square D LC MLO 4 SPACE 125A (load center)
    "HOM2040M100PC":    74,   # Square D 100A Homeline MSP 20/40
    "HOM3060M100PC":    75,   # Square D 100A Homeline MSP 30/60
    "HOM3060M200PC":    76,   # Square D 200A Homeline MSP 30/60
    "HOM4080M200PC":    77,   # Square D 200A Homeline MSP 40/80
    "HOM816M200PFTRB":  78,   # SQ-D Homeline 200A Main Breaker Panel w/Feed-Thru Lug
}
_MSP_SPECIAL_ORDER_ROW = 79   # "Special Order MSP: (enter sku)"


def main_service_panel(new_msp_drawn, msp_pn=None):
    """MSP line (Electrical rows 73-79). Returns (rows, flags, special_order).

    new_msp_drawn : bool — True ONLY if the planset specifies a NEW MSP (PV-1 scope / PV-5 one-line).
                    An existing MSP that "can remain" -> pass False -> NO MSP line.
    msp_pn        : EXACT MSP part number from the planset. Matched verbatim against _MSP_SKU_ROW.
                    NEVER guessed.

    Match -> {row: 1}. Unmapped -> Special Order MSP row 79 {79:1} + special_order {79: <pn>}
    (writer stamps the SKU into the line) + HARD flag. New MSP but no P/N -> HARD flag.
    """
    rows: dict[int, int] = {}
    flags: list = []
    special_order: dict[int, str] = {}
    if not new_msp_drawn:
        return rows, flags, special_order

    if not msp_pn:
        flags.append({"level": "HARD", "item": "msp_pn_missing",
                      "detail": "Planset specifies a NEW MSP but no MSP P/N was read. Transcribe the "
                                "exact panel SKU from PV-1 scope / PV-5 one-line; do not guess."})
        return rows, flags, special_order

    key = " ".join(str(msp_pn).upper().split()).strip()
    norm_table = {" ".join(k.upper().split()).strip(): v for k, v in _MSP_SKU_ROW.items()}
    row = norm_table.get(key)
    if row is None:
        pn = str(msp_pn).strip()
        rows[_MSP_SPECIAL_ORDER_ROW] = 1
        special_order[_MSP_SPECIAL_ORDER_ROW] = f"Special Order MSP: {pn}"
        flags.append({"level": "HARD", "item": "msp_pn_unmapped",
                      "detail": f"MSP P/N {pn!r} not in the MSP table (rows 73-78). Routed to Special "
                                f"Order MSP row {_MSP_SPECIAL_ORDER_ROW} with the P/N stamped into the "
                                f"line; a human must confirm/add the SKU. NOT substituted."})
    else:
        rows[row] = 1
    return rows, flags, special_order


# ---------- Plan-vs-Master-Note cross-check (user, Nelson REVA) ----------
#
# RULE: the PLANSET is authoritative for equipment values. The Master Note is a CROSS-CHECK ONLY.
# When the note disagrees with the plan, DELIVER THE PLAN VALUE and emit a confidence-report NOTE
# (never a HARD hold, never an override). This is the inverse of the mount-resolution precedence:
# mount is not drawn as a discrete value on most plans, so the note legitimately fills it; but any
# value the plan DOES state (harness P/N, breakers, meter SKU, module/battery counts) wins.
_MSP_LINE = re.compile(r"\b(MSP|MAIN SERVICE PANEL|MAIN PANEL|LOAD CENTER|LOADCENTER|MAIN BREAKER PANEL|MPU)\b")
_MODEL = re.compile(r"\b([A-Z]{2,5}\d{2,}[A-Z0-9]*(?:-[A-Z0-9]+)*)\b")


def msp_pn_from_text(text):
    """NEW MSP model number from Master Note / planset text (user, Douglas #967510 / Hundley #878377 /
    Wade #955324 / C. Murphy #894545). Returns (pn, line) or (None, None).
      - a known MSP SKU (rows 74-78) anywhere in the text wins;
      - else a model-number token on a line that names the MSP AND says it is NEW / an UPGRADE /
        REPLACE / INSTALL; lines saying it 'can remain' never count."""
    if not text:
        return None, None
    T = str(text).upper()
    for sku in _MSP_SKU_ROW:
        if sku in T.replace(" ", ""):
            return sku, sku
    for raw in T.splitlines():
        L = " ".join(raw.split())
        if not _MSP_LINE.search(L):
            continue
        if "REMAIN" in L or ("EXISTING" in L and not any(w in L for w in ("UPGRADE", "REPLACE"))):
            continue
        if not any(w in L for w in ("NEW", "UPGRADE", "REPLACE", "INSTALL", "(N)")):
            continue
        for m in _MODEL.finditer(L):
            tok = m.group(1)
            if tok.startswith(("AWG", "THHN", "REV")):
                continue
            return tok, L
    return None, None


def main_service_panel_resolved(plan_new, plan_pn, note_text, plan_text=""):
    """MSP precedence (user, 2026-10-16): if EITHER the Master Note or the plans call out a new MSP, it
    goes on the BOM; if both give a SKU and they DIFFER, the MASTER NOTE wins (NOTE flag)."""
    note_pn, _ = msp_pn_from_text(note_text)
    text_pn, _ = msp_pn_from_text(plan_text)
    plan_pn = plan_pn or text_pn
    flags = []
    if note_pn and plan_pn and note_pn.replace("-", "") != str(plan_pn).upper().replace("-", ""):
        flags.append({"level": "NOTE", "item": "msp_note_vs_plan",
                      "detail": f"MSP SKU: Master Note {note_pn!r} vs plan {plan_pn!r} — Master Note wins."})
    pn = note_pn or plan_pn
    new_txt, amp_txt = msp_new_from_text(note_text)
    new_pl, amp_pl = msp_new_from_text(plan_text)
    new = bool(note_pn) or bool(plan_new) or bool(text_pn) or new_txt or new_pl
    if new and not pn:
        # NEW MSP / MPU called out with no model number (user, Starr #865577): default Homeline 200A
        # row 76 (HOM3060M200PC); a stated 100A -> row 74.
        amp = amp_txt or amp_pl or 200
        row = 74 if amp == 100 else 76
        return {row: 1}, flags + [{"level": "NOTE", "item": "msp_default",
                                   "detail": f"New MSP/MPU called out without a SKU ({amp}A) -> default row {row}."}], {}
    rows, f2, so = main_service_panel(new, pn)
    return rows, flags + f2, so


_PANEL = r"(?:MSP|MAIN SERVICE PANEL|MAIN PANEL|MAIN BREAKER PANEL|SERVICE PANEL|LOAD ?CENTER|PANEL)"
_NEW_MSP = re.compile(
    r"\b(?:\(N\)|NEW|UPGRADE(?:D)?(?:\s+TO)?|REPLACE(?:D)?(?:\s+WITH)?|INSTALL(?:ED)?)\b"
    r"(?:\s+(?!EGC|GROUND|GEC|BONDING|BREAKER|METER|WIRE|CONDUCTOR)[\w/.-]+){0,4}?\s+" + _PANEL + r"\b")
_MPU = re.compile(r"\b(?:MPU|MAIN PANEL UPGRADE|MSP UPGRADE|PANEL UPGRADE|UPGRADE (?:THE )?MSP)\b")


def msp_new_from_text(text):
    """(is_new, amps) for a NEW MSP / main panel upgrade stated in text without a model number.
    The NEW/UPGRADE word must apply to the PANEL itself ('upgrade to new 200A MSP', 'MPU', '(N) MAIN
    SERVICE PANEL'). A line that merely mentions the MSP and something else new does NOT count
    (user, Wyer #915365: 'MSP: Separate N & G, new 4 AWG EGC' was read as a new MSP -> row 76)."""
    for raw in str(text or "").upper().splitlines():
        L = " ".join(raw.split())
        if "REMAIN" in L or "(E)" in L or ("BACK" in L and "UP LOAD" in L):
            continue
        if _MPU.search(L) or _NEW_MSP.search(L):
            m = re.search(r"\b(100|125|150|200|225)\s*A\b", L)
            return True, (int(m.group(1)) if m else None)
    return False, None


# ---------- POI / landing breaker by PANEL MANUFACTURER (user, 2026-10-16) ----------
# The interconnection (POI) breaker's SKU depends on the panel it lands in:
#   Eaton panel           -> BR2xx  (rows 101-110)
#   Homeline / Square D   -> HOM2xx (rows 119-128)
#   Milbank meter/main    -> Eaton BR2xx (Milbank meter-mains take BR breakers)
#   unknown manufacturer  -> row 115 (blank template row), HIGHLIGHTED: find the exact SKU.
_BR2_ROW = {15: 101, 20: 102, 30: 103, 40: 104, 50: 105, 60: 106, 70: 107, 80: 108, 100: 109, 125: 110}
_HOM2_ROW = {15: 119, 20: 120, 30: 121, 40: 122, 50: 123, 60: 124, 70: 125, 80: 126, 100: 127, 125: 128}
POI_UNKNOWN_ROW = 115


def _mfr_in(t: str):
    t = (t or "").upper()
    if "MILBANK" in t and ("METER/MAIN" in t or "METER MAIN" in t or "U6281" in t or "U5168" in t):
        return "eaton"
    if "HOMELINE" in t or "SQUARE D" in t or "SQ-D" in t or "SQ D" in t or re.search(r"\bHOM\d", t):
        return "homeline"
    if "EATON" in t or "CUTLER" in t or re.search(r"\bBR\d{3}", t) or re.search(r"\bBR\d{2}\d*B\d", t):
        return "eaton"
    return None


def poi_panel_mfr(note_text="", one_line_text="", msp_pn=None, meter_pn=None):
    """Manufacturer of the panel the POI breaker lands in. Precedence: Master Note POI/breaker lines
    (the note may name the breaker type outright) -> the NEW MSP SKU -> a NEW Milbank meter/main ->
    one-line text lines that name the MSP/panel. Returns 'eaton' | 'homeline' | None."""
    for raw in str(note_text or "").upper().splitlines():
        if any(k in raw for k in ("POI", "BREAKER", "BACKFEED", "INTERCONNECT", "LAND")):
            m = _mfr_in(raw)
            if m:
                return m
    if msp_pn:
        m = _mfr_in(str(msp_pn))
        if m:
            return m
    if meter_pn and str(meter_pn).upper().startswith(("U6281", "U5168")):
        return "eaton"
    for raw in str(one_line_text or "").upper().splitlines():
        if any(k in raw for k in ("MSP", "MAIN SERVICE PANEL", "PANEL", "LOAD CENTER", "METER/MAIN",
                                  "METER MAIN")):
            m = _mfr_in(raw)
            if m:
                return m
    return None


def poi_breaker(amp, mfr, qty=1, label="POI breaker"):
    """({row: qty}, flags, special) for a 2-pole POI/landing breaker of `amp` in a `mfr` panel."""
    table = _BR2_ROW if mfr == "eaton" else _HOM2_ROW if mfr == "homeline" else None
    row = table.get(int(amp)) if table else None
    if row:
        return {row: qty}, [{"level": "NOTE", "item": "poi_breaker",
                             "detail": f"{label}: {qty}x {amp}A/2P in a {mfr} panel -> row {row}."}], {}
    txt = f"{amp}A 2P BREAKER — FIND EXACT SKU (panel manufacturer not identified)"
    return ({POI_UNKNOWN_ROW: qty},
            [{"level": "HARD", "item": "poi_breaker_mfr_unknown",
              "detail": f"{label}: {amp}A/2P but the panel manufacturer (Eaton/Homeline/Milbank) wasn't "
                        f"found on the plans or Master Note. Placed on HIGHLIGHTED row {POI_UNKNOWN_ROW}; "
                        f"find the exact SKU."}],
            {POI_UNKNOWN_ROW: (txt, txt)})


_POI_AMP = re.compile(r"\b(\d{2,3})\s*A\s*(?:/|-)?\s*2\s*P\b")


def pv_poi_amp_from_text(one_line_text="", note_text=""):
    """Rated PV interconnection breaker from text ('(N) 40A/2P PV BREAKER', 'POI: 40A 2P backfeed').
    Returns int amps or None."""
    for src in (note_text, one_line_text):
        for raw in str(src or "").upper().splitlines():
            if "(E)" in raw or "EXISTING" in raw or "MAIN BREAKER" in raw:
                continue
            if any(k in raw for k in ("POI", "PV", "SOLAR", "BACKFEED", "INTERCONNECT", "BACK-FEED")):
                m = _POI_AMP.search(raw)
                if m and int(m.group(1)) <= 125:
                    return int(m.group(1))
    return None


def line_side_interconnection(one_line_text="", note_text=""):
    t = (str(one_line_text) + " " + str(note_text)).upper()
    return any(k in t for k in ("K4977", "LINE SIDE TAP", "LINE-SIDE TAP", "SUPPLY SIDE", "SUPPLY-SIDE"))


def backed_up_loads_panel(one_line_text="", note_text=""):
    """NEW backed-up loads panel (user, Calhoon #848183): 100A -> row 74 (HOM2040M100PC)."""
    t = " ".join((str(one_line_text) + "\n" + str(note_text)).upper().split())
    m = re.search(r"((?:\(N\)|NEW)[^.\n]{0,40}?)?(\d{3})\s*A[^.]{0,40}?BACK(?:ED)?[ -]?UP LOADS? (?:PANEL|CENTER)", t) \
        or re.search(r"BACK(?:ED)?[ -]?UP LOADS? (?:PANEL|CENTER)[^.]{0,40}?(\d{3})\s*A", t)
    if not m:
        return {}, []
    amp = int([g for g in m.groups() if g and g.isdigit()][-1])
    if amp == 100:
        return {74: 1}, [{"level": "NOTE", "item": "backed_up_loads_panel",
                          "detail": "New 100A backed-up loads panel -> row 74 (HOM2040M100PC)."}]
    return {}, [{"level": "HARD", "item": "backed_up_loads_panel_unmapped",
                 "detail": f"New {amp}A backed-up loads panel called out; only 100A (row 74) is mapped. Add it."}]


def new_meter_main(meter_pn=None, one_line_text=""):
    pn = str(meter_pn or "").upper()
    if pn.startswith(("U6281", "U5168")):
        return True
    t = str(one_line_text or "").upper()
    return "(N)" in t and ("METER/MAIN" in t or "METER MAIN" in t)


def plan_vs_note(item, plan_value, note_value):
    """Deliver plan_value; if note_value is present and differs, return a NOTE flag. Returns
    (delivered_value, flag_or_None). note_value None/empty -> no check (note silent on this item)."""
    if note_value is None or note_value == "":
        return plan_value, None
    same = str(plan_value).strip().upper() == str(note_value).strip().upper()
    if same:
        return plan_value, None
    return plan_value, {
        "level": "NOTE", "item": f"{item}_plan_note_conflict",
        "msg": (f"{item}: planset='{plan_value}' but Master Note says '{note_value}'. "
                f"Delivering PLAN value (plans are authoritative). Master Note is a check only.")}


# ---------- Master-note resolution (MANDATORY, runs every project) ----------


def resolve_master_note(project_id, mcp_list_project_forms, mcp_get_form):
    """MANDATORY master-note fetch — hardcoded into the pipeline so the engine ALWAYS checks the
    Coperniq "Master Note" form, with NO human/Claude step (user, Nelson REVA).

    The two callables are the Coperniq MCP tools, injected by the runner so this stays headless:
        mcp_list_project_forms(project_id) -> list of {id/uuid, name, ...}
        mcp_get_form(form_id)              -> the form dict (formLayouts -> fields)

    Steps (all hardcoded, no inference):
      1. list_project_forms(project_id)
      2. find the form whose name == "Master Note" (case-insensitive, trimmed)
      3. get_form(its id) and run master_notes_from_coperniq(form=...)
    Returns (master_notes_dict, flags). On any miss (no form, fetch error) returns ({}, [HARD flag])
    so the expansion-mount path falls back to its documented default WALL but the report records that
    the master note could not be read — it never silently assumes.
    """
    flags: list = []
    try:
        forms = mcp_list_project_forms(project_id) or []
    except Exception as e:
        return {}, [{"level": "HARD", "item": "master_note_list_failed",
                     "detail": f"list_project_forms({project_id}) raised: {e!r}"}]

    target = None
    for f in forms:
        nm = str((f or {}).get("name", "")).strip().lower()
        if nm == "master note":
            target = f
            break
    if target is None:
        return {}, [{"level": "HARD", "item": "master_note_form_missing",
                     "detail": f"No form named 'Master Note' on project {project_id}. "
                               f"Mount falls back to default WALL; flagged for the report."}]

    form_id = target.get("id") or target.get("uuid") or target.get("formId")
    try:
        form = mcp_get_form(form_id)
    except Exception as e:
        return {}, [{"level": "HARD", "item": "master_note_fetch_failed",
                     "detail": f"get_form({form_id}) raised: {e!r}"}]

    return master_notes_from_coperniq(form=form), flags


def tesla_expansion(unit_count, harness_pn=None, mount=None):
    """
    unit_count: number of 1807000 expansion units (Domestic default -> row 59).

    TWO INDEPENDENT READS (user, Munoz image correction):

    A) HARNESS (rows 63/64/65) — ALWAYS read from the EXPLICIT harness P/N drawn on the plan
       one-line; the harness must MATCH THE PLANS. Do NOT infer it from the mount type or default.
         '-05' -> row 63 (1.64ft - stack);  '-20' -> row 64 (6.56ft - wall);  '-40' -> row 65 (13.12ft - wall)
       qty = unit_count on the matched row. Missing/unrecognized P/N -> HARD flag (read it; never guess).

    B) MOUNT KIT (rows 61 stack / 62 wall-mount) — there is ALWAYS a kit, one per unit. The mount
       value is resolved by resolve_expansion_mount(plan_mount, master_notes) with precedence:
       plans (truth) -> master note (design_notes/installation_notes/field_installation_notes) ->
       DEFAULT WALL. Pass the resolved "stack"/"wall" as `mount`. qty = unit_count on the kit row.

    Returns (rows, flags).
    """
    rows: dict[int, int] = {}
    flags: list = []
    if unit_count <= 0:
        return rows, flags
    rows[59] = unit_count

    # A) harness ALWAYS from explicit P/N (match the plans)
    if harness_pn:
        s = str(harness_pn).upper()
        if "-05" in s:
            hrow = 63
        elif "-20" in s:
            hrow = 64
        elif "-40" in s:
            hrow = 65
        else:
            hrow = None
        if hrow:
            rows[hrow] = unit_count
        else:
            flags.append({"level": "HARD", "item": "expansion_harness_pn_unrecognized",
                          "detail": f"harness P/N {harness_pn!r} has no -05/-20/-40 suffix match"})
    else:
        flags.append({"level": "HARD", "item": "expansion_harness_pn_missing",
                      "detail": "Read the explicit harness P/N (1875157-05/-20/-40) from the plan; "
                                "do not default."})

    # B) mount kit ALWAYS present: default wall-mount unless description says stack
    if mount and "stack" in str(mount).lower():
        rows[61] = unit_count   # stack kit
    else:
        rows[62] = unit_count   # wall-mount kit (default)
    return rows, flags


_HARNESS_ROW = {"-05": 63, "-20": 64, "-40": 65}


def expansion_allocation_from_note(note_text, unit_count):
    """PER-UNIT expansion mount + harness allocation from the Master Note (user, Murphy #852366:
    'First PW3 Expansion installed with stack kit (1978070) ... harness (1875157-05-y)' / 'Second PW3
    Expansion ... wall mount kit (1978069-00-x) and harness (1875157-20-y)' -> 1 stack + 1 wall,
    1x -05 + 1x -20 — NOT 2 stacks + 2x -05).

    Counts explicit kit P/Ns (1978070 stack, 1978069 wall) and harness P/Ns (1875157-05/-20/-40) in the
    note. Returns ({"mount": {61: n, 62: m}, "harness": {63: a, 64: b, 65: c}}) for whichever side
    sums EXACTLY to unit_count, else {} for that side (caller falls back to the single-value path)."""
    out = {}
    if not note_text or unit_count <= 0:
        return out
    t = str(note_text).upper().replace(" ", "")
    stack_n = t.count("1978070")
    wall_n = t.count("1978069")
    if stack_n + wall_n == unit_count and stack_n + wall_n > 0:
        out["mount"] = {k: v for k, v in ((61, stack_n), (62, wall_n)) if v}
    h = {row: t.count(f"1875157{suf}") for suf, row in _HARNESS_ROW.items()}
    if sum(h.values()) == unit_count:
        out["harness"] = {k: v for k, v in h.items() if v}
    return out


def tesla_expansion_resolved(unit_count, plan_harness_pn, project_id,
                             mcp_list_project_forms, mcp_get_form, plan_mount=None):
    """End-to-end expansion resolution. PLANS AUTHORITATIVE, Master Note is a CHECK (user, Nelson).

    The Master Note form is ALWAYS fetched (hardcoded mandatory check), but it never overrides a
    plan-stated value — it only produces a confidence-report NOTE when it disagrees.

      HARNESS (rows 63/64/65): sourced from the PLAN one-line P/N (plan_harness_pn). If the Master
        Note states a different harness P/N, the PLAN wins and a NOTE is emitted (Nelson: plan
        1875157-05 vs note 1875157-20 -> deliver -05/row63, NOTE the conflict).
      MOUNT KIT (rows 61 stack / 62 wall): plan_mount wins if the plan states it; else the Master
        Note fills it; else DEFAULT WALL. If BOTH plan and note state a mount and they differ, plan
        wins + NOTE.

        unit_count             : number of 1807000 expansion units (0 -> nothing)
        plan_harness_pn        : EXACT harness P/N from the PLAN one-line (1875157-05/-20/-40)
        project_id             : Coperniq project id (e.g. 860742)
        mcp_list_project_forms : Coperniq:list_project_forms callable
        mcp_get_form           : Coperniq:get_form callable
        plan_mount             : "stack"/"wall"/None if the PLAN explicitly states the mount

    Returns (rows, flags).
    """
    if unit_count <= 0:
        return {}, []
    flags: list = []
    master_notes, mn_flags = resolve_master_note(project_id, mcp_list_project_forms, mcp_get_form)
    flags += mn_flags
    note_text = ""
    if isinstance(master_notes, dict):
        note_text = " ".join(str(master_notes.get(k, "") or "") for k in
                             ("design_notes", "additional_notes", "installation_notes",
                              "field_installation_notes"))

    # --- HARNESS: plan is authoritative; note is a cross-check ---
    note_harness = None
    for suf in ("-05", "-20", "-40"):
        if f"1875157{suf}" in note_text.upper().replace(" ", ""):
            note_harness = f"1875157{suf}"
            break
    plan_h = None
    if plan_harness_pn:
        s = str(plan_harness_pn).upper()
        for suf in ("-05", "-20", "-40"):
            if suf in s:
                plan_h = f"1875157{suf}"
                break
    if plan_h and note_harness:
        _, hf = plan_vs_note("expansion_harness", plan_h, note_harness)
        if hf:
            flags.append(hf)

    # --- MOUNT: plan wins if stated; else note; else default wall ---
    note_mount = resolve_expansion_mount(plan_mount=None, master_notes=master_notes)  # note/default read
    if plan_mount:
        resolved_mount = "stack" if "stack" in str(plan_mount).lower() else "wall"
        # if note explicitly disagrees with a plan-stated mount, NOTE it (plan wins)
        note_says = None
        t = note_text.lower()
        if _note_says_stack(t):
            note_says = "stack"
        elif "wall mount" in t or "wall-mount" in t or "wallmount" in t:
            note_says = "wall"
        if note_says and note_says != resolved_mount:
            _, mf = plan_vs_note("expansion_mount", resolved_mount, note_says)
            if mf:
                flags.append(mf)
    else:
        resolved_mount = note_mount  # note or default wall

    rows, ef = tesla_expansion(unit_count, harness_pn=plan_harness_pn, mount=resolved_mount)

    # --- PER-UNIT allocation (mixed stack + wall, mixed harness lengths) from the Master Note ---
    # The single-value path above applies ONE mount + ONE harness to every unit. When the note gives
    # an explicit per-unit split that sums to unit_count, deliver that split instead.
    alloc = expansion_allocation_from_note(note_text, unit_count)
    if "mount" in alloc and not plan_mount:
        for r in (61, 62):
            rows.pop(r, None)
        rows.update(alloc["mount"])
    if "harness" in alloc and len(alloc["harness"]) > 1:
        # mixed harness lengths can't be expressed by the single one-line P/N -> note split wins
        for r in (63, 64, 65):
            rows.pop(r, None)
        rows.update(alloc["harness"])
        ef = [f for f in ef if f.get("item") not in ("expansion_harness_pn_missing",
                                                     "expansion_harness_pn_unrecognized")]
        flags.append({"level": "NOTE", "item": "expansion_harness_split",
                      "detail": f"Master Note specifies mixed harness lengths per unit {alloc['harness']}; "
                                f"delivered the split (plan one-line P/N {plan_harness_pn!r})."})
    elif "harness" in alloc and not plan_harness_pn:
        rows.update(alloc["harness"])
        ef = [f for f in ef if f.get("item") != "expansion_harness_pn_missing"]
    return rows, flags + ef


# ---------- Fuses (rows 130-144) ----------

_FRN_ROW = {15: 130, 20: 131, 25: 132, 30: 133, 35: 134, 40: 135, 45: 136,
            50: 137, 60: 138, 70: 139, 80: 140, 90: 141, 100: 142, 125: 143, 200: 144}


# ---------- Homeline interconnection fallback (row 128) ----------

def homeline_interconnection(breaker_rating_2p=None):
    """If a NEW Homeline MSP is the interconnection point and carries a rated, non-(E),
    non-main 2P breaker with no dedicated HOM row (e.g. 125A), use the row-128 fallback.
    breaker_rating_2p: e.g. 125 -> writes B128='HOM2125', desc, qty 1. None -> nothing."""
    if breaker_rating_2p is None:
        return {}, {}, []
    sku = f"HOM2{breaker_rating_2p}"
    desc = f"{breaker_rating_2p}A 2P BREAKER (HOMELINE)"
    return {128: 1}, {128: (sku, desc)}, []


if __name__ == "__main__":
    import json
    # Roland REVA: one fused 60A AC disco; DC = one 4-pole + one 2-pole; 1 PW3 + 1 expansion (stacked,
    # -05 harness explicit on one-line); Tesla Backup Switch; NSI IT-3/0 supply-side tap; NO gateway.
    rows = {}
    fl = []
    for fn, args in [
        ("ac", ([{"amp": 60, "fused": True, "fuse_amp": 60}],)),
        ("dc", ([{"poles": 4}, {"poles": 2}],)),
    ]:
        pass
    acr, acf = ac_disconnects([{"amp": 60, "fused": True, "fuse_amp": 60}])
    dcr, dcf = dc_disconnects([{"poles": 4}, {"poles": 2}])
    rsr, rsf = rsd_device(battery_count=1)          # 1 PW3; expansion excluded
    gbr, gbf = ground_bar(gateway_count=0)          # backup switch -> none
    tpr, tpf = supply_side_taps("... NSI IT-3/0 GEC ... SUPPLY SIDE CONNECTION ...")
    tcr, tcf = tesla_core(pw3_skus=["1707000-XX-Y"], gateway_count=0, backup_switch=True)
    exr, exf = tesla_expansion(unit_count=1, harness_pn="1875157-05-X", mount=None)  # Roland/Munoz: -05 harness, no mount kw
    for d in (acr, dcr, rsr, gbr, tpr, tcr, exr):
        for k, v in d.items():
            rows[k] = rows.get(k, 0) + v
    for f in (acf, dcf, rsf, gbf, tpf, tcf, exf):
        fl += f
    print("ROLAND ELECTRICAL ROWS:", json.dumps(dict(sorted(rows.items())), indent=2))
    print("FLAGS:", json.dumps(fl, indent=2))

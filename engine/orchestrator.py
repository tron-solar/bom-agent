"""Engine orchestrator — the single entry point the trigger service calls.

    build_bom(planset_pdf_path, coperniq_project_dict) -> (xlsx_bytes, confidence_dict)

This is the runtime path (app/pipeline.py -> run_engine -> build_bom). It wires the v2 engine:

    extractor.PlansetExtractor().extract(pdf, project, master_note_form)   # Claude Vision
        -> consolidated block build (electrical_engine blocks; racking when feedable)
        -> bom_writer.write_bom(...)                                        # fills BOM_TEMPLATE.xlsx
        -> xlsx bytes + confidence (flags) returned to the pipeline, which hosts + attaches the DRAFT.

Reference orchestration: docs/pipeline_create_bom.py (injected-callable skeleton). We keep build_bom's
(bytes, confidence) contract so app/pipeline.py (untouched) still does the host/attach/notify.

SHADOW MODE: every run surfaces flags. A planset field the v2 extractor does not yet surface
(disconnect ratings, breaker list, racking table, attachment type, per-array orientation, meter/MSP
equipment SKU + new-vs-existing) is FLAGGED for human review — never guessed. The DRAFT carries what
is confidently computable (module line, battery/RSD, header) plus the flags; a human completes the rest.
"""
from __future__ import annotations
import asyncio
import os
import shutil
import tempfile

from . import electrical_engine as ee
from . import racking_engine as re_eng
from . import bom_writer
from .extractor import PlansetExtractor

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(HERE, "BOM_TEMPLATE.xlsx")


class NeedsHumanExtraction(RuntimeError):
    """Raised when extraction cannot run at all (e.g. ANTHROPIC_API_KEY unset). Routes through the
    pipeline's failure-notify path so a human builds the BOM instead of shipping an empty one."""


# ---------- extraction ----------
def extract_planset(planset_pdf_path: str, project: dict, master_note_form: dict | None = None):
    """Run the real Claude-Vision extractor and return its PlansetData.

    Synchronous wrapper over the async extractor so the (sync) pipeline background task can call it.
    Raises NeedsHumanExtraction if the API key is absent (clean failure, not a stack trace)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise NeedsHumanExtraction(
            "ANTHROPIC_API_KEY is not set; planset extraction cannot run. Set it in Railway "
            "Variables to enable automated BOM drafts.")
    return asyncio.run(PlansetExtractor().extract(planset_pdf_path, project, master_note_form))


# ---------- consolidated block build ----------
def _addr(project: dict) -> str:
    a = project.get("address")
    return a[0] if isinstance(a, list) and a else (a or "")


def _run_block(label, flags, target_rows, fn, target_special=None):
    """Run ONE engine block and merge its output. A block that RAISES (import error, runtime error)
    becomes a HARD flag here — never a crash, never a silent drop. This is the load-bearing rule:
    a swallowed failure must HOLD the BOM, not produce a 'ready for review' draft missing content.
    Accepts blocks that return (rows, flags) or (rows, flags, special_order)."""
    try:
        out = fn()
        if isinstance(out, tuple) and len(out) == 3:
            rows, blk_flags, special = out
        else:
            rows, blk_flags = out
            special = None
        bom_writer.merge_block(target_rows, rows or {})
        if special and target_special is not None:
            bom_writer.merge_special(target_special, special)
        flags.extend(blk_flags or [])
    except Exception as e:  # noqa: BLE001 — any block failure must surface as a HARD flag
        flags.append({"level": "HARD", "item": f"block_failed:{label}",
                      "msg": f"Engine block {label!r} failed to run ({type(e).__name__}: {e}). "
                             f"This line could not be computed — build it manually and report the error."})


def _build_blocks(planset, project: dict):
    """Drive the engine blocks from the extracted PlansetData. Returns
    (solar_rows, electrical_rows, solar_special, electrical_special, flags).

    Every block goes through _run_block so a raised exception becomes a HARD flag rather than
    crashing the run or silently dropping the line.
    """
    solar_rows: dict[int, int] = {}
    elec_rows: dict[int, int] = {}
    solar_special: dict[int, str] = {}
    elec_special: dict[int, str] = {}
    flags: list[dict] = []

    # --- Module line (Solar rows 5-9) ---
    _run_block("solar_module", flags, solar_rows,
               lambda: ee.solar_module(planset.module_model, planset.module_quantity), solar_special)

    # --- RSD device (Electrical row 19) = ceil(batteries/3); PW3 expansion excluded ---
    battery_count = int(planset.battery_quantity or 0)
    _run_block("rsd_device", flags, elec_rows, lambda: ee.rsd_device(battery_count))

    # ===== PV-5 ELECTRICAL BLOCKS (driven by extractor.PlansetData.electrical) =====
    elec = getattr(planset, "electrical", None) or {}
    if not elec:
        flags.append({"level": "HARD", "item": "pv5_electrical_missing",
                      "msg": "PV-5 electrical detail was not extracted (empty). AC/DC disconnects, "
                             "breakers, meter, and MSP could not be auto-populated — build the "
                             "Electrical sheet manually and verify against PV-5."})

    if elec.get("ac_disconnects"):                 # rows 5-12 + hubs 13-15 + fuses 130-144
        _run_block("ac_disconnects", flags, elec_rows, lambda: ee.ac_disconnects(elec["ac_disconnects"]))
    if elec.get("dc_disconnects"):                 # rows 17-18 by pole count
        _run_block("dc_disconnects", flags, elec_rows, lambda: ee.dc_disconnects(elec["dc_disconnects"]))
    if elec.get("one_line_text"):                  # rows 26-28 supply-side taps (substring match)
        _run_block("supply_side_taps", flags, elec_rows, lambda: ee.supply_side_taps(elec["one_line_text"]))

    # Tesla PW3 / Gateway / Backup switch / inverter / remote meter (rows 51-57)
    pw3_skus = elec.get("pw3_skus") or []
    gateway_count = int(elec.get("gateway_count") or 0)
    backup_switch = bool(elec.get("backup_switch"))
    if pw3_skus or gateway_count or backup_switch or elec.get("inverter_sku") or elec.get("remote_meter_count"):
        _run_block("tesla_core", flags, elec_rows,
                   lambda: ee.tesla_core(pw3_skus=pw3_skus, gateway_count=gateway_count,
                                         backup_switch=backup_switch, inverter_sku=elec.get("inverter_sku"),
                                         remote_meter_count=int(elec.get("remote_meter_count") or 0)))

    if gateway_count:
        _run_block("ground_bar", flags, elec_rows, lambda: ee.ground_bar(gateway_count))  # row 22
        pw3_count = len(pw3_skus) or battery_count
        # Read EVERY bus-kit breaker straight from the plan (Vision) — the 60A/2P-per-PW3 ones AND any
        # additional ratings (e.g. 100A/2P -> BR2100). The engine's gate reconciles the 60A/2P count
        # against PW3; other ratings are legitimate and don't trip it. (CSR note cross-check is done in
        # the extractor as csr_note_conflict, so master_note_csr is left unset here.)
        buskit = [(int(b["amp"]), int(b["poles"]))
                  for b in (elec.get("buskit_breakers") or []) if b.get("amp") and b.get("poles")]
        csr = [int(a) for a in (elec.get("csr_breakers") or []) if a]
        _run_block("tesla_gateway_breakers", flags, elec_rows,           # BR 98-110 / CSR 112-114
                   lambda: re_eng.tesla_gateway_breakers(buskit, csr, battery_pw3_count=pw3_count))

    # Tesla PW3 Expansion (rows 59 unit / 63-65 harness by -05/-20/-40 / 61 stack or 62 wall kit).
    # Mount was already resolved in the extractor (plan -> Master Note -> default wall).
    exp_count = elec.get("expansion_count")
    if exp_count and int(exp_count) > 0:
        _run_block("tesla_expansion", flags, elec_rows,
                   lambda: ee.tesla_expansion(int(exp_count), harness_pn=elec.get("harness_pn"),
                                              mount=elec.get("expansion_mount")))
    elif (pw3_skus or battery_count) and exp_count is None:
        # Battery system, but the expansion count couldn't be read (not an explicit 0). Don't silently
        # omit the expansion block — flag for review.
        flags.append({"level": "SOFT", "item": "expansion_count_undetermined",
                      "msg": "Battery system present but no PW3 Expansion-unit count could be read "
                             "from the equipment text (not an explicit 0). If there is an expansion "
                             "unit, its rows (59/61-65) are missing — verify PV-1/PV-5."})

    # Meter line + special-order P/N (rows 81-96): NEW meter only; unmapped P/N -> row 96 stamped B+C.
    _run_block("meter_socket", flags, elec_rows,
               lambda: ee.meter_socket(bool(elec.get("new_meter_drawn")), elec.get("meter_pn")), elec_special)
    # MSP line + special-order P/N (rows 73-79): NEW MSP only; unmapped -> row 79 stamped.
    _run_block("main_service_panel", flags, elec_rows,
               lambda: ee.main_service_panel(bool(elec.get("new_msp_drawn")), elec.get("msp_pn")), elec_special)

    # --- Completeness gates: a PV-5 read that came back but is missing CORE items is an extraction
    #     miss, not a clean BOM. These encode physical install invariants so an implausibly-empty
    #     electrical core HARD-holds for review instead of reading "ready". ---
    if elec:
        if not elec.get("ac_disconnects"):
            flags.append({"level": "HARD", "item": "no_ac_disconnect_read",
                          "msg": "No AC disconnect was read from PV-5, but a grid-tied PV system has at "
                                 "least one. Extraction is likely incomplete — verify the PV-5 page and "
                                 "the one-line before using this BOM."})
        if battery_count > 0 and not gateway_count and not backup_switch:
            flags.append({"level": "HARD", "item": "battery_no_gateway_or_backup",
                          "msg": f"{battery_count} battery unit(s) read but neither a Tesla Gateway nor a "
                                 "Backup Switch — implausible for an ESS. The PV-5 electrical read is "
                                 "likely incomplete; verify before use."})

    # --- Racking BOM table (Solar rows 33-90): K2 shingle roof / K2 ground mount ---
    _run_block("racking", flags, solar_rows, lambda: _racking_block(planset), solar_special)

    # --- Blocks still NOT auto-fed by the v2 extractor: flag, do NOT guess ---
    flags += _missing_input_flags(planset)

    return solar_rows, elec_rows, solar_special, elec_special, flags


def _racking_block(planset):
    """Solar racking BOM (rows 33-90) from the extractor's planset.racking table. Routes by attachment
    type: K2 ground -> racking_engine.k2_ground_mount (rows 73-90); K2 shingle -> roof rows 33-52 with
    the planset-table quantities authoritative and the engine formulas applied for the computed rows
    (deck screw = 4*attach, end cap = plan end clamps, ground lug = rows+1, wire clip = modules*2).
    Returns (rows, flags, overrides). Anything the table can't resolve is FLAGGED, never guessed.

    Orientation-dependent formula cross-checks (resolve_racking's attachment/rail validation, which
    needs the rotated-raster per-array orientation) are deferred to the orientation sub-part; the
    planset-table values are delivered as authoritative in the meantime."""
    rk = getattr(planset, "racking", None) or {}
    rows: dict[int, int] = {}
    overrides: dict[int, tuple] = {}
    flags: list = []
    fmt = rk.get("format")
    if not rk or fmt in (None, "absent"):
        flags.append({"level": "HARD", "item": "racking_not_read",
                      "msg": "Racking BOM table could not be read from the planset (no PV-3 text table "
                             "and no recognizable image BOM sheet). Build Solar racking rows 33-90 by hand."})
        return rows, flags, overrides

    at = rk.get("attachment_type")
    system = rk.get("system_type")
    mc = int(rk.get("module_count") or 0)

    # --- K2 GROUND MOUNT (rows 73-90) — straight through the existing GM engine ---
    if system == "ground" or at == "K2_GROUND":
        table = rk.get("ground_bom_table") or {}
        if not table:
            flags.append({"level": "HARD", "item": "racking_gm_table_empty",
                          "msg": f"K2 ground-mount detected ({rk.get('source')}) but no BOM lines were "
                                 f"resolved. Unresolved: {rk.get('unresolved')}. Populate rows 73-90 by hand."})
            return rows, flags, overrides
        r, ov, f = re_eng.k2_ground_mount(table, bool(rk.get("has_enphase")), mc)
        rows.update(r); overrides.update(ov); flags.extend(f or [])
        for u in (rk.get("unresolved") or []):
            flags.append({"level": "WARN", "item": "racking_line_unresolved",
                          "msg": f"K2 GM BOM line not mapped to a template row: {u}. Verify/add it."})
        return rows, flags, overrides

    # --- K2 SHINGLE ROOF (rows 33-52) — planset table authoritative ---
    if at == "K2_SHINGLE":
        roof = rk.get("roof") or {}
        att, rails, splice = roof.get("attachments"), roof.get("rails"), roof.get("splice")
        mid, ends = roof.get("mid_clamps"), roof.get("end_clamps")
        if att is not None:
            rows[33] = att                                   # K2 Multimount shingle attachment
            rows[46] = att                                   # K2 rail clamp = attachment qty (truth)
            rows[35] = re_eng.f_deck_screw(att)              # deck screw = 4*attach (default; lag row 34 alt)
        if rails is not None:
            rows[45] = rails                                 # K2 CrossRail
        if splice is not None:
            rows[47] = splice                                # K2 splice
        if mid is not None or ends is not None:
            rows[48] = (mid or 0) + (ends or 0)              # K2 cross (combo) clamp = mid + end
        if ends is not None:
            rows[51] = ends                                  # K2 end cap = plan end-clamp qty (truth)
            if ends % 4 == 0:
                rows[50] = ends // 4 + 1                      # K2 ground lug = rows+1 (rows = end_clamps/4)
            else:
                flags.append({"level": "NOTE", "item": "racking_ground_lug_uncomputed",
                              "msg": f"End clamps={ends} not divisible by 4, so the row count "
                                     f"(end_clamps/4) and ground-lug qty couldn't be computed. Verify."})
        if mc:
            rows[52] = mc * 2                                # K2 wire-management clip = modules*2
        if rk.get("has_enphase"):
            flags.append({"level": "NOTE", "item": "racking_mlpe_lug_pending",
                          "msg": "Enphase micros present — K2 MLPE lug (row 49) needs the micro count; "
                                 "not auto-populated. Add row 49 manually."})
        missing = [k for k, v in (("attachments", att), ("rails", rails), ("splice", splice),
                                  ("mid_clamps", mid), ("end_clamps", ends)) if v is None]
        if missing:
            flags.append({"level": "HARD", "item": "racking_table_incomplete",
                          "msg": f"Roof racking table read from {rk.get('source')} but missing fields "
                                 f"{missing}. Those rows were not populated — verify the PV-3 BOM table."})
        flags.append({"level": "NOTE", "item": "racking_orientation_crosschecks_deferred",
                      "msg": "Roof racking quantities delivered from the planset BOM table (authoritative). "
                             "Orientation-based formula cross-checks (attachment/rail validation) are "
                             "deferred to the per-array orientation sub-part."})
        return rows, flags, overrides

    # --- S-5! metal / unrecognized — read but not auto-routed (don't guess) ---
    flags.append({"level": "HARD", "item": "racking_attachment_unrouted",
                  "msg": f"Racking attachment type {at!r} (system {system!r}) is not auto-routed yet "
                         f"(K2 shingle and K2 ground are wired; S-5! metal pending validation). "
                         f"Table read: {rk.get('roof') or rk.get('ground_bom_table')}. Populate manually."})
    return rows, flags, overrides


def _missing_input_flags(planset) -> list[dict]:
    """Explicit NOTE flags for every block whose structured inputs the v2 extractor does not surface,
    so a reviewer sees exactly what was NOT auto-populated (no silent gaps). Each names the field the
    extractor must add to make that block headless."""
    # PV-5 electrical (disconnects, breakers, meter, MSP, taps, gateway) is now wired in _build_blocks.
    # These remain unwired pending their own extractor enrichment:
    needed = [
        ("jboxes", "strings-per-array split by roof type (shingle/rail/metal/ground)"),
        ("micro_accessories", "microinverter SKU + branch-circuit count (Enphase Engage/combiner rows)"),
        ("homeline_msp_breaker", "a NEW Homeline MSP interconnection breaker (row-128 fallback) — "
                                 "deferred: its special-order format writes distinct B/C and needs a "
                                 "bom_writer change"),
    ]
    return [{"level": "NOTE", "item": f"extractor_missing:{item}",
             "msg": f"Not auto-populated — the v2 extractor's PlansetData does not surface {what}. "
                    f"Human must add this line; or enrich extractor.PlansetData with this field."}
            for item, what in needed]


def _normalize_flags(flags: list[dict]) -> list[dict]:
    """The pipeline's _count_flags counts only HARD/SOFT. The engine emits HARD/NOTE/WARN. Normalize
    so HARD stays HARD and NOTE/WARN -> SOFT (so the assignee comment's 'N soft' count reflects review
    items), preserving the original level under level_engine and the message text."""
    out = []
    for fl in flags or []:
        lvl = str(fl.get("level", "")).upper()
        norm = "HARD" if lvl == "HARD" else "SOFT"
        out.append({**fl, "level": norm, "level_engine": lvl,
                    "msg": fl.get("msg") or fl.get("detail", "")})
    return out


# ---------- public entry point ----------
def build_bom(planset_pdf_path: str, coperniq_project_dict: dict,
              master_note_form: dict | None = None) -> tuple[bytes, dict]:
    """Public entry point for the trigger service. Returns (xlsx_bytes, confidence_dict).

    master_note_form: the project's "Master Note" form (app/pipeline fetches it via
    list_project_forms -> get_form). Drives expansion mount/stack resolution; None if absent."""
    confidence: dict = {
        "project": {
            "id": coperniq_project_dict.get("id"),
            "name": coperniq_project_dict.get("title"),
            "number": coperniq_project_dict.get("number"),
        },
        "mode": "shadow",
        "FLAGS_FOR_HUMAN_REVIEW": [],
    }

    # 1) EXTRACT (Claude Vision). app/pipeline.py fetches the project's "Master Note" form and passes
    #    it here so the extractor's mount/stack resolution can use it.
    planset = extract_planset(planset_pdf_path, coperniq_project_dict, master_note_form=master_note_form)

    # 2) HEADER + BLOCKS
    zone, zone_flags = ee.warehouse_zone(coperniq_project_dict)
    solar_rows, elec_rows, solar_sp, elec_sp, flags = _build_blocks(planset, coperniq_project_dict)
    flags = list(zone_flags) + list(flags)
    if master_note_form is None:
        flags.append({"level": "NOTE", "item": "master_note_not_fetched",
                      "msg": "No 'Master Note' form was available for this project, so expansion "
                             "mount/stack resolution falls back to the default — verify if there are "
                             "expansion units."})

    # 2a) Structured flags raised inside the extractor (e.g. equipment_count_mismatch).
    flags.extend(getattr(planset, "extraction_flags", []) or [])

    # 2b) Surface extraction-time issues as FLAGS, not a buried warnings list. Anything that looks
    #     like a failure (import/parse/exception) is HARD — a swallowed error must never read as
    #     "ready for review"; benign notices (fallback page used, mount defaulted) are SOFT but visible.
    _ERR_MARKERS = ("fail", "error", "exception", "no module named", "traceback", "could not", "unparseable")
    for w in (getattr(planset, "extraction_warnings", []) or []):
        is_err = any(k in str(w).lower() for k in _ERR_MARKERS)
        flags.append({"level": "HARD" if is_err else "NOTE",
                      "item": "extraction_error" if is_err else "extraction_warning",
                      "msg": str(w)})

    # 3) WRITE the BOM via the canonical writer (static qtys + special-order P/N stamping into B AND C).
    tmpdir = tempfile.mkdtemp()
    out_path = os.path.join(tmpdir, "bom.xlsx")
    try:
        bom_writer.write_bom(
            TEMPLATE_PATH, out_path,
            customer_name=(planset.customer_name or coperniq_project_dict.get("title") or ""),
            warehouse_zone=zone,
            customer_address=(planset.customer_address or _addr(coperniq_project_dict)),
            solar_rows=solar_rows,
            electrical_rows=elec_rows,
            solar_special_order=solar_sp,
            electrical_special_order=elec_sp,
        )
        with open(out_path, "rb") as fh:
            data = fh.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 4) CONFIDENCE — echo the PARSED PV-5 electrical reads so the report shows what extraction
    #    produced (vs. what a block then did with it). If a row is empty here, extraction missed it.
    el = getattr(planset, "electrical", {}) or {}
    confidence["FLAGS_FOR_HUMAN_REVIEW"] = _normalize_flags(flags)
    confidence["extraction"] = {
        "module_model": planset.module_model,
        "module_quantity": planset.module_quantity,
        "battery_quantity": planset.battery_quantity,
        "warnings": list(getattr(planset, "extraction_warnings", []) or []),
        "electrical": {
            "ac_disconnects": el.get("ac_disconnects"),
            "dc_disconnects": el.get("dc_disconnects"),
            "buskit_breakers": el.get("buskit_breakers"),
            "csr_breakers": el.get("csr_breakers"),
            "gateway_count": el.get("gateway_count"),
            "backup_switch": el.get("backup_switch"),
            "pw3_skus": el.get("pw3_skus"),
            "new_meter_drawn": el.get("new_meter_drawn"),
            "meter_pn": el.get("meter_pn"),
            "new_msp_drawn": el.get("new_msp_drawn"),
            "msp_pn": el.get("msp_pn"),
            "one_line_text_present": bool(el.get("one_line_text")),
            "pw3_count": el.get("pw3_count"),
            "pw3_count_sources": el.get("pw3_count_sources"),
            "expansion_count": el.get("expansion_count"),
            "harness_pn": el.get("harness_pn"),
            "harness_source": el.get("harness_source"),
            "expansion_mount": el.get("expansion_mount"),
            "buskit_breakers": el.get("buskit_breakers"),
            "buskit_source": el.get("buskit_source"),
            "buskit_vision": el.get("buskit_vision"),
            "csr_breakers": el.get("csr_breakers"),
            "csr_vision": el.get("csr_vision"),
            "csr_note_check": el.get("csr_note_check"),
            "one_line_text": el.get("one_line_text"),
        },
        "racking": {k: v for k, v in (getattr(planset, "racking", None) or {}).items()
                    if k != "raw_rows"},
    }
    return data, confidence

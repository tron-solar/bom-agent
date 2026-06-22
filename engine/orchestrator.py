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
        buskit = [(int(b["amp"]), int(b["poles"]))
                  for b in (elec.get("buskit_breakers") or []) if b.get("amp") and b.get("poles")]
        csr = [int(a) for a in (elec.get("csr_breakers") or []) if a]
        _run_block("tesla_gateway_breakers", flags, elec_rows,           # BR 98-110 / CSR 112-114
                   lambda: re_eng.tesla_gateway_breakers(buskit, csr,
                                                         battery_pw3_count=(len(pw3_skus) or battery_count)))

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

    # --- Blocks still NOT auto-fed by the v2 extractor: flag, do NOT guess ---
    flags += _missing_input_flags(planset)

    return solar_rows, elec_rows, solar_special, elec_special, flags


def _missing_input_flags(planset) -> list[dict]:
    """Explicit NOTE flags for every block whose structured inputs the v2 extractor does not surface,
    so a reviewer sees exactly what was NOT auto-populated (no silent gaps). Each names the field the
    extractor must add to make that block headless."""
    # PV-5 electrical (disconnects, breakers, meter, MSP, taps, gateway) is now wired in _build_blocks.
    # These remain unwired pending their own extractor enrichment:
    needed = [
        ("racking", "per-array orientation (rotated raster), the planset racking BOM table, and attachment type"),
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
def build_bom(planset_pdf_path: str, coperniq_project_dict: dict) -> tuple[bytes, dict]:
    """Public entry point for the trigger service. Returns (xlsx_bytes, confidence_dict)."""
    confidence: dict = {
        "project": {
            "id": coperniq_project_dict.get("id"),
            "name": coperniq_project_dict.get("title"),
            "number": coperniq_project_dict.get("number"),
        },
        "mode": "shadow",
        "FLAGS_FOR_HUMAN_REVIEW": [],
    }

    # 1) EXTRACT (Claude Vision). master_note_form is None here: app/pipeline.py (untouched) does not
    #    fetch the project's "Master Note" form, so mount/stack resolution is limited — flagged below.
    planset = extract_planset(planset_pdf_path, coperniq_project_dict, master_note_form=None)

    # 2) HEADER + BLOCKS
    zone, zone_flags = ee.warehouse_zone(coperniq_project_dict)
    solar_rows, elec_rows, solar_sp, elec_sp, flags = _build_blocks(planset, coperniq_project_dict)
    flags = list(zone_flags) + list(flags)
    flags.append({"level": "NOTE", "item": "master_note_not_fetched",
                  "msg": "Master Note form not fetched (app/pipeline.py does not call "
                         "list_project_forms/get_form), so expansion mount/stack resolution is "
                         "unavailable. Wire the form fetch to enable it."})

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
        },
    }
    return data, confidence

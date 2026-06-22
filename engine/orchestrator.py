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


def _build_blocks(planset, project: dict):
    """Drive the engine blocks from the extracted PlansetData. Returns
    (solar_rows, electrical_rows, solar_special, electrical_special, flags).

    Each electrical_engine block returns its own {row: qty} (+ flags, + special-order map for the
    meter/MSP lines); we merge them with bom_writer.merge_block / merge_special, exactly as the
    consolidated builder in docs/pipeline_create_bom.py prescribes.
    """
    solar_rows: dict[int, int] = {}
    elec_rows: dict[int, int] = {}
    solar_special: dict[int, str] = {}
    elec_special: dict[int, str] = {}
    flags: list[dict] = []

    # --- Module line (Solar rows 5-9) — fully drivable from PlansetData ---
    r, f = ee.solar_module(planset.module_model, planset.module_quantity)
    bom_writer.merge_block(solar_rows, r)
    flags += f

    # --- RSD device (Electrical row 19) = ceil(batteries/3); PW3 expansion excluded ---
    battery_count = int(planset.battery_quantity or 0)
    r, f = ee.rsd_device(battery_count)
    bom_writer.merge_block(elec_rows, r)
    flags += f

    # --- Meter line + special-order P/N path (Electrical rows 81-96) ---
    # PlansetData carries `meter_number` (a utility METER SERIAL, not an equipment SKU) and no
    # new-vs-existing signal. The engine rule orders a meter line ONLY for a NEW meter and matches by
    # exact equipment P/N. Feeding a serial as a P/N would falsely stamp the special-order line, so we
    # pass new_meter_drawn=False and FLAG that the extractor must surface (a) new-meter-drawn and
    # (b) the meter equipment SKU. The special-order plumbing below is exercised by the module self-test.
    r, f, sp = ee.meter_socket(new_meter_drawn=False, meter_pn=None)
    bom_writer.merge_block(elec_rows, r)
    bom_writer.merge_special(elec_special, sp)
    flags += f

    # --- MSP line + special-order P/N path (Electrical rows 73-79) ---
    # PlansetData has main_panel_amperage but no MSP equipment SKU and no new-MSP flag -> cannot drive
    # the line without guessing. Pass new_msp_drawn=False and flag for human/extractor enrichment.
    r, f, sp = ee.main_service_panel(new_msp_drawn=False, msp_pn=None)
    bom_writer.merge_block(elec_rows, r)
    bom_writer.merge_special(elec_special, sp)
    flags += f

    # --- Blocks the v2 extractor's PlansetData cannot yet feed: flag, do NOT guess ---
    flags += _missing_input_flags(planset)

    return solar_rows, elec_rows, solar_special, elec_special, flags


def _missing_input_flags(planset) -> list[dict]:
    """Explicit NOTE flags for every block whose structured inputs the v2 extractor does not surface,
    so a reviewer sees exactly what was NOT auto-populated (no silent gaps). Each names the field the
    extractor must add to make that block headless."""
    needed = [
        ("ac_disconnects", "AC disconnect list (amp + fused + fuse_amp) from the PV-5 one-line"),
        ("dc_disconnects", "DC disconnect pole counts from the PV-5 one-line"),
        ("breakers_csr", "breakers drawn on the one-line (bus-kit / CSR / interconnection) by rating+poles"),
        ("supply_side_taps", "the raw PV-5 one-line text (for K4977 / IT-3/0 / IT-250 tap lugs)"),
        ("gateway_ground_bar", "Tesla Gateway count + backup-switch presence (rows 22/54/57)"),
        ("racking", "per-array orientation (rotated raster), the planset racking BOM table, and attachment type"),
        ("jboxes", "strings-per-array split by roof type (shingle/rail/metal/ground)"),
        ("micro_accessories", "microinverter SKU + branch-circuit count (Enphase Engage/combiner rows)"),
        ("meter_sku_and_new_flag", "the meter EQUIPMENT SKU + a NEW-meter-drawn boolean (row 81-96 + special-order)"),
        ("msp_sku_and_new_flag", "the MSP EQUIPMENT SKU + a NEW-MSP-drawn boolean (rows 73-79 + special-order)"),
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

    # 4) CONFIDENCE
    confidence["FLAGS_FOR_HUMAN_REVIEW"] = _normalize_flags(flags)
    confidence["extraction"] = {
        "module_model": planset.module_model,
        "module_quantity": planset.module_quantity,
        "battery_quantity": planset.battery_quantity,
        "warnings": list(getattr(planset, "extraction_warnings", []) or []),
    }
    return data, confidence

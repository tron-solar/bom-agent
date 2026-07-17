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


def _pw3_landing_breaker_qty(ac_disconnects, pw3_count, existing_br260):
    """A PW3 lands on a 60A/2P breaker (BR260, Electrical row 106). When any AC disconnect is
    NON-FUSED and PW3s are present, there must be one BR260 per PW3 — the SAME row/SKU as the bus-kit
    60A/2P, coupled to the PW3 count. A disconnect is FUSED only if 'fuse'/'fused' appears in its read;
    otherwise it defaults to NON-FUSED. Returns the row-106 qty to SET (== pw3_count), or None to leave
    it unchanged — None when there is no PW3, no non-fused disconnect, or the bus-kit already provided
    >= pw3_count BR260 (so the gateway bus-kit breakers are never double-counted)."""
    if pw3_count <= 0:
        return None

    def _fused(d):
        if d.get("fused") is not None:
            return bool(d.get("fused"))
        text = f"{d.get('type', '')} {d.get('description', '')} {d.get('label', '')}".lower()
        if any(neg in text for neg in ("non-fused", "non fused", "nonfused", "non-fuse")):
            return False                       # "NON-FUSED" contains "fuse" — negation wins
        return "fuse" in text

    if not any(not _fused(d) for d in (ac_disconnects or [])):
        return None
    if int(existing_br260 or 0) >= pw3_count:
        return None
    return pw3_count


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

    # --- S-clips (Solar row 30) = module_count * 6, written as a STATIC int. The template ships A30 as
    # '=IF(...)*6', which Coperniq's un-recalced viewer shows BLANK; writing the computed integer fixes
    # the display without changing the x6 rule (user, Dare). ---
    _mc = int(planset.module_quantity or 0)
    if _mc:
        _run_block("s_clips", flags, solar_rows, lambda: ({30: re_eng.f_s_clip(_mc)}, []))

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

    # Tesla MCI-2 RSD modules (SOLAR row 20) — count read from PV-3 BOM (truth) / PV-1 scope (xcheck)
    # in the extractor; conflict already flagged there. Never derived from module count.
    if elec.get("mci2_count"):
        _run_block("solar_mci", flags, solar_rows, lambda: ee.solar_mci(elec.get("mci2_count")))

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

    # PW3 landing breaker (unconditional; decides its own applicability): a NON-FUSED AC disconnect
    # with PW3 present needs one 60A/2P (BR260, Electrical row 106) per PW3 to land on — same row/SKU
    # as the bus-kit 60A/2P, coupled to the PW3 count, de-duped against the gateway bus-kit's BR260.
    pw3_landing_count = len(pw3_skus) or battery_count
    _landing = _pw3_landing_breaker_qty(elec.get("ac_disconnects") or [], pw3_landing_count,
                                        elec_rows.get(106, 0))
    if _landing is not None:
        elec_rows[106] = _landing
        flags.append({"level": "NOTE", "item": "pw3_landing_breaker",
                      "msg": f"Non-fused AC disconnect with {pw3_landing_count} PW3 unit(s): {_landing}x "
                             f"BR260 (60A/2P, Electrical row 106) added for the PW3 to land on."})

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

    # --- J-boxes (Solar rows 25/26): per-plane strings -> JB-1.2 (shingle) / JB-3 (else + ground) ---
    _run_block("jboxes", flags, solar_rows, lambda: _jbox_block(planset))

    # --- Blocks still NOT auto-fed by the v2 extractor: flag, do NOT guess ---
    flags += _missing_input_flags(planset)

    return solar_rows, elec_rows, solar_special, elec_special, flags


def _racking_orientation_crosscheck(rk):
    """CROSS-CHECK ONLY (roof): turn the per-plane orientation read into make_row() arrays and run
    resolve_racking against the planset attach/rail/combo — the INDEPENDENT ground truth (NOT read
    agreement). Returns flags surfacing the PV-3 module-dims + SKU check, the attachment
    orientation-signal, the end_clamps/4 segmentation check, and resolve_racking's tolerance
    crosschecks. NEVER changes delivered rows; NEVER raises (gate/erros -> NOTE)."""
    flags = []
    roof = rk.get("roof") or {}
    md_info = rk.get("module_dims") or {}
    ori = rk.get("orientation") or {}

    # --- module dims: deliver the PV-3 value. A reviewer-facing flag is emitted ONLY when the read is
    # NOT fully corroborated. Fully corroborated == graphic_confirmed AND sku_check.agrees -> NO flag
    # (a check that passed must not produce a human-review item). Gate on the engine's own booleans.
    if md_info.get("flag"):
        flags.append(md_info["flag"])                       # HARD module_dims_unreadable (read failed)
        md = re_eng.ModuleDims.sirius_default()
    elif md_info.get("long_in"):
        md = re_eng.ModuleDims.from_pv3(md_info["long_in"], md_info["short_in"])
        sku = md_info.get("sku_check") or {}
        graphic_ok = bool(md_info.get("graphic_confirmed"))
        sku_agrees = sku.get("agrees") is True
        if graphic_ok and sku_agrees:
            pass                                            # fully corroborated -> emit NOTHING
        elif sku.get("matched_family") and sku.get("agrees") is False:
            flags.append({"level": "NOTE", "item": "module_dims_sku_mismatch",
                          "msg": f"PV-3 module dims {sku.get('pv3')} disagree with the "
                                 f"{sku.get('matched_family')} SKU table {sku.get('expected')}; delivered "
                                 f"the PV-3 value. Verify the module model / dims."})
        else:
            # delivered the PV-3 value but it isn't fully corroborated (graphic not confirmed and/or
            # SKU not cross-checkable) — surface it so a human can confirm the dims.
            flags.append({"level": "NOTE", "item": "module_dims_uncorroborated",
                          "msg": f"Module dims {md.long_in}x{md.short_in} from PV-3 delivered but NOT "
                                 f"fully corroborated (graphic_confirmed={graphic_ok}; sku_check={sku}). "
                                 f"Verify the dims."})
    else:
        md = re_eng.ModuleDims.sirius_default()             # dims fell back to a default -> flag it
        flags.append({"level": "NOTE", "item": "module_dims_defaulted",
                      "msg": "Module dims fell back to the Sirius default (no PV-3 dims read for this "
                             "project); verify the module dimensions."})

    planes = ori.get("planes") or []
    if not planes:
        flags.append({"level": "NOTE", "item": "racking_orientation_unavailable",
                      "msg": f"Per-plane orientation read unavailable ({ori.get('note')}); attachment/"
                             f"rail orientation cross-check not run. Planset racking delivered as-is."})
        return flags

    # --- build make_row() arrays from the reconciled per-plane orientation + row count
    arrays, skipped = [], []
    for p in planes:
        o, rws, mods, az = p.get("orientation"), p.get("rows"), p.get("modules"), p.get("azimuth")
        if o not in ("landscape", "portrait") or not isinstance(rws, int) or rws < 1 or not mods:
            skipped.append(p.get("label"))
            continue
        rws = min(rws, int(mods))                              # can't have more rows than modules
        edge = md.long_in if o == "landscape" else md.short_in
        base, extra = divmod(int(mods), rws)
        row_objs = []
        for i in range(rws):
            n = base + (1 if i < extra else 0)
            if n > 0:
                row_objs.append(re_eng.make_row(n, o, round(n * edge, 1), from_rotated_raster=True,
                                                long_in=md.long_in, short_in=md.short_in))
        arrays.append({"label": p.get("label"), "azimuth": az, "rows": row_objs})
    if skipped:
        flags.append({"level": "NOTE", "item": "racking_orientation_plane_skipped",
                      "msg": f"Orientation read incomplete for plane(s) {skipped}; excluded from the "
                             f"cross-check."})
    if not arrays:
        flags.append({"level": "NOTE", "item": "racking_orientation_unavailable",
                      "msg": "No plane produced a usable orientation read; cross-check not run."})
        return flags

    planset_dict = {"attachments": roof.get("attachments"), "rails": roof.get("rails"),
                    "splice": roof.get("splice"), "mid_clamps": roof.get("mid_clamps") or 0,
                    "end_clamps": roof.get("end_clamps")}
    if any(planset_dict[k] is None for k in ("attachments", "rails", "end_clamps")):
        flags.append({"level": "NOTE", "item": "racking_orientation_no_planset_truth",
                      "msg": f"Planset attach/rails/end-clamps incomplete {planset_dict}; the orientation "
                             f"cross-check needs them as ground truth — skipped."})
        return flags
    try:
        _, cc = re_eng.resolve_racking(arrays, planset_dict, enforce_orientation_gate=True,
                                       attachment_type="K2_SHINGLE", module_dims=md)
    except Exception as e:  # noqa: BLE001 — gate/runtime error must not lose the delivered rows
        flags.append({"level": "NOTE", "item": "racking_orientation_gate",
                      "msg": f"Orientation cross-check could not run ({type(e).__name__}: {e}). Planset "
                             f"quantities still delivered. Per-plane reads: "
                             f"{[(p.get('label'), p.get('orientation'), p.get('rows')) for p in planes]}"})
        return flags

    ac = cc["attachments"]
    per_plane = [(p.get('label'), p.get('orientation'), p.get('rows'),
                  p.get('orientation_agree'), p.get('rows_variance')) for p in planes]
    delta = ac['delta']
    corroborated = -3 <= delta <= 2     # same band as racking_crosscheck; clean attach match = corroborated
    flags.append({"level": "NOTE", "item": "racking_orientation_crosscheck",
                  "msg": (f"Orientation cross-check ({'CORROBORATED' if corroborated else 'NOT corroborated'}): "
                          f"attachments planset={ac['planset']} formula={ac['computed']} (delta {delta:+d}, "
                          f"band -3..+2). per-plane {per_plane}; dims {md.long_in}x{md.short_in}. "
                          f"{ac.get('orientation_signal')}")})
    # CROSS-CHECK-ONLY: planset quantities are delivered as truth, so resolve_racking's tolerance +
    # segmentation flags are ADVISORY signals about the orientation/row READ — surface them as NOTEs,
    # never as a HARD hold on a planset-truth BOM. (They carry their original level in the message.)
    for f in cc.get("tolerance_flags", []):
        flags.append({"level": "NOTE", "item": f"racking_xcheck:{f.get('item')}",
                      "msg": f"[orientation cross-check, advisory — original {f.get('level')}] {f.get('msg')}"})
    return flags


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
    if fmt == "bom_table_not_found":
        flags.append({"level": "HARD", "item": "racking_bom_table_not_found",
                      "msg": f"Racking BOM table not found. {('; '.join(rk.get('unresolved') or []))} "
                             f"Build Solar racking rows 33-90 by hand — the stringing page was NOT read "
                             f"as the BOM."})
        return rows, flags, overrides
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
        flags += _racking_orientation_crosscheck(rk)
        return rows, flags, overrides

    # --- S-5! metal / unrecognized — read but not auto-routed (don't guess) ---
    flags.append({"level": "HARD", "item": "racking_attachment_unrouted",
                  "msg": f"Racking attachment type {at!r} (system {system!r}) is not auto-routed yet "
                         f"(K2 shingle and K2 ground are wired; S-5! metal pending validation). "
                         f"Table read: {rk.get('roof') or rk.get('ground_bom_table')}. Populate manually."})
    return rows, flags, overrides


def _jbox_block(planset):
    """J-boxes (Solar rows 25/26), driven by MODULES per roof plane — always computable, so this never
    HARD-flags. qty = sum over planes of max(1, ceil(modules_on_plane / 40)); SKU row 25 (JB-1.2) for
    asphalt shingle, row 26 (JB-3) for every other roof type and ground.

    The string-count method (per-plane strings, ceil/4) and the drawn JB count are CORROBORATION ONLY:
    at most a NOTE if they disagree or are unreadable — NEVER a HARD flag. (Removed the old HARD
    'jbox_per_plane_unresolved': the module-count rule is always available, so nothing gates on it.)"""
    jb = getattr(planset, "jboxes", None) or {}
    rows: dict[int, int] = {}
    flags: list = []
    roof_type = jb.get("roof_type")
    mpp = [int(m) for m in (jb.get("modules_per_plane") or []) if m]
    if not mpp:
        # Per-plane modules missing (PV-3 roof-description table not read) -> single-plane fallback from
        # the project module total. Still never HARD.
        total_mod = int(jb.get("module_total") or 0)
        if total_mod:
            mpp = [total_mod]
    if not mpp:
        flags.append({"level": "NOTE", "item": "jbox_not_computed",
                      "msg": "No per-plane or total module count available to size J-boxes; add rows "
                             "25/26 manually from the roof plan."})
        return rows, flags

    row, total = re_eng.f_jboxes_by_modules(mpp, roof_type)
    if total >= 1:
        rows[row] = total
    sku = "JB-1.2 (row 25)" if row == 25 else "JB-3 (row 26)"
    flags.append({"level": "NOTE", "item": "jbox_count",
                  "msg": f"J-boxes: {total}x {sku} from modules/plane {mpp} "
                         f"(max(1, ceil(modules/40)) per plane; roof_type={roof_type})."})

    # CORROBORATION ONLY (never HARD): the string-count method, if the per-plane strings resolved.
    planes = jb.get("planes")
    if planes:
        _, s_total = re_eng.f_jboxes(planes, roof_type)
        if s_total != total:
            flags.append({"level": "NOTE", "item": "jbox_string_corroboration",
                          "msg": f"String-count method -> {s_total} J-box(es) vs {total} by module count "
                                 f"(delivered). Cross-check only; verify if the gap is large."})
    return rows, flags


def _missing_input_flags(planset) -> list[dict]:
    """Explicit NOTE flags for every block whose structured inputs the v2 extractor does not surface,
    so a reviewer sees exactly what was NOT auto-populated (no silent gaps). Each names the field the
    extractor must add to make that block headless."""
    # PV-5 electrical (disconnects, breakers, meter, MSP, taps, gateway) is now wired in _build_blocks.
    # These remain unwired pending their own extractor enrichment:
    needed = [
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
            "mci2_count": el.get("mci2_count"),
            "mci2_count_sources": el.get("mci2_count_sources"),
            "one_line_text": el.get("one_line_text"),
        },
        "racking": {k: v for k, v in (getattr(planset, "racking", None) or {}).items()
                    if k != "raw_rows"},
        "jboxes": getattr(planset, "jboxes", None) or {},
    }
    return data, confidence

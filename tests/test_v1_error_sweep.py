"""Regression tests for the 2026-10-16 BOM Agent error sweep (OneNote 'BOM Agent errors')."""
from engine.electrical_engine import meter_socket, tesla_expansion_resolved, resolve_expansion_mount
from engine.racking_engine import k2_ground_mount
from engine.extractor import PlansetExtractor as _X  # noqa: F401  (import check)
import engine.extractor as ex

_CLS = [c for c in vars(ex).values() if isinstance(c, type) and hasattr(c, "_classify_attachment")][0]


def test_u9551_without_5t9_routes_row_89():          # Wells/Bolin/Alderson/Wyer/Hundley/Henderson
    rows, flags, so = meter_socket(True, "U9551-RXL-QG-AMS")
    assert rows == {89: 1} and not so


def test_unknown_meter_still_special_order():
    rows, _, so = meter_socket(True, "XYZ-1")
    assert rows == {96: 1} and 96 in so


def test_solarfoot_not_misclassified_as_protea():
    assert _CLS._classify_attachment("S-5! SOLARFOOT")[1] == "S5_SOLARFOOT"
    assert _CLS._classify_attachment("S-5! ProteaBracket")[1] == "S5_PROTEABRACKET"


def _forms(note):
    form = {"formLayouts": [{"fields": [{"name": "Design notes:", "type": "TEXT", "value": note}]}]}
    return (lambda pid: [{"id": 1, "name": "Master Note"}]), (lambda fid: form)


def test_mixed_stack_and_wall_per_unit():             # Murphy #852366
    note = ("First PW3 Expansion installed with stack kit (1978070) and harness (1875157-05-y)\n"
            "Second PW3 Expansion with wall mount kit (1978069-00-x) and harness (1875157-20-y)")
    lf, gf = _forms(note)
    rows, _ = tesla_expansion_resolved(2, "1875157-05-X", 1, lf, gf)
    assert rows[61] == 1 and rows[62] == 1 and rows[63] == 1 and rows[64] == 1


def test_bare_stack_word_does_not_flip_default_wall():  # Fickas #857566
    assert resolve_expansion_mount(None, "check stack of permits before install") == "wall"
    assert resolve_expansion_mount(None, "expansion stacked (1978070)") == "stack"


def test_gm_end_cap_wire_clip_filled_when_table_omits():  # Wright #918788 / Shewmake #967703
    rows, _, flags = k2_ground_mount({"4001370": 13}, False, 40)
    assert rows[78] == 26 and rows[83] == 80


def test_gm_table_value_still_wins():
    rows, _, _ = k2_ground_mount({"4001370": 13, "4001221": 52, "4000069": 80}, False, 40)
    assert rows[78] == 52 and rows[83] == 80


# ---- round 2: text-only disconnects, mixed roofs, MSP precedence ----
import os
from engine import text_tables as tt
from engine.electrical_engine import main_service_panel_resolved

_FX = os.path.join(os.path.dirname(__file__), "fixtures_bom_table.pdf")
_FX_ROOF = os.path.join(os.path.dirname(__file__), "fixtures_roof_tables.pdf")


def test_bom_table_disconnects_text_only():
    rows, _ = tt.equipment_rows(_FX)
    ac, dc = tt.disconnects_from_rows(rows)
    assert [(d["amp"], d["fused"]) for d in ac] == [(60, False), (60, False)]   # wrapped PV/ESS label
    assert sorted(d["poles"] for d in dc) == [2, 2, 4]


def test_no_text_callout_means_no_disconnect():
    assert tt.disconnects_from_callouts("AC DISCONNECT SHALL BE LOCKABLE\nSEE NOTE 4") == ([], [])


def test_fused_callout_reads_disco_and_fuse_ratings():
    ac, _ = tt.disconnects_from_callouts("(N) 60A FUSED AC DISCONNECT W/ (2) 40A FUSES")
    assert ac[0]["amp"] == 60 and ac[0]["fused"] and ac[0]["fuse_amp"] == 40


def test_existing_service_disconnect_excluded():                 # Latham #916365
    ac, _ = tt.disconnects_from_callouts("(E) 200A MAIN SERVICE DISCONNECT\n(N) 60A NON-FUSED AC DISCONNECT")
    assert [(d["amp"], d["fused"]) for d in ac] == [(60, False)]


def test_mixed_attachments_per_type():                            # Head #976824 / Wyer #915365
    rows, _ = tt.equipment_rows(_FX)
    assert tt.attachments_from_rows(rows) == {"K2_SHINGLE": 32, "S5_PROTEABRACKET": 112}
    assert tt.k2_attach_mounting(rows)["deck"] == 32


def test_roof_planes_by_type_per_structure():
    planes = tt.roof_planes_by_type(_FX_ROOF, 0)
    assert [(p["roof_type"], p["modules"]) for p in planes] == [("shingle", 12), ("metal", 15), ("metal", 15)]


def test_msp_master_note_wins_and_either_source_adds():
    assert main_service_panel_resolved(False, None, "Upgrade to new MSP HOM4080M200PC")[0] == {77: 1}
    assert main_service_panel_resolved(False, None, "Existing MSP can remain")[0] == {}
    rows, flags, _ = main_service_panel_resolved(True, "HOM3060M200PC", "New MSP HOM4080M200PC")
    assert rows == {77: 1} and any(f["item"] == "msp_note_vs_plan" for f in flags)


# ---- round 3: POI/landing breaker by panel mfr, ground bar, loads panel, default MSP, backup switch ----
from types import SimpleNamespace as _NS
import engine.orchestrator as _orch
import engine.electrical_engine as _ee


def _build(elec, mods=30, bat=0):
    p = _NS(module_model="ELNSM54M-HC-N-450", module_quantity=mods, battery_quantity=bat, electrical=elec,
            racking={}, jboxes={"roof_type": "shingle", "modules_per_plane": [mods]}, arrays=[])
    _, er, _, es, _ = _orch._build_blocks(p, {})
    return er, es


def test_backup_switch_milbank_meter_main_lands_on_eaton_106_plus_ground_bar():
    er, _ = _build({"pw3_skus": ["1707000"], "pw3_count": 1, "backup_switch": True, "gateway_count": 0,
                    "meter_pn": "U6281-XL-200-5T6-AMS", "new_meter_drawn": True, "master_note_text": "",
                    "one_line_text": "(N) UTILITY METER/MAIN COMBO MILBANK U6281-XL-200-5T6-AMS",
                    "ac_disconnects": [{"amp": 60, "fused": False}]}, bat=1)
    assert er.get(106) == 1 and er.get(22) == 1 and 124 not in er


def test_homeline_landing_row_124():
    er, _ = _build({"pw3_skus": ["1707000"], "pw3_count": 1, "backup_switch": True, "gateway_count": 0,
                    "master_note_text": "POI: 60A breaker in Homeline MSP", "one_line_text": "",
                    "ac_disconnects": []}, bat=1)
    assert er.get(124) == 1 and 106 not in er


def test_pv_only_poi_40a_homeline_and_no_rsd():
    er, _ = _build({"pw3_count": 0, "gateway_count": 0, "master_note_text": "",
                    "one_line_text": "(E) 200A SQUARE D HOMELINE MSP\n(N) 40A/2P PV BREAKER", "ac_disconnects": []})
    assert er.get(122) == 1 and 19 not in er and 106 not in er


def test_pv_only_unknown_panel_highlighted_row_115():
    er, es = _build({"pw3_count": 0, "gateway_count": 0, "one_line_text": "", "master_note_text": "",
                     "ac_disconnects": []})
    assert er.get(115) == 1 and "FIND EXACT SKU" in es[115][0]


def test_backed_up_loads_panel_100a_row_74():
    assert _ee.backed_up_loads_panel("(N) 100A BACKED UP LOADS PANEL")[0] == {74: 1}


def test_mpu_without_sku_defaults_row_76():
    assert _ee.main_service_panel_resolved(False, None, "MPU: upgrade to new 200A MSP")[0] == {76: 1}


def test_attachment_callouts_without_bom_table():
    assert tt.attachments_from_callouts("(112) S-5! PROTEA BRACKET\nATTACHMENTS\n(32) K2 MULTIMOUNT "
                                        "ATTACHMENTS (DECK MOUNTED)") == {"S5_PROTEABRACKET": 112, "K2_SHINGLE": 32}


# ---- Moyes #971010 (real PV-3): ProteaBracket delivered 74 vs plan 61 ----
_FX_MOYES = os.path.join(os.path.dirname(__file__), "fixtures_moyes_971010_pv3.pdf")


def test_moyes_bom_table_stops_before_roof_description():
    rows, _ = tt.equipment_rows(_FX_MOYES)
    assert tt.attachments_from_rows(rows) == {"S5_PROTEABRACKET": 61}
    assert all("ROOF DESCRIPTION" not in d for _, _, d in rows)


def test_moyes_disconnects_from_table():
    rows, _ = tt.equipment_rows(_FX_MOYES)
    ac, dc = tt.disconnects_from_rows(rows)
    assert [(d["amp"], d["fused"]) for d in ac] == [(60, True)] and [d["poles"] for d in dc] == [4]


def test_moyes_roof_planes_both_metal():
    planes = tt.roof_planes_by_type(_FX_MOYES, 0)
    assert sorted((p["roof_type"], p["modules"]) for p in planes) == [("metal", 10), ("metal", 13)]

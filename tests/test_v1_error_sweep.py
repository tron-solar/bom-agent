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

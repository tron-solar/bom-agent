"""module_dims emit logic: a fully-corroborated dims read must produce ZERO reviewer-facing flags;
only an actual disagreement (SKU mismatch, graphic not confirmed, or default fallback) emits one.
Error class guarded: "a check that passed still produced a HUMAN-REVIEW flag."
"""
from engine.orchestrator import _racking_orientation_crosscheck


def _rk(module_dims):
    # no orientation planes -> the function returns right after the module-dims block (it appends a
    # separate 'racking_orientation_unavailable' note, which these tests filter out).
    return {"attachment_type": "K2_SHINGLE", "roof": {}, "orientation": {"planes": []},
            "module_dims": module_dims}


def _module_dims_flags(flags):
    return [f for f in flags if str(f.get("item", "")).startswith("module_dims")]


def test_corroborated_dims_emit_no_flag():
    md = {"long_in": 67.8, "short_in": 44.65, "graphic_confirmed": True,
          "sku_check": {"matched_family": "ELNSM54M", "expected": [67.8, 44.65],
                        "pv3": [67.8, 44.65], "agrees": True}}
    assert _module_dims_flags(_racking_orientation_crosscheck(_rk(md))) == []


def test_sku_mismatch_emits_one_flag():
    md = {"long_in": 74.9, "short_in": 44.6, "graphic_confirmed": True,
          "sku_check": {"matched_family": "ELNSM54M", "expected": [67.8, 44.65],
                        "pv3": [74.9, 44.6], "agrees": False}}
    f = _module_dims_flags(_racking_orientation_crosscheck(_rk(md)))
    assert len(f) == 1 and f[0]["item"] == "module_dims_sku_mismatch"


def test_graphic_not_confirmed_emits_one_flag():
    # graphic NOT confirmed, even though the SKU agrees -> not fully corroborated -> one flag
    md = {"long_in": 67.8, "short_in": 44.65, "graphic_confirmed": False,
          "sku_check": {"matched_family": "ELNSM54M", "expected": [67.8, 44.65],
                        "pv3": [67.8, 44.65], "agrees": True}}
    f = _module_dims_flags(_racking_orientation_crosscheck(_rk(md)))
    assert len(f) == 1 and f[0]["item"] == "module_dims_uncorroborated"


def test_default_fallback_emits_one_flag():
    f = _module_dims_flags(_racking_orientation_crosscheck(_rk({})))
    assert len(f) == 1 and f[0]["item"] == "module_dims_defaulted"

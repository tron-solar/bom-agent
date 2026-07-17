"""FIX 2 (module-driven J-boxes, no HARD flag) and FIX 3 (non-fused disconnect -> BR260 per PW3)."""
import types

from engine.racking_engine import f_jboxes_by_modules
from engine.orchestrator import _jbox_block, _pw3_landing_breaker_qty


# ---------- FIX 2: J-boxes = sum over planes of max(1, ceil(modules/40)) ----------
def test_jboxes_by_modules_moore():
    # Moore: plane1=16, plane2=15 -> 1 + 1 = 2, shingle -> JB-1.2 row 25
    assert f_jboxes_by_modules([16, 15], "shingle") == (25, 2)


def test_jboxes_by_modules_over_40_plane():
    # a plane with >40 modules steps up: 45 -> ceil(45/40)=2 on that plane
    assert f_jboxes_by_modules([45], "shingle") == (25, 2)


def test_jboxes_by_modules_two_plane_large():
    # 50 -> 2, 90 -> 3  => 5 total
    assert f_jboxes_by_modules([50, 90], "shingle") == (25, 5)


def test_jboxes_by_modules_type_routing():
    assert f_jboxes_by_modules([16, 15], "ground") == (26, 2)      # non-shingle -> JB-3 row 26
    assert f_jboxes_by_modules([16, 15], "metal") == (26, 2)


def test_jbox_block_moore_no_hard_flag():
    planset = types.SimpleNamespace(jboxes={
        "roof_type": "shingle", "modules_per_plane": [16, 15], "module_total": 31, "planes": None})
    rows, flags = _jbox_block(planset)
    assert rows == {25: 2}                                         # 2x JB-1.2 (row 25)
    assert not any(f.get("level") == "HARD" for f in flags)       # ZERO HARD flags from jbox logic


def test_jbox_block_string_disagreement_is_note_not_hard():
    # module method -> 2; string method (5 strings on one plane -> ceil(5/4)=2, +1 = 3) disagrees ->
    # NOTE only, never HARD.
    planset = types.SimpleNamespace(jboxes={
        "roof_type": "shingle", "modules_per_plane": [16, 15], "module_total": 31,
        "planes": [{"label": "P1", "strings": 5}, {"label": "P2", "strings": 3}]})
    rows, flags = _jbox_block(planset)
    assert rows == {25: 2}
    assert not any(f.get("level") == "HARD" for f in flags)
    assert any(f["item"] == "jbox_string_corroboration" for f in flags)


# ---------- FIX 3: non-fused AC disconnect + PW3 -> BR260 (row 106) qty = PW3 count ----------
def test_nonfused_plus_pw3_adds_br260():
    q = _pw3_landing_breaker_qty([{"amp": 60, "fused": False}], pw3_count=2, existing_br260=0)
    assert q == 2                                                  # one BR260 per PW3


def test_fused_disconnect_adds_no_br260():
    q = _pw3_landing_breaker_qty([{"amp": 60, "fused": True, "fuse_amp": 60}], pw3_count=2,
                                 existing_br260=0)
    assert q is None                                              # fused -> fuses by rating, no auto BR260


def test_nonfused_does_not_double_count_buskit_br260():
    # bus-kit already provided 2 BR260 (gateway system) -> the landing rule must NOT add more
    q = _pw3_landing_breaker_qty([{"amp": 60, "fused": False}], pw3_count=2, existing_br260=2)
    assert q is None


def test_no_pw3_no_br260():
    q = _pw3_landing_breaker_qty([{"amp": 60, "fused": False}], pw3_count=0, existing_br260=0)
    assert q is None


def test_fused_inferred_from_description_when_flag_absent():
    # no explicit 'fused' key -> FUSED only if 'fuse'/'fused' text present; else NON-FUSED
    assert _pw3_landing_breaker_qty([{"amp": 60, "description": "60A FUSED AC DISCONNECT"}],
                                    pw3_count=2, existing_br260=0) is None
    assert _pw3_landing_breaker_qty([{"amp": 60, "description": "60A NON-FUSED AC DISCONNECT"}],
                                    pw3_count=2, existing_br260=0) == 2

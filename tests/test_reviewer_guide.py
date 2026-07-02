"""reviewer_guide: per-item guidance lookup, racking-family collapse, and the visible fallback for
unmapped items (never blank). Doc-only guidance — these tests assert the table, not the JSON."""
from engine import reviewer_guide as rg


def test_mapped_items_return_their_entry():
    loc, chk = rg.guidance_for("mci_count")
    assert loc == "Solar BOM — row 20" and "MCI-2 count" in chk
    loc, chk = rg.guidance_for("jbox_count")
    assert "row 25" in loc and "J-box" in chk


def test_racking_family_all_map_to_one_guidance():
    a = rg.guidance_for("racking_orientation_crosscheck")
    b = rg.guidance_for("racking_xcheck:row_segmentation_mismatch")
    c = rg.guidance_for("racking_xcheck:combo_clamp_crosscheck_high")
    assert a == b == c == rg._RACKING_GUIDANCE
    assert a[0] == "Solar BOM — racking rows"


def test_unmapped_item_returns_visible_fallback_never_blank():
    loc, chk = rg.guidance_for("some_future_flag_with_no_mapping")
    assert chk == rg.FALLBACK_CHECK and chk.startswith("⚠")
    assert loc and chk            # never blank


def test_reviewer_rows_collapses_racking_to_one_row():
    flags = [
        {"level": "SOFT", "item": "mci_count", "msg": "mci ok"},
        {"level": "SOFT", "item": "racking_orientation_crosscheck", "msg": "orient"},
        {"level": "SOFT", "item": "racking_xcheck:row_segmentation_mismatch", "msg": "seg"},
        {"level": "SOFT", "item": "racking_xcheck:end_cap_crosscheck_high", "msg": "endcap"},
        {"level": "SOFT", "item": "harness_pn_source", "msg": "harness"},
        {"level": "SOFT", "item": "brand_new_flag", "msg": "??"},
    ]
    rows = rg.reviewer_rows(flags)
    # 6 flags -> 4 rows (3 racking collapse to 1)
    items = [r[1] for r in rows]
    assert items == ["mci_count", "racking", "harness_pn_source", "brand_new_flag"]
    racking_row = rows[1]
    assert racking_row[3] == "Solar BOM — racking rows"          # collapsed location
    assert rows[-1][4] == rg.FALLBACK_CHECK                       # unmapped -> visible fallback


def test_no_guidance_leaks_into_json_shape():
    # reviewer_guide must be pure lookup — importing/using it does not mutate any flag dict
    flags = [{"level": "SOFT", "item": "mci_count", "msg": "x"}]
    rg.reviewer_rows(flags)
    assert set(flags[0].keys()) == {"level", "item", "msg"}      # untouched (JSON stays source of truth)

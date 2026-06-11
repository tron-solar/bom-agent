"""Tests for strict planset confirmation (app/planset_confirm.py)."""
import pytest
from app.planset_confirm import (select_planset, revision_letter, _convention_match,
                                 confirm_planset_content, PlansetNotConfirmed)


def _pdf(name, url="http://x", created="2026-01-01"):
    return {"name": name, "url": url, "createdAt": created, "metaData": {"extention": ".pdf"}}


def test_screenshot_case_picks_planset_not_cad_zip():
    files = [
        {"name": "Master Note", "metaData": {}},
        _pdf("Joseph Dare REVA.pdf"),
        {"name": "Joseph Dare_CAD_REV A.zip", "url": "z", "metaData": {"extention": ".zip"}},
    ]
    got = select_planset(files, "Joseph Dare")
    assert got.name == "Joseph Dare REVA.pdf" and got.revision == "A"


def test_highest_revision_wins():
    files = [_pdf("Joseph Dare REVA.pdf"), _pdf("Joseph Dare REVB.pdf"), _pdf("Joseph Dare REVC.pdf")]
    assert select_planset(files, "Joseph Dare").revision == "C"


def test_decoys_only_raises_no_false_pick():
    decoys = [_pdf("aLQUJ00000S6GJh4AN_Inputs.pdf"), _pdf("1778789557324_Design_Plans.pdf"),
              _pdf("1778789557324_EPC.pdf"), _pdf("darebill.pdf"), _pdf("site-audit-printout.pdf")]
    with pytest.raises(PlansetNotConfirmed):
        select_planset(decoys, "Joseph Dare")


def test_design_plans_is_not_a_planset():
    assert _convention_match("1778789557324_Design_Plans.pdf", "Joseph", "Dare") is False


def test_revision_letter_ignores_revised_revenue():
    assert revision_letter("Joseph Dare REVISED.pdf") == ""
    assert revision_letter("Q1 REVENUE.pdf") == ""
    assert revision_letter("Joseph Dare REV B.pdf") == "B"
    assert revision_letter("Joseph Dare REVC.pdf") == "C"


def test_separators_and_case_tolerated():
    for nm in ["joseph dare rev a.pdf", "Joseph  Dare  REV-A.pdf", "JOSEPH DARE REV_A.pdf"]:
        assert _convention_match(nm, "Joseph", "Dare") is True


def test_extra_words_not_matched():
    assert _convention_match("Joseph Dare CAD REV A.pdf", "Joseph", "Dare") is False
    assert _convention_match("Joseph Dare Planset REV A.pdf", "Joseph", "Dare") is False


def test_middle_name_customer():
    files = [_pdf("Joseph Michael Dare REVA.pdf")]
    # "Joseph ... Dare" — first+last tokens; middle word breaks the strict pattern by design.
    # Confirm it does NOT match (so we never silently accept a differently-formatted name).
    with pytest.raises(PlansetNotConfirmed):
        select_planset(files, "Joseph Michael Dare")


def test_content_confirmation_flags():
    assert confirm_planset_content("JOSEPH DARE 114 N JENNETTE ST", "Joseph Dare",
                                   "114 N Jennette St, Enfield, IL") == []
    flags = confirm_planset_content("WRONG PERSON 999 OTHER", "Joseph Dare", "114 N Jennette St")
    items = {f["item"] for f in flags}
    assert "planset_name_mismatch" in items


# --- pagination (the 20-file cap fix) ---
def test_page_loop_pulls_all_pages_past_20_cap():
    from app.coperniq import CoperniqClient
    ALL = [{"id": 1000 + i, "name": f"f{i}.pdf"} for i in range(35)]
    ALL.append({"id": 99999, "name": "Joseph Dare REVA.pdf"})

    def fake_call(params=None):
        params = params or {}
        if "page" in params:
            size = params.get("limit", 20)
            start = (params["page"] - 1) * size
            return ALL[start:start + size]
        return ALL[:20]

    got = CoperniqClient._page_loop(fake_call, "page", 20, start=1)
    assert len(got) == 36
    assert any("REVA" in f["name"] for f in got)


def test_page_loop_stops_when_param_ignored():
    from app.coperniq import CoperniqClient
    ALL = [{"id": i, "name": f"f{i}.pdf"} for i in range(20)]
    # API ignores page param -> always returns same 20 -> loop must not infinite-loop
    got = CoperniqClient._page_loop(lambda params=None: ALL, "page", 20, start=1)
    assert len(got) == 20

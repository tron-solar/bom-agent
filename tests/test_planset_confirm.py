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


# --- pagination (the page_size/page cap fix; confirmed scheme) ---
def test_list_project_files_pages_through_page_size_cap(monkeypatch):
    from app import coperniq as cq

    # Simulate Coperniq: hard 100/page cap, ?page_size & ?page honored; planset on page 2.
    ALL = [{"id": 10248746 + i, "name": f"f{i}.pdf"} for i in range(140)]
    ALL.append({"id": 10999999, "name": "Joseph Dare REVA.pdf"})  # 141st file -> page 2

    class FakeResp:
        def __init__(self, data): self._d = data
        def raise_for_status(self): pass
        def json(self): return self._d

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None, params=None):
            size = min(params.get("page_size", 100), 100)
            page = params.get("page", 1)
            start = (page - 1) * size
            return FakeResp(ALL[start:start + size])

    monkeypatch.setattr(cq.httpx, "Client", FakeClient)
    client = cq.CoperniqClient(base="http://x", api_key="k")
    files = client.list_project_files("868257")
    assert len(files) == 141
    assert any("REVA" in f["name"] for f in files)


def test_list_project_files_stops_on_repeated_page(monkeypatch):
    from app import coperniq as cq
    ALL = [{"id": i, "name": f"f{i}.pdf"} for i in range(50)]

    class FakeResp:
        def __init__(self, data): self._d = data
        def raise_for_status(self): pass
        def json(self): return self._d

    class FakeClient:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url, headers=None, params=None):
            return FakeResp(ALL[:50])  # ignores page -> always same 50; must not loop forever

    monkeypatch.setattr(cq.httpx, "Client", FakeClient)
    client = cq.CoperniqClient(base="http://x", api_key="k")
    files = client.list_project_files("1")
    assert len(files) == 50  # got the page, detected no-progress, stopped

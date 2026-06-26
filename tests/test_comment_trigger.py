"""Comment-trigger poller tests: phrase matcher (tolerant of mention markup) + persisted dedup +
pipeline reuse. The trigger tokens are PROVISIONAL pending the captured rendered string; these tests
pin the MATCH STRATEGY (mention id + phrase), not a guessed literal.
"""
import os

os.environ.setdefault("FILE_STORAGE_DIR", "/tmp/bom_test_files")

import app.comment_trigger as ct


def test_matches_real_mention_rendered_form():
    # the rendered form Coperniq stores: @mention -> [Name|~id:4679] token + phrase
    html = "<p>[API API User - Christian Guest|~id:4679] Please create BOM</p>"
    assert ct.matches_trigger(html) is True


def test_match_is_case_and_whitespace_tolerant():
    html = "<p>[Christian Guest|~id:4679]&nbsp;&nbsp;please   CREATE bom</p>"
    assert ct.matches_trigger(html) is True


def test_no_match_without_mention():
    assert ct.matches_trigger("<p>Please create BOM</p>") is False


def test_no_match_without_phrase():
    assert ct.matches_trigger("<p>[Christian Guest|~id:4679] please review the design</p>") is False


def test_no_match_wrong_person():
    assert ct.matches_trigger("<p>[Joel Donskey|~id:4622] please create bom</p>") is False


def test_persisted_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "_processed_dir", lambda: str(tmp_path))
    assert ct.comment_already_processed(999) is False
    ct.mark_comment_processed(999, {"status": "done"})
    assert ct.comment_already_processed(999) is True


def test_scan_fires_pipeline_once_then_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(ct, "_processed_dir", lambda: str(tmp_path))
    calls = []

    class FakeResult:
        status, detail = "done", "ok"

    def fake_process(project_id, task_key):
        calls.append((project_id, task_key))
        return FakeResult()

    monkeypatch.setattr(ct.pipeline, "process", fake_process)

    class FakeClient:
        def list_project_comments(self, pid):
            return [
                {"id": 1, "comment": "<p>[Christian Guest|~id:4679] Please create BOM</p>"},
                {"id": 2, "comment": "<p>just a normal note</p>"},
            ]

    fired = ct.scan_project(FakeClient(), 852515)
    assert len(fired) == 1 and fired[0]["comment_id"] == 1
    assert calls == [("852515", "create_bom_comment_1")]      # comment-scoped task_key; process reused
    # second scan: comment 1 already processed -> no re-fire
    fired2 = ct.scan_project(FakeClient(), 852515)
    assert fired2 == [] and len(calls) == 1

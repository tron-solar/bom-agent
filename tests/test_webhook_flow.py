"""Smoke tests for the BOM trigger service.

Run: pytest -q
These stub the engine + Coperniq so they exercise routing, hosting, attach/notify wiring, signature
verification, and idempotency WITHOUT hitting real Coperniq or running the (slow) real engine.
"""
import hashlib
import hmac
import io
import json
import os

os.environ.setdefault("FILE_STORAGE_DIR", "/tmp/bom_test_files")
os.environ.setdefault("PUBLIC_BASE_URL", "http://localhost:8000")
os.environ.setdefault("COPERNIQ_WEBHOOK_SECRET", "")  # dev: skip sig unless a test sets it

import importlib
import openpyxl
from fastapi.testclient import TestClient

from app import config as cfg  # noqa
import app.pipeline as pipeline
import app.main as main
from app.models import ProjectContext, Assignee


def _fake_engine(planset_path, project, master_note_form=None):
    wb = openpyxl.load_workbook(os.path.join("engine", "BOM_TEMPLATE.xlsx"))
    wb["Solar BOM"]["A5"] = 29
    b = io.BytesIO(); wb.save(b)
    conf = {"FLAGS_FOR_HUMAN_REVIEW": [
        {"level": "HARD", "item": "orientation", "msg": "x"},
        {"level": "SOFT", "item": "rails", "msg": "+2"}]}
    return b.getvalue(), conf


class _FakeClient:
    def __init__(self):
        self.files = []
        self.comments = []

    def build_context(self, pid):
        return ProjectContext(project_id=str(pid), number=7802, customer_name="Joseph Woroszylo",
                              address="305 Boyd St, Eldorado, IL 62930", zone="Zone 3",
                              create_bom_assignee=Assignee(id=11695, first_name="Ankurkumar",
                                                           last_name="Suthar", email="a@x.com"),
                              raw={"id": pid, "title": "Joseph Woroszylo",
                                   "phase": {"instanceId": 2854399},  # current phase (NOT Engineering)
                                   "phaseInstances": [
                                       {"id": 2854301, "name": "Initiation", "phaseTemplate": {"id": 2796}},
                                       {"id": 2854306, "name": "Engineering", "phaseTemplate": {"id": 2797}}],
                                   "address": ["305 Boyd St"]})

    def find_planset_file(self, pid, customer_name):
        return {"file": {"id": 1}, "url": "http://x/REVA.pdf",
                "name": f"{customer_name} REVA.pdf", "revision": "A", "diagnostics": {}}
    def get_project_file(self, pid, fid): return {"url": "http://x/REVA.pdf"}

    def create_project_file(self, project_id, url, name, phase_instance_id=None, is_archived=False):
        self.files.append({"url": url, "name": name, "phase": phase_instance_id})
        return {"id": len(self.files)}

    def create_project_comment(self, project_id, body):
        self.comments.append(body)
        return {"id": 1}


def _wire(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(pipeline, "run_engine", _fake_engine)
    monkeypatch.setattr(pipeline, "CoperniqClient", lambda: fake)
    monkeypatch.setattr(pipeline, "download_planset",
                        lambda client, pid, name: {"path": _write_tmp_pdf(), "name": f"{name} REVA.pdf",
                                                   "revision": "A", "diagnostics": {}})
    return fake


def _write_tmp_pdf():
    p = "/tmp/bom_test_fake.pdf"
    with open(p, "wb") as f:
        f.write(b"%PDF-1.4 fake")
    return p


def test_ignores_other_task(monkeypatch):
    _wire(monkeypatch)
    c = TestClient(main.app)
    r = c.post("/webhooks/coperniq/create-bom", json={"project_id": 1, "task_key": "other"})
    assert r.status_code == 200 and r.json()["status"] == "ignored"


def test_full_flow_attaches_and_notifies(monkeypatch):
    fake = _wire(monkeypatch)
    c = TestClient(main.app)
    r = c.post("/webhooks/coperniq/create-bom", json={"project_id": 857222, "task_key": "create_bom"})
    assert r.status_code == 202
    # EXACTLY two files attach: the xlsx + the confidence DOCX (the JSON is generated/hosted but NOT
    # posted). Display names use spaces (not underscores), drop "auto,", keep the "DRAFT — " prefix.
    names = [f["name"] for f in fake.files]
    assert "DRAFT — BOM Joseph Woroszylo (pending review).xlsx" in names
    assert "DRAFT — Confidence Report (BOM Joseph Woroszylo).docx" in names
    assert not any(n.endswith(".json") for n in names)                     # JSON no longer attached
    assert len(fake.files) == 2
    # BOTH files attach to the Engineering phase instance (2854306), NOT the current phase (2854399)
    assert [f["phase"] for f in fake.files] == [2854306, 2854306]
    # assignee @mentioned with flag counts
    assert fake.comments and "[Ankurkumar Suthar|~id:11695]" in fake.comments[0]
    assert "1 hard / 1 soft" in fake.comments[0]
    assert "HARD FLAGS PRESENT" in fake.comments[0]


def test_served_file_roundtrip(monkeypatch):
    fake = _wire(monkeypatch)
    c = TestClient(main.app)
    c.post("/webhooks/coperniq/create-bom", json={"project_id": 999001, "task_key": "create_bom"})
    url = fake.files[0]["url"]
    token, name = url.split("/files/")[1].split("/", 1)
    r = c.get(f"/files/{token}/{name}")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]


def test_idempotent(monkeypatch):
    _wire(monkeypatch)
    c = TestClient(main.app)
    c.post("/webhooks/coperniq/create-bom", json={"project_id": 999002, "task_key": "create_bom"})
    assert pipeline.already_processed("999002", "create_bom")


def test_signature(monkeypatch):
    monkeypatch.setenv("COPERNIQ_WEBHOOK_SECRET", "topsecret")
    importlib.reload(cfg)
    importlib.reload(main)
    fake = _wire(monkeypatch)
    monkeypatch.setattr(main.pipeline, "CoperniqClient", lambda: fake)
    c = TestClient(main.app)
    body = json.dumps({"project_id": 5, "task_key": "create_bom"}).encode()
    bad = c.post("/webhooks/coperniq/create-bom", content=body,
                 headers={"X-Coperniq-Signature": "sha256=bad", "Content-Type": "application/json"})
    assert bad.status_code == 401
    sig = hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    good = c.post("/webhooks/coperniq/create-bom", content=body,
                  headers={"X-Coperniq-Signature": f"sha256={sig}", "Content-Type": "application/json"})
    assert good.status_code == 202
    importlib.reload(cfg); importlib.reload(main)  # reset

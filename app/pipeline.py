"""Orchestration: project trigger -> BOM DRAFT attached + assignee notified.

This module is the glue. It does NOT reimplement the BOM engine — it calls the validated engine
in `engine/` (copy your modules there). The single integration boundary is `run_engine()`, which
must return (xlsx_bytes, confidence_dict). Everything else (download, host, attach, notify) is here.

Idempotency: a processed (project_id, task_key) is recorded on the volume; a duplicate webhook is a
no-op. Failures post a "generation failed, needs human" comment instead of attaching a partial BOM.
"""
from __future__ import annotations
import json
import logging
import os
import tempfile
import traceback
from dataclasses import dataclass

import httpx

from .config import CONFIG
from .coperniq import CoperniqClient
from .hosting import host_bytes
from .models import ProjectContext
from .planset_confirm import PlansetNotConfirmed

log = logging.getLogger("pipeline")

_PROCESSED_DIR = lambda: os.path.join(CONFIG.file_storage_dir, "_processed")


@dataclass
class PipelineResult:
    status: str                 # "done" | "skipped" | "failed"
    detail: str
    file_url: str | None = None
    hard_flags: int = 0
    soft_flags: int = 0


# ---------- idempotency ----------
def _processed_marker(project_id: str, task_key: str) -> str:
    os.makedirs(_PROCESSED_DIR(), exist_ok=True)
    return os.path.join(_PROCESSED_DIR(), f"{project_id}__{task_key}.json")


def already_processed(project_id: str, task_key: str) -> bool:
    return os.path.isfile(_processed_marker(project_id, task_key))


def mark_processed(project_id: str, task_key: str, info: dict) -> None:
    with open(_processed_marker(project_id, task_key), "w") as f:
        json.dump(info, f)


# ---------- planset download ----------
def download_planset(client: CoperniqClient, project_id: str, customer_name: str) -> dict:
    """Confirm + download the planset. Returns {"path","name","revision","diagnostics"}.
    Raises PlansetNotConfirmed (caught upstream -> fail-loud human notify) if unconfirmed."""
    confirmed = client.find_planset_file(project_id, customer_name)
    url = confirmed["url"]
    if not url:
        fid = confirmed["file"].get("id") or confirmed["file"].get("fileId")
        meta = client.get_project_file(project_id, fid)
        url = meta.get("url") or meta.get("downloadUrl")
    if not url:
        raise RuntimeError("Confirmed planset has no resolvable download URL.")
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    with httpx.Client(timeout=120) as c:
        r = c.get(url, follow_redirects=True)
        r.raise_for_status()
        tmp.write(r.content)
    tmp.close()
    return {"path": tmp.name, "name": confirmed["name"], "revision": confirmed["revision"],
            "diagnostics": confirmed["diagnostics"]}


# ---------- master note ----------
def _fetch_master_note_form(client, project_id) -> dict | None:
    """Find the project's 'Master Note' form and fetch its full field layout (formLayouts), which the
    extractor uses for expansion mount/stack resolution. Returns None if there's no such form or the
    forms API errors — never fatal; mount resolution just falls back to its default."""
    try:
        forms = client.list_project_forms(project_id) or []
        mn = next((f for f in forms
                   if str(f.get("name", "")).strip().lower() == "master note"), None)
        if not mn:
            log.info("no 'Master Note' form on project %s", project_id)
            return None
        return client.get_form(mn.get("id"))
    except Exception:
        log.warning("Master Note form fetch failed for %s; proceeding without it",
                    project_id, exc_info=True)
        return None


# ---------- engine boundary ----------
def run_engine(planset_pdf_path: str, project: ProjectContext,
               master_note_form: dict | None = None) -> tuple[bytes, dict]:
    """INTEGRATION BOUNDARY — calls the validated BOM engine in engine/.

    Replace the import/call below with the engine project's orchestrator entry point. It must:
      1. extract planset fields (planset_extractor) + read Coperniq cross-check fields (project.raw)
      2. run racking_engine + all blocks -> row/qty map, cell writes, confidence dict
      3. fill engine/BOM_TEMPLATE.xlsx, recalc via LibreOffice, apply_qty_filter
      4. return (xlsx_bytes, confidence_dict)

    The confidence_dict MUST include a top-level "FLAGS_FOR_HUMAN_REVIEW" list whose items each have
    a "level" of "HARD"/"SOFT"/"INFO" (see AUTONOMY_READINESS_SPEC §B) so notify() can count them.
    """
    try:
        from engine.orchestrator import build_bom  # type: ignore
    except Exception as e:  # engine not wired yet
        raise RuntimeError(
            "engine.orchestrator.build_bom not found — copy the validated engine into engine/ and "
            "expose build_bom(planset_pdf_path, coperniq_project_dict) -> (xlsx_bytes, confidence_dict). "
            f"Import error: {e}"
        )
    xlsx_bytes, confidence = build_bom(planset_pdf_path, project.raw,
                                       master_note_form=master_note_form)
    return xlsx_bytes, confidence


def _count_flags(confidence: dict) -> tuple[int, int]:
    hard = soft = 0
    for fl in confidence.get("FLAGS_FOR_HUMAN_REVIEW", []):
        lvl = str(fl.get("level", "")).upper()
        if lvl == "HARD":
            hard += 1
        elif lvl == "SOFT":
            soft += 1
    return hard, soft


def _safe_name(project: ProjectContext) -> str:
    base = project.customer_name or f"project_{project.project_id}"
    return "BOM_" + "_".join(base.split()) + ".xlsx"


# ---------- notify ----------
def notify_assignee(client: CoperniqClient, project: ProjectContext, file_name: str,
                    hard: int, soft: int) -> None:
    mention = project.create_bom_assignee.mention()
    flagline = f"{hard} hard / {soft} soft confidence flag(s)"
    review = "⚠ HARD FLAGS PRESENT — review required before use." if hard else "Ready for review."
    body = (f"{mention} Auto-generated BOM **DRAFT** is attached: '{file_name}'. "
            f"{flagline}. {review} — generated by the BOM bot (shadow mode).")
    client.create_project_comment(project.project_id, body)


def notify_failure(client: CoperniqClient, project_id: str, assignee_mention: str, err: str) -> None:
    body = (f"{assignee_mention} ⚠ Automated BOM generation FAILED for this project and no file was "
            f"attached. A human needs to build the BOM manually. Error: {err[:500]}")
    try:
        client.create_project_comment(project_id, body)
    except Exception:
        log.exception("Failed to post failure comment")


# ---------- main entry ----------
def process(project_id: str, task_key: str) -> PipelineResult:
    client = CoperniqClient()

    if already_processed(project_id, task_key):
        return PipelineResult("skipped", f"{project_id}/{task_key} already processed")

    # Resolve assignee early so we can notify on failure too.
    try:
        project = client.build_context(project_id)
    except Exception as e:
        log.exception("get_project failed")
        return PipelineResult("failed", f"get_project failed: {e}")

    mention = project.create_bom_assignee.mention()

    # Confirm + download the planset (strict; raises if not confidently the right file).
    try:
        pdf = download_planset(client, project_id, project.customer_name)
        planset_path = pdf["path"]
    except PlansetNotConfirmed as e:
        diag = getattr(e, "diagnostics", {})
        notify_failure(client, project_id, mention,
                       f"planset not confirmed: {e}. PDFs seen: {diag.get('all_pdf_names', [])}")
        return PipelineResult("failed", f"planset not confirmed: {e}")
    except Exception as e:
        notify_failure(client, project_id, mention, f"planset download: {e}")
        return PipelineResult("failed", f"planset download failed: {e}")

    # Fetch the project's "Master Note" form (drives expansion mount/stack resolution). Optional:
    # absent form or a forms-API hiccup -> None, never fails the run.
    master_note_form = _fetch_master_note_form(client, project_id)

    try:
        xlsx_bytes, confidence = run_engine(planset_path, project, master_note_form)
        # record which planset was used + its revision in the confidence report
        confidence.setdefault("planset", {})
        confidence["planset"].update({"file_name": pdf["name"], "revision": pdf["revision"],
                                      "selection_diagnostics": pdf["diagnostics"]})
    except Exception:
        err = traceback.format_exc()
        log.error(err)
        notify_failure(client, project_id, mention, "engine run error (see service logs)")
        return PipelineResult("failed", "engine run failed")
    finally:
        try:
            os.unlink(planset_path)
        except OSError:
            pass

    hard, soft = _count_flags(confidence)
    file_name = _safe_name(project)

    # host the xlsx, attach as DRAFT
    try:
        hosted = host_bytes(xlsx_bytes, file_name)
        stem, ext = os.path.splitext(file_name)            # ext from the real filename, not assumed
        draft_name = f"DRAFT — {stem} (auto, pending review){ext}"
        phase_iid = (project.raw.get("phase") or {}).get("instanceId")
        client.create_project_file(project_id, url=hosted.public_url, name=draft_name,
                                   phase_instance_id=phase_iid)
    except Exception as e:
        log.exception("attach failed")
        notify_failure(client, project_id, mention, f"file attach: {e}")
        return PipelineResult("failed", f"attach failed: {e}")

    # also host the confidence report (optional, attach as a companion)
    try:
        conf_bytes = json.dumps(confidence, indent=2).encode()
        conf_name = file_name.replace("BOM_", "").replace(".xlsx", "_confidence.json")
        conf_hosted = host_bytes(conf_bytes, conf_name)
        _, conf_ext = os.path.splitext(conf_name)          # .json, from the confidence file's own name
        client.create_project_file(project_id, url=conf_hosted.public_url,
                                   name=f"DRAFT — confidence report ({stem}){conf_ext}")
    except Exception:
        log.warning("confidence report attach failed (non-fatal)", exc_info=True)

    # notify
    try:
        notify_assignee(client, project, draft_name, hard, soft)
    except Exception:
        log.exception("notify failed (file is attached; assignee not pinged)")

    mark_processed(project_id, task_key, {"file": hosted.public_url, "hard": hard, "soft": soft})
    return PipelineResult("done", "BOM draft attached and assignee notified",
                          file_url=hosted.public_url, hard_flags=hard, soft_flags=soft)

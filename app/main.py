"""FastAPI entrypoint.

Routes:
  POST /webhooks/coperniq/create-bom  -> verify, enqueue/process the BOM generation
  GET  /files/{token}/{name}          -> serve a hosted file for Coperniq to fetch
  GET  /healthz                       -> health check

The webhook returns 202 immediately and runs the pipeline in a background task so Coperniq's
delivery doesn't time out on the (slow) engine + recalc. If your other handlers use a queue
(RQ/Celery/Railway cron), swap BackgroundTasks for that — the call site is one line.
"""
from __future__ import annotations
import hashlib
import hmac
import logging
import os
import platform

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from .config import CONFIG
from .hosting import resolve_file, gc_expired
from .models import CoperniqWebhook
from . import pipeline

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("main")

app = FastAPI(title="Tron Solar BOM Trigger", version="1.0.0")


@app.on_event("startup")
def _startup():
    removed = gc_expired()
    if removed:
        log.info("startup gc removed %d expired hosted files", removed)


def verify_signature(raw_body: bytes, signature: str | None) -> bool:
    """HMAC-SHA256 of the raw body with the shared secret.
    ALIGN THIS with however your existing Coperniq webhooks sign requests. If they use a plain
    shared-secret header instead, replace this with a constant-time compare of that header."""
    if not CONFIG.coperniq_webhook_secret:
        log.warning("COPERNIQ_WEBHOOK_SECRET unset — skipping signature verification (dev only)")
        return True
    if not signature:
        return False
    expected = hmac.new(CONFIG.coperniq_webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    # accept "sha256=<hex>" or bare hex
    provided = signature.split("=", 1)[-1].strip()
    return hmac.compare_digest(expected, provided)


@app.post("/webhooks/coperniq/create-bom")
async def create_bom_webhook(
    request: Request,
    background: BackgroundTasks,
    x_coperniq_signature: str | None = Header(default=None),
):
    raw = await request.body()
    if not verify_signature(raw, x_coperniq_signature):
        raise HTTPException(status_code=401, detail="bad signature")

    try:
        payload = CoperniqWebhook.model_validate_json(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"bad payload: {e}")

    project_id = payload.resolve_project_id()
    if not project_id:
        raise HTTPException(status_code=400, detail="no project_id in webhook")

    task_key = payload.resolve_task_key(CONFIG.create_bom_task_key)
    # Only act on the create_bom trigger; ignore other automations hitting this route.
    if task_key != CONFIG.create_bom_task_key:
        return JSONResponse({"status": "ignored", "reason": f"task_key={task_key}"}, status_code=200)

    # Run in the background so we can ack fast (engine + recalc is slow).
    background.add_task(_run, project_id, task_key)
    return JSONResponse({"status": "accepted", "project_id": project_id, "task_key": task_key},
                        status_code=202)


def _run(project_id: str, task_key: str):
    try:
        result = pipeline.process(project_id, task_key)
        log.info("pipeline %s/%s -> %s: %s", project_id, task_key, result.status, result.detail)
    except Exception:
        log.exception("pipeline crashed for %s/%s", project_id, task_key)


@app.get("/files/{token}/{name}")
def serve_file(token: str, name: str):
    found = resolve_file(token, name)
    if not found:
        raise HTTPException(status_code=404, detail="not found or expired")
    path, meta = found
    media = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
             if name.endswith(".xlsx") else "application/octet-stream")
    return FileResponse(path, media_type=media, filename=meta.get("name", name))


@app.get("/healthz")
def healthz():
    # Readiness for live DRAFT posting — booleans + non-secret values only (never key values), so this
    # can be curled against the deployed service to verify Railway env without leaking secrets.
    pub = CONFIG.public_base_url
    return {
        "ok": True,
        "shadow_mode": CONFIG.shadow_mode,
        "public_base_url": pub,
        "public_base_url_has_https": pub.startswith("https://"),  # MUST be True for working file links
        "has_anthropic_key": bool(CONFIG.anthropic_api_key),
        "has_coperniq_key": bool(CONFIG.coperniq_api_key),
        "has_webhook_secret": bool(CONFIG.coperniq_webhook_secret),
        "claude_model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6 (default)"),
        "create_bom_task_key": CONFIG.create_bom_task_key,
        "file_storage_dir": CONFIG.file_storage_dir,
        "python_version": platform.python_version(),
    }

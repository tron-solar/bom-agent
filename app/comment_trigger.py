"""Comment-trigger poller — a SECOND entry point into the EXISTING create_bom pipeline.

Coperniq has NO comment webhook (Automations fire only on stage / SLA / work-order-status — confirmed),
so we POLL:

    scheduled job -> list_project_comments on active-BOM projects -> strip HTML -> exact-match the
    trigger phrase -> pipeline.process(...)  (DRAFT xlsx + flagged confidence + @-mention; NEVER
    completes the work order, NEVER replaces line items)

Dedup is on the comment id and PERSISTED (same marker pattern as pipeline.already_processed) so a
trigger comment never double-fires across restarts. pipeline.process is reused UNCHANGED — this module
only decides WHEN to call it.

Run as a scheduled job (e.g. Railway cron):  python -m app.comment_trigger
"""
from __future__ import annotations
import os
import re
import html
import json
import logging

from .config import CONFIG
from .coperniq import CoperniqClient
from . import pipeline

log = logging.getLogger("comment_trigger")

# --- TRIGGER MATCH ----------------------------------------------------------------------------------
# Trigger comment: "@API API User - Christian Guest Please create BOM". In Coperniq the @mention is
# stored in the comment HTML as a MENTION TOKEN "[Name|~id:4679]" (Christian Guest = id 4679), NOT as
# literal "@..." text (see list_project_comments output). Per the agreed strategy we match on the
# MENTION ID + the phrase, tolerant of surrounding mention markup / whitespace, case-insensitive.
#
# PROVISIONAL: TRIGGER_MENTION_ID / TRIGGER_PHRASE are confirmed against the REAL captured rendered
# comment before production use (tools/capture_trigger_comment.py). Both are env-overridable so the
# exact tokens can be locked without a code change.
TRIGGER_MENTION_ID = os.environ.get("BOM_COMMENT_TRIGGER_MENTION_ID", "4679")          # Christian Guest
TRIGGER_PHRASE = os.environ.get("BOM_COMMENT_TRIGGER_PHRASE", "please create bom").lower()


def _strip_html(s: str) -> str:
    """Comment HTML -> plain text: drop tags, unescape entities, collapse whitespace. Mention tokens
    like '[Christian Guest|~id:4679]' are NOT HTML tags, so they survive into the text."""
    txt = re.sub(r"<[^>]+>", " ", s or "")
    txt = html.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def matches_trigger(comment_html: str) -> bool:
    """True iff the comment is the BOM trigger: it @-mentions Christian Guest (~id:<ID>) AND contains
    the trigger phrase. Tolerant of mention markup / whitespace; phrase match is case-insensitive."""
    raw = comment_html or ""
    text = _strip_html(raw).lower()
    has_mention = (f"~id:{TRIGGER_MENTION_ID}" in raw) or (f"~id:{TRIGGER_MENTION_ID}" in text)
    has_phrase = TRIGGER_PHRASE in text
    return has_mention and has_phrase


# --- persisted per-comment dedup (mirrors pipeline.already_processed) --------------------------------
def _processed_dir() -> str:
    return os.path.join(CONFIG.file_storage_dir, "_processed_comments")


def _comment_marker(comment_id) -> str:
    os.makedirs(_processed_dir(), exist_ok=True)
    return os.path.join(_processed_dir(), f"{comment_id}.json")


def comment_already_processed(comment_id) -> bool:
    return os.path.isfile(_comment_marker(comment_id))


def mark_comment_processed(comment_id, info: dict) -> None:
    with open(_comment_marker(comment_id), "w") as f:
        json.dump(info, f)


# --- scan + fire ------------------------------------------------------------------------------------
def scan_project(client: CoperniqClient, project_id) -> list[dict]:
    """Scan ONE project's comments; fire the pipeline once per NEW matching trigger comment.
    Returns [{comment_id, project_id, status, detail}] for comments that fired."""
    fired: list[dict] = []
    try:
        comments = client.list_project_comments(project_id) or []
    except Exception as e:  # noqa: BLE001 — one bad project must not stop the poll
        log.warning("list_project_comments(%s) failed: %s", project_id, e)
        return fired
    for c in comments:
        cid = c.get("id")
        body = c.get("comment") or c.get("body") or ""
        if cid is None or comment_already_processed(cid) or not matches_trigger(body):
            continue
        # Comment-scoped task_key so pipeline.process treats each trigger comment as a fresh DRAFT job
        # (its own idempotency marker) — it won't collide with the webhook's "create_bom" marker and
        # won't be skipped if a BOM was already generated. pipeline.process itself is UNCHANGED.
        task_key = f"create_bom_comment_{cid}"
        log.info("comment trigger matched: project=%s comment=%s -> pipeline.process", project_id, cid)
        try:
            res = pipeline.process(str(project_id), task_key)
            status, detail = res.status, res.detail
        except Exception as e:  # noqa: BLE001
            status, detail = "error", str(e)
            log.exception("pipeline.process failed for comment trigger %s/%s", project_id, cid)
        mark_comment_processed(cid, {"project_id": project_id, "task_key": task_key,
                                     "status": status, "detail": detail})
        fired.append({"comment_id": cid, "project_id": project_id, "status": status, "detail": detail})
    return fired


def active_bom_project_ids(client: CoperniqClient) -> list[str]:
    """Scan set = the active-BOM work-order projects. Source, in priority:
      1. BOM_POLL_PROJECT_IDS env (comma-separated) — explicit, SAFEST for a controlled rollout.
      2. else recently-updated projects (BOM_POLL_UPDATED_AFTER ISO date) as a fallback — a superset;
         the strict trigger match + per-comment dedup make over-scanning safe but heavier.
    Returns [] (logs a warning) if neither is configured, so the poller is a no-op until set."""
    explicit = os.environ.get("BOM_POLL_PROJECT_IDS", "").strip()
    if explicit:
        return [p.strip() for p in explicit.split(",") if p.strip()]
    updated_after = os.environ.get("BOM_POLL_UPDATED_AFTER", "").strip() or None
    if updated_after:
        return [str(p.get("id")) for p in client.list_projects(updated_after=updated_after) if p.get("id")]
    log.warning("comment-trigger poller has no scan source: set BOM_POLL_PROJECT_IDS (recommended) or "
                "BOM_POLL_UPDATED_AFTER. No projects scanned.")
    return []


def poll(project_ids=None) -> list[dict]:
    """Poll the given project IDs (or the configured active-BOM set) once. Returns the fired list."""
    client = CoperniqClient()
    ids = project_ids if project_ids is not None else active_bom_project_ids(client)
    fired: list[dict] = []
    for pid in ids:
        fired.extend(scan_project(client, pid))
    if fired:
        log.info("comment-trigger poll fired %d BOM run(s): %s", len(fired), fired)
    return fired


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = poll()
    print(json.dumps(result, indent=2))

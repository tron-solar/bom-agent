#!/usr/bin/env python3
"""Local DRY-RUN for the create_bom pipeline — no HTTP, no Coperniq writes.

Runs the REAL pipeline path (build_context -> confirm+download planset -> engine -> host -> attach
-> notify) against a real project, but INTERCEPTS the two Coperniq write calls (create_project_file,
create_project_comment) so nothing is written back to Coperniq. The generated DRAFT xlsx is written
to a local folder and the review/hold comment text is printed.

What is REAL vs intercepted:
  REAL   : Coperniq READS (get_project, list_project_files, get_project_file, download planset PDF)
           and the Anthropic Vision extraction (costs tokens; needs ANTHROPIC_API_KEY).
  LOCAL  : the DRAFT xlsx is hosted to --out (a local dir), not a Railway volume.
  STUBBED: create_project_file / create_project_comment / update_project_work_order -> printed, not sent.
           Idempotency markers are disabled so you can re-run freely.

Usage:
    python tools/dry_run_bom.py 868257
    python tools/dry_run_bom.py 868257 --task-key create_bom --out ./dry_run_out

Env (read-only): COPERNIQ_API_KEY + COPERNIQ_API_BASE (reads), ANTHROPIC_API_KEY (extraction).
Loads the repo .env automatically if present. COPERNIQ_WEBHOOK_SECRET is irrelevant here (no HTTP).
"""
from __future__ import annotations
import argparse
import glob
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_dotenv(path: pathlib.Path) -> None:
    """Minimal .env loader (the app itself reads os.environ; locally we populate it from .env)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    ap = argparse.ArgumentParser(description="Dry-run the create_bom pipeline with no Coperniq writes.")
    ap.add_argument("project_id", help="Coperniq project id, e.g. 868257")
    ap.add_argument("--task-key", default="create_bom")
    ap.add_argument("--out", default=str(ROOT / "dry_run_out"), help="local dir for the DRAFT xlsx")
    args = ap.parse_args()

    # Comment bodies contain non-ASCII (e.g. the ⚠ flag marker); the Windows console defaults to
    # cp1252 and would crash on print(). Force UTF-8 with a safe fallback.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    _load_dotenv(ROOT / ".env")
    out_dir = os.path.abspath(args.out)
    os.makedirs(out_dir, exist_ok=True)
    # Host the DRAFT locally; never to a Railway volume. Must be set BEFORE importing app.config.
    os.environ["FILE_STORAGE_DIR"] = out_dir
    os.environ.setdefault("PUBLIC_BASE_URL", "http://dry-run.local")
    sys.path.insert(0, str(ROOT))

    if not os.environ.get("COPERNIQ_API_KEY"):
        print("ERROR: COPERNIQ_API_KEY not set (needed for reads). Add it to .env or the environment.")
        return 2
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("WARNING: ANTHROPIC_API_KEY not set — extraction will fail and you'll see the "
              "'generation failed, needs human' comment path (still a useful dry-run).\n")

    from app import pipeline
    from app.coperniq import CoperniqClient

    client = CoperniqClient()  # real READ client (uses COPERNIQ_API_BASE/_KEY from env)
    captured: dict = {"files": [], "comments": []}

    def _fake_create_file(project_id, url, name, phase_instance_id=None, is_archived=False):
        captured["files"].append({"url": url, "name": name, "phase": phase_instance_id})
        print(f"\n[DRY-RUN] WOULD create_project_file:\n  name = {name!r}\n  url  = {url}"
              f"\n  phaseInstanceId = {phase_instance_id}")
        return {"id": 0, "dryRun": True}

    def _fake_create_comment(project_id, body):
        captured["comments"].append(body)
        print(f"\n[DRY-RUN] WOULD create_project_comment:\n{'-' * 64}\n{body}\n{'-' * 64}")
        return {"id": 0, "dryRun": True}

    # Intercept the writes on this instance; reads keep their real implementations.
    client.create_project_file = _fake_create_file
    client.create_project_comment = _fake_create_comment
    client.update_project_work_order = lambda *a, **k: {"dryRun": True}

    # Make pipeline.process build OUR instrumented client, and disable idempotency for re-runs.
    pipeline.CoperniqClient = lambda: client
    pipeline.already_processed = lambda pid, tk: False
    pipeline.mark_processed = lambda pid, tk, info: None

    print(f"[DRY-RUN] project={args.project_id} task={args.task_key}")
    print(f"[DRY-RUN] out={out_dir}")
    print("[DRY-RUN] Coperniq WRITES intercepted; READS + Anthropic extraction are REAL.\n")

    result = pipeline.process(args.project_id, args.task_key)

    print(f"\n[DRY-RUN] result: {result.status} — {result.detail}")
    print(f"[DRY-RUN] flags: {result.hard_flags} hard / {result.soft_flags} soft")
    xlsx = sorted(glob.glob(os.path.join(out_dir, "**", "*.xlsx"), recursive=True))
    print(f"[DRY-RUN] DRAFT xlsx on disk: {xlsx or '(none — see the comment above for why)'}")
    print(f"[DRY-RUN] {len(captured['files'])} file(s) + {len(captured['comments'])} comment(s) "
          f"would have been sent to Coperniq.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Controlled REAL-WRITE path for ONE named project (DRAFT-only, shadow).

Runs the EXISTING pipeline (app.pipeline.process) which:
  - attaches the DRAFT xlsx via create_project_file (phaseInstanceId set),
  - attaches the confidence JSON via create_project_file,
  - posts an @-mention comment via create_project_comment,
  - NEVER completes the work order, NEVER calls replace_project_line_items.

DEFAULT = PREVIEW: writes are intercepted and the EXACT create_project_file /
create_project_comment payloads are printed; nothing is posted. Reads (get_project,
find_planset, download) and the engine run are REAL so the payloads are real.

DELIBERATE LIVE SWITCH (must set BOTH): LIVE_POST=1 and pass --confirm:
    LIVE_POST=1 python tools/live_post.py 852515 --confirm
Without both, it stays in preview and posts nothing.
"""
import os
import sys
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

for line in (ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

import logging
logging.basicConfig(level=logging.WARNING)

import app.pipeline as pipeline                      # noqa: E402
from app.coperniq import CoperniqClient              # noqa: E402


def main():
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        print("usage: [LIVE_POST=1] python tools/live_post.py <project_id> [--confirm]")
        return 2
    pid = sys.argv[1]
    confirmed = os.environ.get("LIVE_POST") == "1" and "--confirm" in sys.argv

    # Hard guard: this path must NEVER complete a work order or replace line items. The pipeline
    # doesn't call them, but trip-wire them so a future change can't silently start.
    def _blocked(name):
        def _f(self, *a, **k):
            raise RuntimeError(f"{name} is BLOCKED in live_post (DRAFT-only, shadow): {a} {k}")
        return _f
    for attr in ("replace_project_line_items", "complete_work_order", "update_project_work_order"):
        if hasattr(CoperniqClient, attr):
            setattr(CoperniqClient, attr, _blocked(attr))

    if not confirmed:
        captured = []

        def fake_file(self, project_id, url, name, phase_instance_id=None, is_archived=False):
            body = {"url": url, "name": name, "isArchived": is_archived}
            if phase_instance_id is not None:
                body["phaseInstanceId"] = phase_instance_id
            captured.append((f"POST /v1/projects/{project_id}/files", body))
            return {"id": f"preview-{len(captured)}"}

        def fake_comment(self, project_id, body):
            captured.append((f"POST /v1/projects/{project_id}/comments", {"body": body}))
            return {"id": "preview-comment"}

        CoperniqClient.create_project_file = fake_file
        CoperniqClient.create_project_comment = fake_comment
        print(f"=== PREVIEW (NOT firing) — project {pid}. Writes intercepted; reads + engine real. ===")
        res = pipeline.process(pid, "create_bom")
        print(f"\nresult: {res.status} — {res.detail}  | hard={res.hard_flags} soft={res.soft_flags}")
        print(f"\n--- EXACT payloads that WOULD be sent ({len(captured)}) ---")
        for endpoint, body in captured:
            print(f"\n{endpoint}")
            print(json.dumps(body, indent=2))
        print("\n[nothing posted] To fire for real: LIVE_POST=1 python tools/live_post.py "
              f"{pid} --confirm")
        return 0

    print(f"=== LIVE POST — project {pid}: DRAFT xlsx + confidence + @-mention WILL be posted to "
          f"Coperniq (no WO completion, no line-items). ===")
    res = pipeline.process(pid, "create_bom")
    print(f"\nresult: {res.status} — {res.detail}  | hard={res.hard_flags} soft={res.soft_flags} "
          f"| file_url={res.file_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Read back a project's comments and print the EXACT rendered HTML of any comment that looks like the
BOM trigger ("create bom" appears, case-insensitive). Use this AFTER posting the real trigger comment
to capture the exact rendered form (the @mention renders as a [Name|~id:N] token, not literal text),
so the matcher tokens can be confirmed before finalizing. Read-only; posts nothing.

Usage:  python tools/capture_trigger_comment.py <project_id>
"""
import os
import sys
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
for line in (ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

from app.coperniq import CoperniqClient            # noqa: E402
from app.comment_trigger import matches_trigger, _strip_html  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("usage: python tools/capture_trigger_comment.py <project_id>")
        return 2
    pid = sys.argv[1]
    comments = CoperniqClient().list_project_comments(pid) or []
    print(f"{len(comments)} comments on project {pid}. Candidates containing 'create bom':\n")
    hits = 0
    for c in comments:
        body = c.get("comment") or c.get("body") or ""
        if "create bom" in _strip_html(body).lower():
            hits += 1
            print("=" * 78)
            print(f"comment id : {c.get('id')}")
            print(f"author     : {(c.get('createdByUser') or {})}")
            print(f"createdAt  : {c.get('createdAt')}")
            print(f"RAW HTML   : {body!r}")
            print(f"stripped   : {_strip_html(body)!r}")
            print(f"matches_trigger() -> {matches_trigger(body)}")
    if not hits:
        print("(no comment contains 'create bom' — post the trigger comment, then re-run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

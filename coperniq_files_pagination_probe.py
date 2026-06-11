#!/usr/bin/env python3
"""
Coperniq files pagination diagnostic.

RESULT (confirmed against live API, project 868257):
  The paging params are `page` + `page_size`. Coperniq enforces a HARD MAX of 100 files/page
  (?page_size=200 returned 100; ?page=2 returned a distinct set). The single-call default returns
  only the first 20, which is why manually-uploaded plansets (newer/higher id, on later pages)
  were invisible. FIX (live in app/coperniq.py): loop ?page=N&page_size=100 until a short page.
  This script is kept for re-verification if Coperniq changes the scheme.

WHY: GET /v1/projects/{id}/files returns a small first page by default. The Coperniq docs list NO
pagination params for this endpoint, so this probes the common ones and reports which works.

SECURITY: pass your key via env var. Never hard-code it. Nothing here logs the key.
    export COPERNIQ_API_KEY="...your key..."
    python3 coperniq_files_pagination_probe.py 868257

It prints ONLY counts + filenames so the output is safe to share back.
"""
import os
import sys
import json
import urllib.request
import urllib.parse

BASE = "https://api.coperniq.io/v1"
API_KEY = os.environ.get("COPERNIQ_API_KEY")
PROJECT_ID = sys.argv[1] if len(sys.argv) > 1 else "868257"
# The filename we expect to find once pagination works (adjust per project):
EXPECT_SUBSTR = os.environ.get("EXPECT_SUBSTR", "REV")  # e.g. "Dare REV" / "Woroszylo"


def _get(path, params=None):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"x-api-key": API_KEY})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def names(data):
    if isinstance(data, dict):  # some APIs wrap in {items:[...], meta:{...}}
        for k in ("items", "data", "files", "results"):
            if k in data:
                return data[k], data
        return [], data
    return data, None


def summarize(label, data):
    items, wrapper = names(data)
    n = len(items)
    has = any(EXPECT_SUBSTR.lower() in (f.get("name", "").lower()) for f in items)
    ids = [f.get("id") for f in items if isinstance(f.get("id"), int)]
    meta = ""
    if wrapper:
        meta = " | wrapper keys: " + ",".join(k for k in wrapper if k not in ("items", "data", "files", "results"))
    print(f"[{label}] count={n} planset_found={has}"
          f" id_range={min(ids) if ids else '-'}..{max(ids) if ids else '-'}{meta}")
    return n, has, items


def main():
    if not API_KEY:
        sys.exit("Set COPERNIQ_API_KEY env var first.")
    path = f"/projects/{PROJECT_ID}/files"

    print(f"Probing {path} (expecting a file whose name contains {EXPECT_SUBSTR!r})\n")

    # 0) baseline
    base_n, base_has, _ = summarize("baseline (no params)", _get(path))

    # 1) common page-size params
    for p in ({"limit": 200}, {"pageSize": 200}, {"page_size": 200}, {"per_page": 200}, {"size": 200}):
        try:
            summarize(f"?{list(p)[0]}=200", _get(path, p))
        except Exception as e:
            print(f"[?{list(p)[0]}=200] ERROR {e}")

    # 2) page 2 under common page params
    for p in ({"page": 2}, {"page": 2, "limit": 20}, {"offset": 20}, {"offset": 20, "limit": 20},
              {"skip": 20}, {"cursor": ""}):
        try:
            n, has, items = summarize(f"?{urllib.parse.urlencode(p)}", _get(path, p))
            if has:
                print("    ^^^ PLANSET FOUND ON THIS PAGE — note these params.")
        except Exception as e:
            print(f"[?{urllib.parse.urlencode(p)}] ERROR {e}")

    print("\nINTERPRETATION:")
    print("- If a ?...=200 call returns >20 -> that page-size param works; use it (loop if still capped).")
    print("- If a page=2 / offset=20 call returns a DIFFERENT set (and the planset) -> that's the")
    print("  pagination scheme; loop pages until an empty/short page.")
    print("- If NOTHING exceeds 20 and no page-2 differs -> the cap may be hard; contact Coperniq")
    print("  support asking how to page /projects/{id}/files (or use a FILE-type property for the planset).")


if __name__ == "__main__":
    main()

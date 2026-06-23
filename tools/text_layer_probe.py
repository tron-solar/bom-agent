#!/usr/bin/env python3
"""Diagnostic (NOT wired into the pipeline): does a planset PDF carry an extractable text layer?

Downloads the planset the pipeline already fetches and, for the given pages, dumps the PyMuPDF
word/bbox text layer and greps it for the fine-text tokens Vision struggled with (breaker ratings,
harness P/N, equipment counts). If these exist as exact vector text we can read them deterministically
instead of doing low-res image reads.

Usage:
    python tools/text_layer_probe.py                 # Lackey #852515, PV-5=page 5, PV-3=page 2
    python tools/text_layer_probe.py 852515 5 2
"""
from __future__ import annotations
import os
import sys
import pathlib
import tempfile

import httpx
import fitz  # PyMuPDF

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOKENS = ["1875157", "1807000", "BUS-KIT", "BUSKIT", "CSR", "POWERWALL", "60A", "100A", "200A"]


def _load_dotenv(path: pathlib.Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _download_planset(project_id: str) -> str:
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("ANTHROPIC_API_KEY", "unused-here")
    from app.coperniq import CoperniqClient
    cli = CoperniqClient()
    title = cli.get_project(project_id).get("title")
    conf = cli.find_planset_file(project_id, title)
    url = conf.get("url") or cli.get_project_file(project_id, conf["file"].get("id")).get("downloadUrl")
    out = os.path.join(tempfile.mkdtemp(), "planset.pdf")
    with httpx.Client(timeout=120) as c:
        r = c.get(url, follow_redirects=True)
        r.raise_for_status()
        with open(out, "wb") as f:
            f.write(r.content)
    print(f"planset: {conf.get('name')}  ->  {out}")
    return out


def probe_page(doc, idx: int, sheet: str) -> None:
    page = doc[idx]
    words = page.get_text("words")  # (x0, y0, x1, y1, word, block, line, word_no)
    print(f"\n{'='*78}\n{sheet}  (page index {idx})  —  {page.rect.width:.0f} x {page.rect.height:.0f} pts")
    print(f"  text-layer word count: {len(words)}  "
          f"({'REAL text layer' if len(words) > 50 else 'FLATTENED IMAGE (no usable text)'})")
    full = page.get_text() or ""
    for tok in TOKENS:
        n = full.upper().count(tok)
        if n:
            print(f"  token {tok!r}: {n} occurrence(s) in full text")
    print(f"  --- matching words (x, y) ---")
    any_match = False
    for x0, y0, x1, y1, w, *_ in words:
        u = w.upper()
        if any(t in u for t in TOKENS):
            any_match = True
            print(f"    x={x0:6.0f} y={y0:6.0f}   {w!r}")
    if not any_match:
        print("    (no token matched as a discrete word)")


def main() -> int:
    project_id = sys.argv[1] if len(sys.argv) > 1 else "852515"
    pv5 = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    pv3 = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    _load_dotenv(ROOT / ".env")
    if not os.environ.get("COPERNIQ_API_KEY"):
        print("ERROR: COPERNIQ_API_KEY not set (needed to download the planset).")
        return 2
    pdf = _download_planset(project_id)
    doc = fitz.open(pdf)
    print(f"total pages: {doc.page_count}")
    probe_page(doc, pv5, "PV-5 (electrical one-line)")
    probe_page(doc, pv3, "PV-3 (roof plan / racking BOM)")
    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

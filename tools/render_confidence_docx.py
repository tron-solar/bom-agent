#!/usr/bin/env python3
"""Render a confidence JSON -> Word (.docx) for eyeballing the flags table + 'What to check' column.
Read-only; touches no Coperniq state.

Usage:  python tools/render_confidence_docx.py <path-to-confidence.json> [out.docx]
"""
import sys
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.confidence_docx import render_confidence_docx  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print("usage: python tools/render_confidence_docx.py <confidence.json> [out.docx]")
        return 2
    src = pathlib.Path(sys.argv[1])
    out = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".docx")
    confidence = json.loads(src.read_text(encoding="utf-8"))
    out.write_bytes(render_confidence_docx(confidence))
    print(f"wrote {out}  ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

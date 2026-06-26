#!/usr/bin/env python3
"""DIAGNOSTIC (not wired): per-plane spatial-read scaffolding for ROOF mounts.

Shared read shape (serves BOTH orientation and strings-per-plane):
  1. locate each roof plane's region on the routed string page (PV-3.1 for roof) via Vision,
  2. rotate that plane crop to south (pil_angle = azimuth-180) -> high-res rotated raster,
  3. run the per-plane Vision feature read 2-3x, reconcile, carry a per-plane confidence + variance.

This run implements the STRINGS-PER-PLANE feature read (count distinct string-colors landing on each
plane) and reconciles the per-plane counts against the text-layer total. It writes the rotated crops
and prints the multi-read variance so we can judge whether string-tracing is reliable on a hip roof
BEFORE wiring it into f_jboxes. Nothing here touches the pipeline or the live project.

Usage:  python tools/plane_spatial_probe.py [project_id]   (default Morrow 913411)
"""
from __future__ import annotations
import os
import sys
import json
import asyncio
import pathlib
import tempfile
import statistics
from collections import Counter

import httpx
import fitz
from PIL import Image

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _load_env():
    for line in (ROOT / ".env").read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()
from app.coperniq import CoperniqClient                  # noqa: E402
from engine.extractor import PlansetExtractor             # noqa: E402
from engine.racking_engine import rotate_to_south         # noqa: E402

OUTDIR = ROOT / "dry_run_out" / "plane_probe"
N_READS = 3                      # multi-read count per plane
CONF_VARIANCE_THRESHOLD = 1      # max-min spread across reads that we still trust (<=1 -> OK)


# ---------------- shared scaffolding ----------------
class PlaneSpatialReader:
    """Locate -> rotate-to-south -> multi-read + reconcile, per roof plane. Feature-agnostic: the
    per-plane feature read is injected (here: distinct string-color count)."""

    def __init__(self, ext: PlansetExtractor):
        self.ext = ext

    def render_page(self, pdf_path: str, page_index: int, zoom: float = 4.0) -> Image.Image:
        """Render the page as displayed (respects /Rotate) to a high-res PIL image."""
        doc = fitz.open(pdf_path)
        try:
            pix = doc[page_index].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        finally:
            doc.close()
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    async def locate_planes(self, page_img: Image.Image) -> list:
        """Vision: bounding box (normalized 0-1) of each roof plane's module region on the string map."""
        b64 = self.ext._pil_to_b64(page_img) if hasattr(self.ext, "_pil_to_b64") else _pil_b64(page_img)
        prompt = (
            "This is a Tron Solar STRING MAP (PV-3.1) for a multi-plane roof PV system. Each roof plane "
            "(Roof #1, #2, ...) is drawn as a separate block of solar modules with dashed colored string "
            "lines routed across it. Return ONLY JSON: "
            '{"planes":[{"label":"Roof #1","bbox":[x0,y0,x1,y1]}]} where bbox is the NORMALIZED (0..1) '
            "bounding box of that plane's MODULE block (top-left x0,y0; bottom-right x1,y1). Include every "
            "distinct roof plane that has modules. EXCLUDE the legend, title block, and equipment one-line.")
        raw = await self.ext._call_claude([b64], prompt)
        try:
            return self.ext._parse_json(raw).get("planes", []) or []
        except Exception:
            return []

    def rotated_crop(self, page_img: Image.Image, bbox, azimuth, label) -> tuple[Image.Image, float]:
        """Crop the plane region and rotate to south (pil_angle = azimuth-180). Returns (img, pil_angle)."""
        W, H = page_img.size
        x0, y0, x1, y1 = bbox
        # pad 4% so a slightly-tight Vision bbox doesn't clip the plane's strings
        px, py = 0.04 * W, 0.04 * H
        box = (max(0, int(x0 * W - px)), max(0, int(y0 * H - py)),
               min(W, int(x1 * W + px)), min(H, int(y1 * H + py)))
        crop = page_img.crop(box)
        # upscale small crops so dashed lines stay legible after rotation
        if max(crop.size) < 900:
            s = 900 / max(crop.size)
            crop = crop.resize((int(crop.width * s), int(crop.height * s)), Image.LANCZOS)
        rot = rotate_to_south(crop, azimuth)   # reuse engine scaffolding; pil_angle = azimuth-180
        return rot.img, rot.pil_angle

    async def read_string_colors(self, plane_img: Image.Image) -> dict:
        """One Vision read: distinct string-color count on this plane crop."""
        b64 = _pil_b64(plane_img)
        prompt = (
            "This is a cropped, rotated view of ONE roof plane from a solar string map. Solar strings are "
            "drawn as DASHED COLORED lines routed across the modules; each DISTINCT color is a separate "
            "string. Count how many DISTINCT colored dashed string-lines appear on THIS plane. Return ONLY "
            'JSON: {"distinct_string_colors": <integer>, "colors": ["red","blue",...]}.')
        raw = await self.ext._call_claude([b64], prompt)
        try:
            d = self.ext._parse_json(raw)
            return {"count": int(d.get("distinct_string_colors") or 0), "colors": d.get("colors") or []}
        except Exception:
            return {"count": None, "colors": []}

    async def multi_read(self, plane_img: Image.Image, n=N_READS) -> dict:
        """Run the feature read n times; reconcile (mode) + variance + confidence."""
        reads = [await self.read_string_colors(plane_img) for _ in range(n)]
        counts = [r["count"] for r in reads if isinstance(r["count"], int)]
        if not counts:
            return {"reads": reads, "reconciled": None, "variance": None, "confidence": 0.0}
        mode = Counter(counts).most_common(1)[0][0]
        agree = sum(1 for c in counts if c == mode)
        variance = max(counts) - min(counts)
        return {"reads": reads, "counts": counts, "reconciled": mode,
                "variance": variance, "agree": f"{agree}/{len(counts)}",
                "confidence": round(agree / len(counts), 2),
                "stdev": round(statistics.pstdev(counts), 2) if len(counts) > 1 else 0.0}


def _pil_b64(img: Image.Image) -> str:
    import io
    import base64
    b = io.BytesIO()
    img.save(b, format="PNG")
    return base64.standard_b64encode(b.getvalue()).decode("utf-8")


def fetch_planset(pid: str) -> str:
    cli = CoperniqClient()
    title = cli.get_project(pid).get("title")
    conf = cli.find_planset_file(pid, title)
    url = conf.get("url") or cli.get_project_file(pid, conf["file"].get("id")).get("downloadUrl")
    out = os.path.join(tempfile.mkdtemp(), "planset.pdf")
    with httpx.Client(timeout=180) as c:
        r = c.get(url, follow_redirects=True)
        r.raise_for_status()
        open(out, "wb").write(r.content)
    return out


async def main(pid: str):
    OUTDIR.mkdir(parents=True, exist_ok=True)
    ext = PlansetExtractor()
    pdf = fetch_planset(pid)

    pv3 = ext._find_page_by_label(pdf, "PV-3")
    pv31 = ext._find_page_by_label(pdf, "PV-3.1")
    mount = ext._detect_mount_type(pdf, pv3, ext._page_text(pdf, 0))
    string_page = pv31 if pv31 is not None else pv3
    print(f"project={pid}  mount={mount}  PV-3={pv3}  PV-3.1={pv31}  string_page={string_page}")

    # text-layer strings (ground truth set + total) for the reconcile check
    strings = ext._parse_strings_text(ext._page_text(pdf, string_page))
    text_total = len(strings)
    print(f"text strings: {strings}  total={text_total}")

    # plane azimuths from the PV-3 roof-plan read
    b64 = ext._pdf_page_to_base64(pdf, pv3, label="PV-3")
    arr = await ext._extract_roof_plan(b64, 415)
    arrays = arr.get("arrays", [])
    az_by_label = {f"Roof #{i}": a.get("azimuth") for i, a in enumerate(arrays, 1)}
    mod_by_label = {f"Roof #{i}": a.get("module_count") for i, a in enumerate(arrays, 1)}
    print(f"planes (azimuth/modules): {[(k, az_by_label[k], mod_by_label[k]) for k in az_by_label]}")

    reader = PlaneSpatialReader(ext)
    page_img = reader.render_page(pdf, string_page, zoom=4.0)
    page_img.save(OUTDIR / "PV-3.1_full.png")
    print(f"\nrendered string page -> {OUTDIR / 'PV-3.1_full.png'}  ({page_img.size[0]}x{page_img.size[1]})")

    located = await reader.locate_planes(page_img)
    print(f"Vision located {len(located)} plane region(s): {[p.get('label') for p in located]}")

    results = []
    for p in located:
        label = p.get("label", "?")
        bbox = p.get("bbox")
        az = az_by_label.get(label)
        if bbox is None or az is None:
            print(f"\n[{label}] SKIP — bbox={bbox} az={az} (no azimuth match from PV-3)")
            continue
        crop_img, pil_angle = reader.rotated_crop(page_img, bbox, az, label)
        fn = OUTDIR / f"plane_{label.replace(' ', '_').replace('#', '')}_rot.png"
        crop_img.save(fn)
        mr = await reader.multi_read(crop_img)
        results.append({"label": label, "az": az, "modules": mod_by_label.get(label),
                        "pil_angle": pil_angle, "crop": str(fn), **mr})
        print(f"\n[{label}] az={az} modules={mod_by_label.get(label)} pil_angle={pil_angle:.0f} "
              f"crop={fn.name} ({crop_img.size[0]}x{crop_img.size[1]})")
        print(f"   reads={[r['count'] for r in mr['reads']]}  colors={[r['colors'] for r in mr['reads']]}")
        print(f"   reconciled={mr['reconciled']}  variance={mr.get('variance')}  "
              f"agree={mr.get('agree')}  confidence={mr['confidence']}")

    # ---- reconcile vs text ----
    print("\n" + "=" * 70 + "\nRECONCILE vs TEXT")
    recon = [r["reconciled"] for r in results if isinstance(r["reconciled"], int)]
    summed = sum(recon) if recon else None
    print(f"  per-plane reconciled counts: {[(r['label'], r['reconciled']) for r in results]}")
    print(f"  sum(per-plane) = {summed}   text total_strings = {text_total}   "
          f"{'MATCH' if summed == text_total else 'MISMATCH'}")
    lowconf = [r["label"] for r in results
               if (r.get("variance") is None or r["variance"] > CONF_VARIANCE_THRESHOLD
                   or r["confidence"] < 0.67)]
    print(f"  low-confidence planes (variance>{CONF_VARIANCE_THRESHOLD} or conf<0.67): {lowconf or 'none'}")
    gate = "SHIP per-plane counts" if (summed == text_total and not lowconf) else \
           "DO NOT SHIP — keep HARD jbox_per_plane_unresolved (confidence gate)"
    print(f"  CONFIDENCE GATE -> {gate}")
    print(f"\ncrops + full render saved under {OUTDIR}")


if __name__ == "__main__":
    pid = sys.argv[1] if len(sys.argv) > 1 else "913411"
    asyncio.run(main(pid))

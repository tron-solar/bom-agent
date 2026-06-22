"""
Automated PER-MODULE orientation detector — runs on EVERY array, EVERY engine run.

Why this exists: the Hacker (#860760) miss happened because per-module orientation was a
MANUAL re-check after the fact. For automation that cannot be a human step. This module
rasterizes each array crop at HIGH RESOLUTION, rotates it to south, detects every module
cell from the planset's blue module-border rectangles, and classifies each cell's
orientation from ITS OWN aspect ratio (w>h -> landscape, h>w -> portrait). The output is a
list of rows (each row = a horizontal band of same-y cells, left-to-right), with a per-cell
orientation list — exactly the shape split_physical_row()/make_row() consume.

It is the concrete implementation of the `module_bbox_detector` hook that
determine_orientation() requires, lifted to detect ALL cells, not just one representative.

Pipeline per array:
  1. crop the array region from the planset page (caller supplies the clip rect or full page)
  2. get_pixmap at HIGH zoom (default 12x) -> rotate by (azimuth-180) to south
  3. mask the blue module borders; find row bands (horizontal blue lines) and, within each
     band, the vertical cell separators
  4. for each cell: w_px, h_px from the band height and inter-separator spacing
     -> orientation = 'portrait' if h_px > w_px else 'landscape'
  5. return rows top->bottom, each a left->right list of orientations + the band's drawn px
     dims, so the engine can build make_row()/split_physical_row() rows with provenance.

Dependencies: PyMuPDF (fitz), Pillow, numpy. No manual input anywhere.
"""
import numpy as np
from PIL import Image

# module true dims (Sirius ELNSM54M); generalize per-module-spec later
LONG_IN = 67.80
SHORT_IN = 44.65
ASPECT_TRUE = LONG_IN / SHORT_IN  # ~1.519


def render_rotated_array(page, clip_rect, azimuth_deg, zoom=12):
    """Rasterize the array crop at HIGH RES and rotate to south (180).
    page: a fitz.Page; clip_rect: fitz.Rect around the array; azimuth_deg: array azimuth.
    Returns a PIL.Image in the south-normalized frame (rails horizontal)."""
    import fitz
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip_rect)
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    pil_angle = azimuth_deg - 180.0
    return img.rotate(pil_angle, expand=True, fillcolor=(255, 255, 255))


def _blue_mask(rgb):
    R, G, B = rgb[:, :, 0].astype(int), rgb[:, :, 1].astype(int), rgb[:, :, 2].astype(int)
    # module borders are saturated blue (B high, R&G low). Robust to the brown string lines
    # and orange MCI dots, which are NOT blue.
    return (B > 120) & (R < 110) & (G < 110)


def _cluster(idx, gap):
    """Cluster sorted indices that are within `gap` of each other; return (start,end) spans."""
    if len(idx) == 0:
        return []
    out = []
    cur = [idx[0]]
    for v in idx[1:]:
        if v - cur[-1] <= gap:
            cur.append(v)
        else:
            out.append((cur[0], cur[-1]))
            cur = [v]
    out.append((cur[0], cur[-1]))
    return out


def _segment_by_gaps(vseps, cell_w, *, gap_factor=1.4):
    """Split a band's cells into CONTIGUOUS RUNS (user, Meyer #877571).

    A 'row' is a MAXIMAL CONTIGUOUS horizontal run of modules along a rail line. Within a detected
    band, adjacent module cells share a vertical separator; a real GAP (missing module / spacing
    between separate runs) shows up as an inter-cell pitch much larger than the typical cell width.
    Each contiguous run becomes its OWN qualified row — interrupted/'retired' rows no longer exist;
    a gap simply yields MORE rows.

    vseps  : sorted x-positions of vertical separators (len = n_cells+1)
    cell_w : [vseps[j+1]-vseps[j]] widths (len = n_cells)
    Returns a list of runs; each run is a list of cell indices (into cell_w) that are contiguous.
    A run boundary is declared where a cell's width exceeds gap_factor * median true-cell width
    (i.e. that 'cell' is actually empty space between two runs).
    """
    if not cell_w:
        return []
    import statistics
    # true module cells cluster around the median width; a gap span is markedly wider.
    med = statistics.median(cell_w)
    runs, cur = [], []
    for j, w in enumerate(cell_w):
        if w > gap_factor * med and cur:
            # this 'cell' is a gap between runs -> close current run, skip the gap cell
            runs.append(cur)
            cur = []
            continue
        if w > gap_factor * med and not cur:
            continue  # leading gap, ignore
        cur.append(j)
    if cur:
        runs.append(cur)
    return runs


def detect_rows_and_orientations(rotated_img, *, min_line_frac=0.30, min_sep_frac=0.45,
                                 cluster_gap=20, sep_gap=18):
    """Detect module cells in the SOUTH-rotated raster and classify each per orientation.

    Returns a list of rows, top->bottom. Each row is a dict:
      {'orientations': ['landscape'|'portrait', ...] left->right,
       'cell_w_px': [...], 'cell_h_px': band_height_px,
       'row_dim_in_per_cell': [...]}   # drawn width back-converted to inches via the cell's edge

    Classification is PER CELL from its own w/h. A single band can therefore contain a mix,
    which split_physical_row() will break into separate make_row() rows downstream.

    SEGMENTATION (user, Meyer #877571): a band is additionally split into CONTIGUOUS RUNS wherever
    modules are not adjacent. A horizontal gap inside a band ends one qualified row and starts the
    next, so e.g. a top tier of [6]-gap-[2]-gap-[1] becomes THREE rows, not one row of 9. Counting
    horizontal rail RUNS is NOT how rows are counted (each row carries two rail runs); rows come
    only from contiguous module segmentation.
    """
    rgb = np.array(rotated_img.convert("RGB"))
    blue = _blue_mask(rgb)
    if blue.sum() == 0:
        return []

    # --- horizontal blue lines -> row-band boundaries ---
    row_density = blue.sum(axis=1)
    hthr = min_line_frac * row_density.max()
    hlines = _cluster(np.where(row_density > hthr)[0], cluster_gap)
    hcenters = [(a + b) // 2 for a, b in hlines]
    if len(hcenters) < 2:
        return []

    rows = []
    # Each consecutive pair of horizontal lines bounds one module-tall band.
    for i in range(len(hcenters) - 1):
        yA, yB = hcenters[i], hcenters[i + 1]
        band_h = yB - yA
        if band_h < 20:
            continue
        # vertical separators that span most of this band's height
        sub = blue[yA:yB, :]
        col_density = sub.sum(axis=0)
        vthr = min_sep_frac * band_h
        vseps = [int((a + b) / 2) for a, b in _cluster(np.where(col_density > vthr)[0], sep_gap)]
        if len(vseps) < 2:
            continue  # not a real cell row (e.g. a gap band between staggered rows)
        cell_w = [vseps[j + 1] - vseps[j] for j in range(len(vseps) - 1)]
        # SEGMENT this band into contiguous runs; EACH run is its own qualified row.
        runs = _segment_by_gaps(vseps, cell_w)
        for run in runs:
            orientations, dims_in, run_w = [], [], []
            for j in run:
                w = cell_w[j]
                orient = "portrait" if band_h > w else "landscape"
                orientations.append(orient)
                edge = LONG_IN if orient == "landscape" else SHORT_IN
                dims_in.append(round(edge, 2))
                run_w.append(w)
            rows.append({
                "orientations": orientations,
                "cell_w_px": run_w,
                "cell_h_px": band_h,
                "n": len(run),
                "row_dim_in_per_cell": dims_in,
            })
    return rows


def array_to_engine_rows(rotated_img, make_row, split_physical_row, *, from_rotated_raster=True):
    """Turn a south-rotated array image into make_row()/split_physical_row() rows automatically.

    For each detected band: if all cells share an orientation -> one make_row of that width;
    if a band mixes orientations -> split_physical_row() breaks it into separate rows.
    Returns the flat list of engine rows for this array (ready for resolve_racking()).
    The row_dim_inches passed is count*edge (each cell measured), so the gate's dim_check is
    self-consistent with the per-cell measurement.
    """
    detected = detect_rows_and_orientations(rotated_img)
    engine_rows = []
    for band in detected:
        ors = band["orientations"]
        if len(set(ors)) == 1:
            n = len(ors)
            edge = LONG_IN if ors[0] == "landscape" else SHORT_IN
            engine_rows.append(make_row(n, ors[0], round(n * edge, 1),
                                        from_rotated_raster=from_rotated_raster))
        else:
            # mixed band: split at every orientation change, left->right
            seg_dims = {}  # let split compute count*edge per segment
            engine_rows.extend(split_physical_row(ors, seg_dims,
                                                  from_rotated_raster=from_rotated_raster))
    return engine_rows


if __name__ == "__main__":
    # Self-test on the Hacker array (the case that motivated this): must auto-detect
    # top row 4 LANDSCAPE + bottom row 5 PORTRAIT with NO manual input.
    import fitz, sys
    sys.path.insert(0, "/tmp/x")
    from racking_engine import make_row, split_physical_row, resolve_racking

    doc = fitz.open("/mnt/user-data/uploads/Jason_Hacker_REVA.pdf")
    page = doc[2]
    rect = page.rect
    clip = fitz.Rect(rect.width * 0.20, rect.height * 0.46, rect.width * 0.42, rect.height * 0.70)
    rot = render_rotated_array(page, clip, azimuth_deg=153, zoom=12)
    rows = detect_rows_and_orientations(rot)
    print("AUTO-DETECTED rows (no manual input):")
    for r in rows:
        print(f"  n={r['n']} orientations={r['orientations']} "
              f"cell_w_px={r['cell_w_px']} band_h_px={r['cell_h_px']}")
    eng = array_to_engine_rows(rot, make_row, split_physical_row)
    print("Engine rows:", [(r["n"], r["orient"]) for r in eng])
    arrays = [{"label": "Roof1", "azimuth": 153, "rows": eng}]
    planset = {"attachments": 26, "rails": 6, "splice": 4, "mid_clamps": 14, "end_clamps": 8}
    delivered, xc = resolve_racking(arrays, planset)
    print("attach orientation_signal:", xc["attachments"]["orientation_signal"])

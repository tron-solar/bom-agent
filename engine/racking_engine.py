"""
Tron Solar BOM — Racking Engine (refined per sourcing policy, this session).

Pipeline (runs every time, every array):
  1. rotate_to_south(array_image, azimuth)   -> normalize so rails are horizontal
  2. read rows + per-row module counts (landscape/portrait) in the south frame,
     building each row with make_row(... from_rotated_raster=True) so it carries proof
  3. assert_orientation_provenance(arrays)   -> HARD GATE (see below), runs inside
     resolve_racking(); a non-180 array without rotated-raster provenance + a passing
     dimension reconciliation CANNOT produce a BOM
  4. compute ALL formulas (so cross-checks + formula-truth items always exist)
  5. apply SOURCING POLICY to decide delivered value per item

ORIENTATION GATE (enforced, not advisory):
  Every row must be created via make_row(), which stamps orientation_source and a
  dim_check (modules*edge vs the drawn width callout). resolve_racking() then calls
  assert_orientation_provenance(), which RAISES OrientationGateError unless, for every
  row of every array: (1) the array is az 180 OR orientation came from the rotated
  raster; (2) the dimension reconciliation passed; (3) no flip is suspected (the
  opposite orientation must not fit the drawn width better). This is the structural fix
  for the recurring az 90/270/88/268 misread (Evrard Roof1/Roof3, Davis-Kelly Roof2,
  Shaw) where a landscape module drawn sideways looks portrait in the as-drawn frame.

SOURCING POLICY:
  splice      : PLANSET ONLY            (formula = None for now; user will supply)
  attachment  : PLANSET, checked vs formula
  rails       : PLANSET, checked vs formula
  combo_clamp : PLANSET (mid+end),      checked vs formula
  deck_screw  : FORMULA (truth)
  ground_lug  : FORMULA (truth)
  wire_clip   : FORMULA (truth)
  end_cap     : FORMULA (truth)
"""
import math

# ---------------------------------------------------------------------------
# MODULE DIMENSIONS ARE PER-MODEL — READ FROM PV-3, NEVER HARDCODE (user, Woroszylo).
# PV-3 carries the module size in TWO places that must agree:
#   (1) the "MODULE TYPE, DIMENSIONS & WEIGHT" text block ("MODULE DIMENSIONS = 67.80\" x 44.65\"")
#   (2) the dimensioned module graphic (e.g. 44.65" wide x 67.80" tall callout)
# The LONG edge (67.80 for Sirius) and SHORT edge (44.65) differ for QCELL / Hyundai / Aptos /
# LONGi etc., so every build MUST pass the module's actual dims read+confirmed off PV-3.
# The constants below are ONLY the Sirius ELNSM54M default/fallback; resolve_racking() and the
# orientation builders take an explicit module_dims so the rail span + dim-checks use real numbers.
# ---------------------------------------------------------------------------
LONG_IN = 67.80     # Sirius ELNSM54M long edge — DEFAULT/FALLBACK ONLY; read PV-3 per build
SHORT_IN = 44.65    # Sirius ELNSM54M short edge — DEFAULT/FALLBACK ONLY
RAIL_LEN_IN = 172.0


class ModuleDims:
    """Module long/short edge in inches, read from PV-3 for the project's module model.
    long_in  = the larger dimension (rail span when LANDSCAPE), short_in = smaller (when PORTRAIT).
    Build with ModuleDims.from_pv3(long, short) after confirming BOTH PV-3 sources agree."""
    __slots__ = ("long_in", "short_in", "source")
    def __init__(self, long_in, short_in, source="PV-3"):
        long_in, short_in = float(long_in), float(short_in)
        if short_in > long_in:
            long_in, short_in = short_in, long_in  # enforce long >= short regardless of input order
        self.long_in, self.short_in, self.source = long_in, short_in, source

    @classmethod
    def from_pv3(cls, dim_a, dim_b, source="PV-3 module dims block + graphic"):
        """Pass the two PV-3 dimensions in any order; the larger becomes long_in."""
        return cls(dim_a, dim_b, source=source)

    @classmethod
    def sirius_default(cls):
        return cls(LONG_IN, SHORT_IN, source="Sirius ELNSM54M DEFAULT (PV-3 not read!)")

    def edge(self, orientation):
        return self.long_in if orientation == "landscape" else self.short_in

    def __repr__(self):
        return f"ModuleDims(long={self.long_in}, short={self.short_in}, src={self.source!r})"


class RotatedArray:
    """Wrapper proving rotate_to_south() has run AND that the array is now at 180deg
    (south). Orientation determination REQUIRES one of these with is_south=True, so a
    raw image, an as-drawn frame, or a not-yet-normalized non-180 array cannot be used."""
    __slots__ = ("img", "orig_azimuth_deg", "pil_angle", "result_azimuth_deg", "is_south")
    def __init__(self, img, orig_azimuth_deg, pil_angle, result_azimuth_deg):
        self.img = img
        self.orig_azimuth_deg = orig_azimuth_deg
        self.pil_angle = pil_angle
        self.result_azimuth_deg = result_azimuth_deg
        # south-normalized only if the post-rotation azimuth is 180
        self.is_south = (round(result_azimuth_deg) % 360 == 180)


def needs_rotation_to_south(azimuth_deg):
    """True if this array is NOT already at 180deg (south) and therefore MUST be rotated
    before orientation can be judged. (180 is the only azimuth that needs no rotation.)"""
    return round(azimuth_deg) % 360 != 180


def audit_arrays_for_rotation(arrays):
    """Pre-flight check: scan every array's azimuth and report which are non-180.
    Returns (all_handled, report). Use this BEFORE orientation to guarantee each
    non-180 array gets rotate_to_south. Davis-Kelly Roof2 (az270) is the case this
    catches: it must be rotated to 180 before landscape/portrait is decided."""
    report = []
    for a in arrays:
        az = a.get("azimuth")
        non180 = needs_rotation_to_south(az) if az is not None else True
        report.append({"label": a.get("label"), "azimuth": az,
                       "non_180": non180,
                       "action": "ROTATE to 180 then determine orientation" if non180
                                 else "already south (180); rotate 0deg, still normalize"})
    return report


def rotate_to_south(pil_img, azimuth_deg):
    """Rotate an array crop so it sits at 180deg (south), rails HORIZONTAL.
    pil_angle = azimuth - 180 (validated sign). MUST run on EVERY array, no exceptions.
    After rotation the array's azimuth IS 180 by construction; the returned token carries
    is_south=True only then. Returns a RotatedArray required by determine_orientation()."""
    pil_angle = azimuth_deg - 180.0
    rotated = pil_img.rotate(pil_angle, expand=True, fillcolor="white")
    # by construction the array is now oriented to south (180)
    return RotatedArray(rotated, azimuth_deg, pil_angle, result_azimuth_deg=180)


def measure_module_box_from_rotated(rotated, module_bbox_detector):
    """Measure a representative module bounding box (w_px, h_px) FROM the rotated raster.

    `module_bbox_detector(rotated.img) -> (w_px, h_px)` must read pixels off the
    rotated image (e.g. detect the module rectangle / rail spacing in that frame).
    This is the ONLY supported way to obtain the dims used for orientation, so the
    measurement is structurally tied to the rotated image — a caller cannot substitute
    numbers eyeballed from the as-drawn page.
    """
    if not isinstance(rotated, RotatedArray):
        raise TypeError("measure_module_box_from_rotated requires a RotatedArray.")
    w_px, h_px = module_bbox_detector(rotated.img)
    return float(w_px), float(h_px)


def determine_orientation(rotated, module_bbox_detector,
                          long_in=LONG_IN, short_in=SHORT_IN):
    """Determine a module's orientation FROM GEOMETRY MEASURED OFF THE ROTATED RASTER.

    CHANGED (was a source of a real error): this no longer accepts caller-supplied
    w_px/h_px. The earlier signature let a caller pass dims eyeballed from the AS-DRAWN
    page while a blank RotatedArray satisfied the type check — defeating rotation in
    substance (Davis-Kelly Roof#2: called portrait from the as-drawn read; the true
    south-frame read is landscape). Now the dims MUST be measured from rotated.img via
    `module_bbox_detector`, so the as-drawn frame is not even available at this point.

    Primary test (per spec): in the rotated frame the rail is HORIZONTAL.
      - module height > width  -> PORTRAIT
      - else                   -> LANDSCAPE
    Confirmation: edge parallel to the horizontal rail = SHORT (portrait) / LONG (landscape).
    Returns (orientation, confirmation_ok, detail).
    """
    if not isinstance(rotated, RotatedArray):
        raise TypeError(
            "determine_orientation requires a RotatedArray from rotate_to_south(); "
            "orientation must be judged AFTER rotation, never on a raw/as-drawn image.")
    if not rotated.is_south:
        raise ValueError(
            f"Array is not south-normalized (result azimuth={rotated.result_azimuth_deg}). "
            "A non-180 array MUST be rotated to 180 before orientation is determined.")
    if not callable(module_bbox_detector):
        raise TypeError(
            "determine_orientation requires module_bbox_detector(rotated.img)->(w_px,h_px); "
            "caller-supplied pixel dims are no longer accepted (they let the as-drawn "
            "shortcut bypass rotation).")

    module_w_px, module_h_px = measure_module_box_from_rotated(rotated, module_bbox_detector)

    primary = "portrait" if module_h_px > module_w_px else "landscape"
    aspect = module_w_px / module_h_px if module_h_px else float("inf")
    if aspect >= 1.0:
        edge_parallel, confirm = "long", "landscape"
    else:
        edge_parallel, confirm = "short", "portrait"
    confirmation_ok = (primary == confirm)
    detail = {
        "measured_from": "rotated_raster",
        "orig_azimuth": rotated.orig_azimuth_deg,
        "normalized_to_south": rotated.is_south,
        "primary_test": f"h={module_h_px:.1f} {'>' if module_h_px>module_w_px else '<='} w={module_w_px:.1f}",
        "primary_result": primary,
        "edge_parallel_to_rail": edge_parallel,
        "confirm_result": confirm,
        "aspect_measured": round(aspect, 3),
        "confirmation_ok": confirmation_ok,
        "rotation": {"orig_azimuth": rotated.orig_azimuth_deg, "pil_angle": rotated.pil_angle},
    }
    return primary, confirmation_ok, detail


def check_row_count(modules_in_row, orientation, row_dim_inches,
                    long_in=LONG_IN, short_in=SHORT_IN, tol=0.6):
    """Cross-check a row's module count against its drawn dimension callout.
    row_dim_inches = the row's overall width along the rail, from the planset dim line.
    Catches eyeball miscounts (Roof#1 10-vs-11: 62'-7"=751in /67.79=11.08 -> 11).
    Returns (ok, implied_count, detail)."""
    edge = long_in if orientation == "landscape" else short_in
    implied = row_dim_inches / edge
    ok = abs(implied - modules_in_row) <= tol
    return ok, round(implied), {
        "row_dim_in": row_dim_inches, "edge_in": edge,
        "implied_count": round(implied, 2), "stated_count": modules_in_row, "match": ok}


def edge_along_rail(orientation, module_dims=None):
    """Width each module contributes along the (horizontal, post-rotation) rail.

    ORIENTATION RULE (canonical): after rotate_to_south the rail is HORIZONTAL.
    Look at which module edge runs PARALLEL to that rail:
      - long edge  parallel to rail  -> 'landscape' -> contributes module long edge
      - short edge parallel to rail  -> 'portrait'  -> contributes module short edge
    Dimensions come from PV-3 per module model (pass module_dims); falls back to Sirius default.
    Sanity check against the drawn array dimension: (modules in row) * edge_along_rail
    should match the row's overall width callout (e.g. Shaw Roof2 7*44.64=312in=26'-3").
    Do NOT judge orientation in the as-drawn frame for non-180 azimuths; rotate first.
    """
    md = module_dims or ModuleDims.sirius_default()
    return md.edge(orientation)


# ---------- FORMULAS (always computed; truth for some items, cross-check for others) ----------

def f_attachments(arrays):
    """(landscape*3 + portrait*2) + rows*2 + interrupted_rows*2, summed per array."""
    tot = 0
    for a in arrays:
        l = sum(r["n"] for r in a["rows"] if r["orient"] == "landscape")
        p = sum(r["n"] for r in a["rows"] if r["orient"] == "portrait")
        nrows = len(a["rows"])
        irows = sum(1 for r in a["rows"] if r.get("interrupted"))
        tot += l * 3 + p * 2 + nrows * 2 + irows * 2
    return tot

def f_rails(arrays, module_dims=None):
    """CORRECTED FORMULA (user, Woroszylo): rails = ceil(total_span / 172) * 2 + 1, where the
    span is rounded up ONCE over the COMBINED module run, NOT per-row.

      rails = ceil( (landscape_modules*LONG + portrait_modules*SHORT) / 172 ) * 2 + 1

    MODULE DIMENSIONS ARE PER-MODEL, READ FROM PV-3 (user): LONG/SHORT come from module_dims
    (ModuleDims read off the PV-3 "MODULE TYPE, DIMENSIONS & WEIGHT" block + dimensioned module
    graphic, confirmed to agree). They are NOT hardcoded — a QCELL/Hyundai/Aptos/LONGi module has
    different dims than the Sirius 67.80x44.65 default. Falls back to Sirius only if none passed.

    WHY THE OLD PER-ROW VERSION WAS WAY OFF (Woroszylo: computed 34 vs planset 23):
      The old code did ceil(row_width/172)*2 for EACH row and summed. With 9 short rows that
      rounds up 9 separate times (e.g. a 3-module run = ceil(203/172)=2 pieces *2 = 4), losing
      the shared-rail efficiency. A single 172" rail spans MULTIPLE modules and continues across
      row gaps within a plane, so the round-up must happen ONCE on the total span.
      Old:34, new:25, planset:23 -> the +1-once global model tracks reality (delta +2 vs +11).

    Mixed-orientation arrays: landscape modules contribute LONG each, portrait SHORT each, to a
    single combined span before the single round-up.
    """
    md = module_dims or ModuleDims.sirius_default()
    landscape = sum(r["n"] for a in arrays for r in a["rows"] if r["orient"] == "landscape")
    portrait  = sum(r["n"] for a in arrays for r in a["rows"] if r["orient"] == "portrait")
    span = landscape * md.long_in + portrait * md.short_in
    if span <= 0:
        return 0
    return math.ceil(span / RAIL_LEN_IN) * 2 + 1

def f_splice(arrays, module_dims=None):
    """CONFIRMED formula: per row (ceil(row_width/172)-1)*2.
    NOTE: per policy, splice is now PLANSET-ONLY. This stays for reference/telemetry
    until the user supplies the official splice formula. Module dims read from PV-3."""
    md = module_dims or ModuleDims.sirius_default()
    tot = 0
    for a in arrays:
        for r in a["rows"]:
            w = r["n"] * edge_along_rail(r["orient"], md)
            tot += (math.ceil(w / RAIL_LEN_IN) - 1) * 2
    return tot

def f_combo_clamp(arrays):
    """mid+end clamps: modules*2 + rows*2 + 2, per array. (Cross-check basis.)"""
    tot = 0
    for a in arrays:
        mods = sum(r["n"] for r in a["rows"])
        tot += mods * 2 + len(a["rows"]) * 2 + 2
    return tot

def f_deck_screw(attachments):       # TRUTH
    return attachments * 4

def f_ground_lug(arrays):            # TRUTH
    return sum(len(a["rows"]) + 2 * sum(1 for r in a["rows"] if r.get("interrupted"))
               for a in arrays)

def f_wire_clip(arrays):             # TRUTH
    return 2 * sum(r["n"] for a in arrays for r in a["rows"])

def f_end_cap(arrays):               # TRUTH
    return sum(len(a["rows"]) * 4 +
               4 * sum(1 for r in a["rows"] if r.get("interrupted"))
               for a in arrays)


# ---------- MANDATORY ORIENTATION GATE (runs on EVERY array, EVERY build) ----------
#
# WHY THIS EXISTS (Evrard Roof1/Roof3, Davis-Kelly Roof2 — same failure 3x):
#   determine_orientation() is well guarded, but NOTHING forced the per-array `rows`
#   handed to resolve_racking() to have come from that guarded path. A human (or a
#   future extractor) could eyeball orientation in the AS-DRAWN frame — where an az
#   90/270/88/268 landscape module has height>width and thus LOOKS portrait — and the
#   pipeline accepted it silently. The fix: resolve_racking() now REFUSES to run unless
#   every array proves, per row, that orientation was measured from a rotated raster
#   AND that the row's module count was reconciled against its drawn dimension callout.
#   The dimension check is the real backstop: at az 90/270 the orientation eyeball is
#   unreliable, but modules*edge == drawn-width is not (landscape 5*67.8=339 vs portrait
#   would need 44.65 and never matches). A non-180 array CANNOT pass without it.

class OrientationGateError(ValueError):
    """Raised when an array reaches the BOM pipeline without proof its orientation was
    measured from the rotated raster and dimension-confirmed. Hard stop — never warn-and-pass."""


def make_row(n, orient, row_dim_inches, *, from_rotated_raster, interrupted=False,
             long_in=LONG_IN, short_in=SHORT_IN, tol=0.6):
    """Build a pipeline-ready row that CARRIES ITS PROOF. This is the only blessed way to
    create a row for resolve_racking().

      n                   : module count in the row
      orient              : "landscape"/"portrait" — for a non-180 array this MUST have been
                            read from the ROTATED raster (pass from_rotated_raster=True only
                            if determine_orientation() produced it off rotated.img)
      row_dim_inches      : the row's drawn width callout along the rail (from the planset dim line)
      from_rotated_raster : True iff `orient` came from a rotated-raster measurement

    A ROW IS ONE HORIZONTAL RAIL RUN AND HOLDS A SINGLE ORIENTATION. A landscape and a
    portrait module cannot share a row: along a horizontal rail they have different heights,
    so they ride different rail pairs. If a physical run on the roof mixes orientations
    left-to-right, it is NOT one row — split it first with split_physical_row(). `orient`
    here MUST be a single scalar ("landscape" or "portrait"); a list/mixed value is rejected.

    The row is stamped with a dim_check (modules*edge vs drawn width). If the stamped
    orientation disagrees with the dimension within tolerance, the row is marked failing and
    the gate will reject the whole build — catching a sign-flip exactly like Roof1/Roof3.
    """
    if orient not in ("landscape", "portrait"):
        raise ValueError(
            f"make_row: orient must be a single 'landscape' or 'portrait', got {orient!r}. "
            "A row is ONE rail run with ONE orientation; mixed runs must be split via "
            "split_physical_row() into separate rows.")
    ok, implied, detail = check_row_count(n, orient, row_dim_inches,
                                          long_in=long_in, short_in=short_in, tol=tol)
    # Also test the OTHER orientation: if the wrong orientation fits the dim far better,
    # that's a flip signal even if `ok` squeaked by tolerance.
    other = "portrait" if orient == "landscape" else "landscape"
    ok_other, implied_other, _ = check_row_count(n, other, row_dim_inches,
                                                 long_in=long_in, short_in=short_in, tol=tol)
    flip_suspected = (not ok and ok_other)
    return {
        "n": n, "orient": orient, "interrupted": bool(interrupted),
        "row_dim_in": row_dim_inches,
        "orientation_source": "rotated_raster" if from_rotated_raster else "UNVERIFIED",
        "uniform_orientation": True,
        "dim_check": {**detail, "ok": ok, "other_orient_fits": ok_other,
                      "flip_suspected": flip_suspected},
    }


def split_physical_row(module_orientations, segment_dims_inches, *, from_rotated_raster,
                       interrupted=False, long_in=LONG_IN, short_in=SHORT_IN, tol=0.6):
    """Split ONE physical horizontal run (read after rotate_to_south) into the separate
    BOM rows it actually represents, breaking wherever orientation changes left-to-right.

      module_orientations : list of per-module orientations across the run, IN PHYSICAL
                            LEFT-TO-RIGHT ORDER, e.g. ["landscape","landscape","portrait"].
                            (For a clean single-orientation run just use make_row directly.)
      segment_dims_inches : dict mapping each contiguous same-orientation SEGMENT to its
                            drawn width callout, keyed by segment index (0-based in
                            left-to-right order). If a key is missing the segment width is
                            computed as count*edge (still dim-checked, just self-consistent).

    Returns a LIST of make_row() rows (one per contiguous same-orientation segment). A run
    like L,L,P,P,P -> two rows: a 2-wide landscape row and a 3-wide portrait row. This is the
    physical truth (Rick States 7L-top + 10P-bottom was the cross-array version; this handles
    the SAME-run version where orientations sit side by side).
    """
    if not module_orientations:
        return []
    for o in module_orientations:
        if o not in ("landscape", "portrait"):
            raise ValueError(f"split_physical_row: bad orientation {o!r} in run.")
    # group contiguous same-orientation segments
    segments = []  # list of (orient, count)
    cur = module_orientations[0]; cnt = 1
    for o in module_orientations[1:]:
        if o == cur:
            cnt += 1
        else:
            segments.append((cur, cnt)); cur = o; cnt = 1
    segments.append((cur, cnt))
    rows = []
    for idx, (orient, count) in enumerate(segments):
        edge = long_in if orient == "landscape" else short_in
        dim = segment_dims_inches.get(idx, round(count * edge, 1)) if segment_dims_inches else round(count * edge, 1)
        rows.append(make_row(count, orient, dim, from_rotated_raster=from_rotated_raster,
                             interrupted=interrupted, long_in=long_in, short_in=short_in, tol=tol))
    return rows





def assert_orientation_provenance(arrays):
    """HARD GATE. For every array, every row, require:
       (1) az 180 OR orientation_source == 'rotated_raster'  (no as-drawn reads for non-180)
       (2) a dim_check that PASSED (modules*edge == drawn width, within tol)
       (3) no flip_suspected (the wrong orientation must NOT fit the dimension better)
    Raises OrientationGateError listing every offending row. Returns an audit list on success.
    This is called by resolve_racking() so it cannot be skipped."""
    problems = []
    audit = []
    for a in arrays:
        az = a.get("azimuth")
        is_180 = (az is not None) and (round(az) % 360 == 180)
        for i, r in enumerate(a.get("rows", [])):
            label = f"{a.get('label','?')}[row{i}]"
            src = r.get("orientation_source")
            dc = r.get("dim_check")
            # (0) a row is ONE rail run with ONE orientation: reject mixed/list orient
            ori = r.get("orient")
            if ori not in ("landscape", "portrait"):
                problems.append(f"{label}: orient={ori!r} is not a single 'landscape'/"
                                f"'portrait'. A row is one rail run with one orientation; "
                                f"a physical run mixing both must be split (split_physical_row).")
            # (1) provenance: non-180 arrays may not use an unverified/as-drawn read
            if not is_180 and src != "rotated_raster":
                problems.append(f"{label}: az={az} is non-180 but orientation_source="
                                f"{src!r} (must be measured from the rotated raster).")
            # (2) dimension reconciliation must be present and passing
            if dc is None:
                problems.append(f"{label}: no dim_check — every row must reconcile its "
                                f"module count against the drawn dimension (use make_row()).")
            else:
                if not dc.get("ok"):
                    problems.append(f"{label}: dim_check FAILED — {r.get('n')} "
                                    f"{r.get('orient')} modules imply width "
                                    f"{dc.get('implied_count')} vs drawn {dc.get('row_dim_in')}in.")
                if dc.get("flip_suspected"):
                    problems.append(f"{label}: ORIENTATION FLIP SUSPECTED — the opposite "
                                    f"orientation fits the drawn width better. Re-read from the "
                                    f"rotated raster (this is the Roof1/Roof3 / Davis-Kelly bug).")
            audit.append({"array": a.get("label"), "az": az, "row": i,
                          "orient": r.get("orient"), "source": src,
                          "dim_ok": (dc or {}).get("ok"),
                          "flip_suspected": (dc or {}).get("flip_suspected")})
    if problems:
        raise OrientationGateError(
            "Orientation gate REJECTED the build (" + str(len(problems)) + " issue(s)). "
            "Non-180 arrays must be rotated to south and dimension-confirmed before BOM:\n  - "
            + "\n  - ".join(problems))
    return audit


# ---------- POLICY APPLICATION ----------

def resolve_racking(arrays, planset, enforce_orientation_gate=True, attachment_type=None,
                    module_dims=None):
    """
    arrays: list of {"label","azimuth","rows":[<row from make_row()>]}
            Each row MUST be built with make_row() so it carries orientation_source +
            dim_check. (rows already read in the south-rotated frame)
    planset: dict of printed K2 BOM values: attachments, rails, splice, mid_clamps, end_clamps
    attachment_type: one of None / "K2_SHINGLE" / "S5_PROTEABRACKET" / "S5_SOLARFOOT".
            Controls the 1.066 attachment check (see below). Only "S5_PROTEABRACKET" enables it.
    module_dims: ModuleDims read from PV-3 (long/short edge for THIS project's module model).
            Feeds the rails + splice span math. Falls back to Sirius 67.80x44.65 if None — but a
            real build MUST pass the PV-3-confirmed dims (they differ per module model).
    Returns (delivered, crosscheck) where delivered = BOM quantities, crosscheck = telemetry.

    Raises OrientationGateError if any non-180 array lacks rotated-raster provenance or a
    passing dimension reconciliation (set enforce_orientation_gate=False ONLY for unit tests
    of the formula math itself — never for a real build).
    """
    md = module_dims or ModuleDims.sirius_default()
    if enforce_orientation_gate:
        gate_audit = assert_orientation_provenance(arrays)
    else:
        gate_audit = None
    # always compute formulas
    comp_attach = f_attachments(arrays)
    comp_rails  = f_rails(arrays, md)
    comp_splice = f_splice(arrays, md)
    comp_combo  = f_combo_clamp(arrays)

    planset_combo = (planset.get("mid_clamps", 0) + planset.get("end_clamps", 0))

    delivered = {
        # planset-sourced (deck screw keys off DELIVERED attachment = planset)
        "attachments": planset["attachments"],
        "rails":       planset["rails"],
        "splice":      planset["splice"],
        "combo_clamp": planset_combo,
        # formula-truth
        "deck_screw":  f_deck_screw(planset["attachments"]),  # 4 per delivered attachment
        "ground_lug":  f_ground_lug(arrays),
        "wire_clip":   f_wire_clip(arrays),
        "end_cap":     f_end_cap(arrays),
        # rail clamps = attachments (delivered)
        "rail_clamp":  planset["attachments"],
    }

    def chk(name, planset_v, comp_v):
        return {"delivered_from": "planset", "planset": planset_v, "computed": comp_v,
                "delta": comp_v - planset_v, "match": comp_v == planset_v}

    # ATTACHMENT CROSS-CHECK IS ORIENTATION-SENSITIVE -> use it to VALIDATE orientation.
    # attach = (landscape*3 + portrait*2) + rows*2 + irows*2. A landscape module costs 1 MORE
    # attach than portrait, so if N modules are misread landscape-instead-of-portrait the computed
    # attach is too high by exactly N (and vice-versa). When the planset prints an attachment count,
    # an EXACT match corroborates the orientation mix; an off-by-k delta that equals a plausible
    # L<->P swap count is an ORIENTATION-ERROR SIGNAL, not benign telemetry. (Hacker: all-landscape
    # gave delta +5; correct 4L+5P gave delta 0. The +5 WAS the 5 portrait modules misread landscape.)
    attach_chk = chk("attachments", planset["attachments"], comp_attach)
    if attach_chk["match"]:
        attach_chk["orientation_signal"] = "EXACT -> orientation mix corroborated"
    else:
        dlt = attach_chk["delta"]
        total_mods = sum(r["n"] for a in arrays for r in a["rows"])
        if 0 < abs(dlt) <= total_mods:
            attach_chk["orientation_signal"] = (
                f"delta {dlt:+d}: a landscape<->portrait misread of {abs(dlt)} module(s) would "
                f"explain this exactly (landscape costs +1 attach vs portrait). RE-CHECK per-module "
                f"orientation from the rotated raster before trusting the read.")
        else:
            attach_chk["orientation_signal"] = (
                f"delta {dlt:+d}: not cleanly explained by an L/P swap; likely shared-geometry telemetry.")

    # 1.066 PLANSET-ATTACHMENT CHECK — SCOPED (user, Woroszylo correction):
    # The 1.066 factor models the EXTRA attach points from S-5! ProteaBracket's TIGHTER 45" spacing
    # (vs 48" shingle). It applies ONLY to S-5! ProteaBracket. It DOES NOT apply to S-5! SolarFoot
    # (SolarFoot uses standard rail spacing — the foot pedestal sits per rail-run like a normal
    # attachment, so the bare formula is already correct). On the Woroszylo metal/SolarFoot job
    # ceil(105*1.066)=112 vs planset 102 (delta +10) — the factor badly overshoots SolarFoot, which
    # is exactly why it must be gated to ProteaBracket. (On shingle it also overshot, so K2_SHINGLE
    # does NOT use it either.)
    if attachment_type == "S5_PROTEABRACKET":
        factor = 1.066
        attach_expected = math.ceil(comp_attach * factor)
        attach_chk["check_1066"] = {
            "applies": True,
            "attachment_type": attachment_type,
            "formula_attach": comp_attach,
            "factor": factor,
            "expected_planset_attach": attach_expected,   # ceil(formula * 1.066)
            "planset_attach": planset["attachments"],
            "delta_vs_planset": attach_expected - planset["attachments"],
            "match": attach_expected == planset["attachments"],
        }
    else:
        attach_chk["check_1066"] = {
            "applies": False,
            "attachment_type": attachment_type,
            "note": ("1.066 factor applies ONLY to S-5! ProteaBracket (45in spacing). "
                     "Not applied for SolarFoot / K2 shingle / unspecified — bare formula stands."),
        }

    rails_chk = chk("rails", planset["rails"], comp_rails)
    rails_chk["module_dims"] = {"long_in": md.long_in, "short_in": md.short_in, "source": md.source}
    crosscheck = {
        "attachments": attach_chk,
        "rails":       rails_chk,
        "combo_clamp": chk("combo_clamp", planset_combo, comp_combo),
        "splice":      {"delivered_from": "planset", "planset": planset["splice"],
                        "computed_legacy": comp_splice, "note": "formula PLANSET-ONLY per policy; official formula TBD"},
        "deck_screw":  {"delivered_from": "formula"},
        "ground_lug":  {"delivered_from": "formula"},
        "wire_clip":   {"delivered_from": "formula"},
        "end_cap":     {"delivered_from": "formula"},
        "orientation_gate": {"enforced": enforce_orientation_gate, "audit": gate_audit},
    }
    return delivered, crosscheck


if __name__ == "__main__":
    import json
    # Furlow, all landscape, rotated-to-south reads. Rows built via make_row() so each
    # carries rotated-raster provenance + a dimension reconciliation. row_dim_in = n*67.8
    # (the drawn width callout) for these clean landscape rows.
    def L(n, from_rotated=True):  # landscape row helper for the demo
        return make_row(n, "landscape", round(n * LONG_IN, 1), from_rotated_raster=from_rotated)
    arrays = [
        {"label":"Roof1","azimuth":180,"rows":[L(5),L(5),L(5)]},
        {"label":"Roof2","azimuth":180,"rows":[L(2),L(3)]},
        {"label":"Roof3","azimuth":90, "rows":[L(1),L(2),L(3)]},
        {"label":"Roof4","azimuth":90, "rows":[L(4),L(4)]},
        {"label":"Roof5","azimuth":270,"rows":[L(3),L(3)]},
    ]
    planset = {"attachments":126,"rails":33,"splice":18,"mid_clamps":56,"end_clamps":48}
    print("ROTATION AUDIT:", json.dumps(audit_arrays_for_rotation(arrays), indent=2))
    delivered, crosscheck = resolve_racking(arrays, planset)
    print("DELIVERED:", json.dumps(delivered, indent=2))
    print("\nCROSSCHECK:", json.dumps(crosscheck, indent=2))

    # ---- SELF-TEST: the gate MUST reject the Evrard-style mistake ----
    # An az-268 array whose rows were eyeballed PORTRAIT from the as-drawn frame.
    # 5 landscape modules span 5*67.8=339in. If mislabeled portrait, 339/44.65=7.6 -> flip.
    bad = [{"label":"Roof1_badread","azimuth":268,
            "rows":[make_row(5, "portrait", 339.0, from_rotated_raster=False)]}]
    try:
        resolve_racking(bad, planset)
        print("\nSELF-TEST 1 FAILED: gate did NOT reject the as-drawn portrait misread!")
    except OrientationGateError as e:
        print("\nSELF-TEST 1 OK — gate rejected the as-drawn misread:\n", e)
    # ---- SELF-TEST 2: a physical run mixing orientations splits into separate rows ----
    # Rick-States-style run read after rotation: 2 landscape then 3 portrait, side by side.
    seg = split_physical_row(["landscape","landscape","portrait","portrait","portrait"],
                             segment_dims_inches={}, from_rotated_raster=True)
    assert len(seg) == 2, "mixed run must split into 2 rows"
    assert seg[0]["orient"] == "landscape" and seg[0]["n"] == 2
    assert seg[1]["orient"] == "portrait" and seg[1]["n"] == 3
    print("\nSELF-TEST 2 OK — mixed L,L,P,P,P run split into "
          f"{seg[0]['n']} landscape + {seg[1]['n']} portrait rows.")

    # And a hand-built row with a bogus mixed orient is rejected by the gate.
    mixedbad = [{"label":"R","azimuth":180,
                 "rows":[{"n":5,"orient":["landscape","portrait"],
                          "orientation_source":"rotated_raster",
                          "dim_check":{"ok":True,"flip_suspected":False}}]}]
    try:
        resolve_racking(mixedbad, planset)
        print("SELF-TEST 2b FAILED: gate accepted a mixed-orient row!")
    except OrientationGateError as e:
        print("SELF-TEST 2b OK — gate rejected a mixed-orientation row.")


# ====================================================================
# MICROINVERTER (ENPHASE) ACCESSORY + COMBINER BREAKER LOGIC
# Added after Davis-Kelly gaps: (1) Engage cabling rows 14-18 were empty
# despite 15 IQ8 micros; (2) the 20A/2P branch breakers INSIDE the IQ
# Combiner were not added to the breaker rows.
# ====================================================================

def parse_branch_circuits(one_line_text):
    """Branch-circuit (== string) count from the PV-5 one-line note, e.g.
    "(04) BRANCH CIRCUITS OF 10 MODULES ARE CONNECTED IN PARALLEL" -> 4, or the split
    form "(01) BRANCH CIRCUITS OF 09 MODULES AND (01) BRANCH CIRCUIT OF 06 MODULES ARE
    CONNECTED IN PARALLEL" -> 1+1 = 2. Sums ALL "(0n) BRANCH CIRCUIT(S) OF m MODULES"
    occurrences. Falls back to counting labeled 'BRANCH CIRCUIT #n' / 'STRING #n'."""
    import re
    matches = re.findall(r"\(?0*(\d+)\)?\s+BRANCH\s+CIRCUITS?\s+OF\s+\d+\s+MODULES",
                         one_line_text, flags=re.IGNORECASE)
    if matches:
        return sum(int(m) for m in matches)
    nums = set(re.findall(r"(?:BRANCH\s+CIRCUIT|STRING)\s*#?\s*(\d+)",
                          one_line_text, flags=re.IGNORECASE))
    return len(nums)


def enphase_engage_accessories(num_modules, num_arrays, num_branch_circuits):
    """Solar BOM rows 14-18 Engage cabling — per the installer's rules:
      14 Q-12-17-240 trunk        = modules + arrays
      15 Q-TERM-10  terminator    = (branch_circuits * 2) + 1
      16 Q-SEAL-10  sealing cap   = branch_circuits + 1
      17 Q-CONN-10F female conn   = sealing caps (= branch_circuits + 1)
      18 Q-CONN-10M male conn     = NOT USED (skip)
    Returns {solar_row: qty}. Populates whenever micros are present (num_modules>0).
    """
    if not num_modules:
        return {}
    seal = num_branch_circuits + 1
    return {
        14: num_modules + num_arrays,            # trunk
        15: (num_branch_circuits * 2) + 1,       # terminator caps
        16: seal,                                # sealing caps
        17: seal,                                # female connectors (= sealing caps)
        # 18 male connector intentionally NOT filled
    }


def combiner_branch_breakers(num_branch_circuits, branch_breaker_rating="20A_2P"):
    """Breakers INSIDE an Enphase IQ Combiner: one 2-pole branch breaker per PV branch
    circuit. These are rated, not (E), not a service main -> they DO go on the BOM.
    Maps to Electrical BOM breaker rows. Davis-Kelly: 2 branch circuits -> 2x 20A/2P (BR220).
    The combiner's internal 10A/15A IQ-Gateway breaker ships with the combiner assembly and
    is NOT added separately.
    Returns {electrical_row: qty}."""
    row_by_rating = {
        "15A_2P": 101, "20A_2P": 102, "30A_2P": 103, "40A_2P": 104,
        "50A_2P": 105, "60A_2P": 106,
    }
    row = row_by_rating.get(branch_breaker_rating, 102)
    bc = max(1, num_branch_circuits)
    return {row: bc}


def microinverter_electrical_block(num_micros, num_branch_circuits,
                                   combiner_row=35, branch_breaker_rating="20A_2P"):
    """Full set of micro-driven ELECTRICAL additions: the combiner (already handled by
    equipment logic) + its internal branch breakers. Returns {electrical_row: qty} for
    the branch breakers only (combiner placed elsewhere)."""
    if not num_micros:
        return {}
    return combiner_branch_breakers(num_branch_circuits, branch_breaker_rating)


# ====================================================================
# TESLA ENERGY GATEWAY 3 — BREAKER LOGIC (CSR rule CORRECTED, user/Woroszylo)
# ====================================================================

# Electrical BOM breaker rows
_BR_2P_BY_RATING = {15:101, 20:102, 30:103, 40:104, 50:105, 60:106,
                    70:107, 80:108, 100:109, 125:110}
_CSR_BY_RATING   = {100:112, 125:113, 200:114}


def tesla_gateway_breakers(buskit_60a_2p_count, battery_pw3_count,
                           gateway_breakers_outside_buskit):
    """Breakers for a Tesla Energy Gateway 3 project. Returns {electrical_row: qty}.

    RULE (CORRECTED — user, Woroszylo): There is NEVER an existing/excluded breaker drawn
    inside the Tesla Energy Gateway. EVERY rated breaker drawn in the Gateway that sits
    OUTSIDE the internal bus-kit is a NEW CSR main breaker and MUST be ordered, mapped by
    rating: 100A->CSR2100(112), 125A->CSR2125(113), 200A->CSR2200(114). This fires EVERY
    time such a breaker is present (no "is it the gateway main / is it existing" judgment —
    that judgment was the Woroszylo miss, where a 200A/2P CSR was wrongly excluded as an
    "internal main"). It also matches Eroh ("200A CSR in TEG" -> CSR2200) as the universal rule.

      buskit_60a_2p_count            : number of 60A/2P breakers drawn INSIDE the bus-kit
                                       (one per PW3). Drives BR260.
      battery_pw3_count              : count of PW3 (1707000) units (expansion EXCLUDED).
      gateway_breakers_outside_buskit: list of ratings (amps, 2-pole) drawn in the Gateway
                                       OUTSIDE the bus-kit, e.g. [200] or [100, 200].
                                       EACH becomes a CSR. (The small unrated microgrid-
                                       interconnect toggle has no rating callout -> not passed in.)

    BR260 floor: max(bus-kit 60A/2P count, PW3 battery count).
    """
    out = {}
    # BR260 backup breaker(s) for the PW3(s) on the bus-kit
    br260 = max(buskit_60a_2p_count, battery_pw3_count)
    if br260 > 0:
        out[106] = out.get(106, 0) + br260
    # CSR main breaker(s) — EVERY rated breaker outside the bus-kit is a NEW CSR
    for rating in gateway_breakers_outside_buskit:
        row = _CSR_BY_RATING.get(rating)
        if row is None:
            # rating without a CSR row (rare) — flag via a sentinel key the caller can surface
            out.setdefault("_csr_unmapped", []).append(rating)
            continue
        out[row] = out.get(row, 0) + 1
    return out

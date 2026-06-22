# Tron Solar BOM Engine — Complete Project Record
**As of:** 2026-05-29
**Purpose:** Automated Bill-of-Materials creator. Reads Tron Solar PV/ESS planset
PDFs + Coperniq project data, applies business rules, fills a fixed two-sheet
Excel template (`BOM_Template_for_AI.xlsx`: "Solar BOM" + "Electrical BOM"),
outputs a filled, filtered `.xlsx`.

End goal: API pipeline that downloads plans from Coperniq, runs the engine,
uploads the BOM back. (Coperniq API + Anthropic API key must run in the user's
environment — the dev sandbox cannot reach Coperniq's API directly.)

---

## SESSION CHANGELOG (most recent validation pass)

### AUTOMATION — per-module orientation now AUTO-DETECTED every run (orientation_detector.py)
The Hacker per-module orientation fix was a MANUAL re-check; that can't survive automation. New module
orientation_detector.py runs on EVERY array, EVERY run, NO manual input:
  - render_rotated_array(page, clip_rect, azimuth, zoom=12): HIGH-RES rasterize + rotate to south.
  - detect_rows_and_orientations(rotated_img): masks the blue module borders, finds horizontal row
    bands, then within each band finds vertical cell separators; classifies EACH cell by its OWN
    w/h aspect (h>w portrait, else landscape). A band may return MIXED orientations.
  - array_to_engine_rows(...): converts detected bands into make_row()/split_physical_row() rows
    (uniform band -> one make_row; mixed band -> split). Feeds resolve_racking() with measured
    provenance so the orientation gate sees real rotated-raster reads, never a hand read.
  Self-test on Hacker auto-detects top 4 LANDSCAPE (cells ~460x303px) + bottom 5 PORTRAIT
  (~304x459px) with zero manual input; attach cross-check then reports EXACT. This is the concrete
  implementation of determine_orientation()'s module_bbox_detector hook, lifted to ALL cells.
  Deps: fitz, Pillow, numpy.

### 1.066 PLANSET-ATTACHMENT CHECK (user, metal-roof calibration trial)
resolve_racking() crosscheck['attachments']['check_1066'] now computes
  expected_planset_attach = ceil(formula_attachments * 1.066)
and compares to the PRINTED planset attachment count. Rationale: bridge the systematic gap between the
bare per-row attach formula and what plansets actually call out. CALIBRATION NOTE: on Hacker (shingle)
ceil(26*1.066)=28 vs planset 26 -> overshoots by 2, so 1.066 does NOT fit shingle. It is proposed for
METAL roofs (tighter 45in spacing vs 48in shingle -> more attach points). The incoming metal planset is
the real test of the factor.


### JASON HACKER (#860760) — per-module orientation; MIXED array caught; attachment cross-check as orientation validator
- 9 Sirius 450W, 3 MCI-2, 1 PW3, Tesla Gateway 3, asphalt shingle roof, 1 array (az 153). Existing 100A
  service retained (upgrade_needed=No): (E) bi-directional meter kept (NO new meter row), (E) 100A MSP +
  (E) MBE retained. BR260=1 (bus-kit 60A/2P, floored vs 1 battery); NO CSR (only other gateway breaker is
  the internal transfer switch, no rating callout). JB-1.2=1 (shingle, 1 string).
- NAME MISMATCH (HIGH flag): Coperniq title 'Jason Hacker' but planset + primary email
  (johnbryant2755@gmail.com) are 'Angela G Bryant / John Bryant' at the SAME address/system. Headered to
  planset name; flagged for account reconciliation. Not a quantity issue.
- ORIENTATION read corrected by PER-MODULE check (user instruction: check per module, not per array).
  First all-landscape read was WRONG. Per-module pixel measurement of the rotated raster: TOP row 4 cells
  460x303px (w>h LANDSCAPE), BOTTOM row 5 cells 304x459px (h>w PORTRAIT), aspect 1.51-1.52 = 67.8/44.65.
  Array is MIXED: 4 landscape + 5 portrait. DELIVERED VALUES UNCHANGED (ground_lug=2, end_cap=8,
  wire_clip=18, S-clip=54 depend on rows+modules, not orientation; racking is planset-sourced).

### RULE — ATTACHMENT CROSS-CHECK IS AN ORIENTATION VALIDATOR (not benign telemetry)
attach = (landscape*3 + portrait*2) + rows*2 + irows*2. A landscape module costs +1 attach vs portrait,
so misreading N modules landscape-instead-of-portrait inflates computed attach by EXACTLY N. Therefore:
- attach cross-check EXACT (delta 0) -> orientation mix corroborated.
- attach delta = +k where k <= module count and equals a plausible L<->P swap count -> ORIENTATION-ERROR
  SIGNAL; re-check per-module orientation from the rotated raster before trusting the read.
Hacker proof: wrong all-landscape gave delta +5 (= the 5 portrait modules misread); correct 4L+5P gave
delta 0 EXACT. The +5 I had dismissed as 'normal positive telemetry' WAS the error signature. Now encoded
in resolve_racking(): crosscheck['attachments']['orientation_signal'] computes this automatically.
LESSON: when a planset prints an attachment count, an orientation-sensitive cross-check landing exact-vs-off
is a SIGNAL. Don't wave off a clean integer delta that matches an L/P swap as shared-geometry noise.
PROCESS: judge orientation PER MODULE from the rotated raster (aspect ratio of each cell), never per-array.


### WINTHROP WILLIFORD (#818226) — first SnapNrack S200 ground mount; 2 rule fixes
- GROUND MOUNT, SnapNrack S200 (NOT K2). Ground racking read STRAIGHT from the PV-3.1 SnapNrack
  BOM table -> SnapNrack ground rows (Solar 56-67), distinct from the K2 ground block (73-90) used
  by Steele/Fickas. All 11 table lines mapped: GroundRail172(56)=17, MidClamp(57)=36, EndClamp(58)=24,
  BondingPipeClamp(59)=24, GroundRailEndCap(60)=24, OmniLug(61)=1, ReducingTee/PipeCap(62)=16,
  PlugEnd(63)=20, AdjReducingElbowTee(64)=28, GroundRailSplice(65)=12, AmericanGroundScrew AGS#3(67)=16.
- 24 Sirius 450W, 8 MCI-2, 1 Tesla PW3, Tesla BACKUP SWITCH (line-side at meter collar, NEC 705.11)
  -> row 57; no bus-kit breakers -> NO BR rows, NO CSR. New Milbank U6281-XL-200-5T9-AMS meter/main
  -> row 94 (closest; -AMS suffix not in template). JB-3 ground rule = ceil(3/4)=1. S-clip formula
  left to populate (144) per Steele/Fickas precedent.
- DC disconnects: 4-POLE #1 (two strings) -> SI32 row 18; 2-POLE #2 (single string) -> EN-EP200G row 17.
- AC DISCONNECT CONFLICT (HIGH flag): one-line DIAGRAM draws FUSED (60A fuses) but equipment SCHEDULE
  says NON-FUSED. One-line governs -> built FUSED: D222NRB(10) + 2x FRN-R-60(138). Flagged for designer.

### RULE FIX 1 — GROUND MOUNTS ARE ALWAYS 4-HIGH LANDSCAPE (user)
Every ground-mount array is 4 modules high in LANDSCAPE orientation. Do NOT read ground orientation
per-array; assume 4-high landscape. (Williford 24 = 6 wide x 4 high landscape.) Racking is table-read
so delivered quantities are unaffected, but the layout record and any future ground formula logic must
use 4-high landscape. The roof orientation gate does NOT apply to ground mounts (ground racking is read
from the SnapNrack/K2 BOM table, never computed from orientation).

### RULE FIX 2 — K4977 TAP-LUG MATCH BY SUBSTRING (user)
Electrical taps block: the Milbank K4977 dual slide-in lugs were MISSED because the matcher looked for
the exact token 'K4977-INT' but plansets write it as 'MILBANK K4977-LUGS INT' (the '-LUGS' infix broke
the match). FIX: match on substring 'K4977' anywhere in the one-line text; when found, populate Electrical
row 26 (K4977-INT, MILBANK DUAL SLIDE IN LUGS) at FLAT qty 3 (consistent with the taps-block flat-3 rule).
These are the supply-side interconnection lugs for line-side connections (Tesla Backup Switch / meter collar).


### FILTER FIX + PER-ROW MIXED-ORIENTATION (Eroh render bug + user rule)
(1) BLANK-ROW FILTER was wrong: it only hid menu rows (SKU present, qty empty) and left
FULLY-EMPTY spacer rows visible, so the rendered BOM showed gaps between items. CORRECTED in
filter_blank_rows.py / apply_qty_filter(): in the data region (row 5 down) hide EVERY row whose
QUANTITY (col A) is empty, whether it has a SKU or not. Only exemption: a col-A formula string
(e.g. the S-clip =IF(...)) counts as present. Header rows 1-4 untouched. This is now the canonical
final step for every build (recalc -> apply_qty_filter -> present).

(2) PER-ROW ORIENTATION GRANULARITY (user rule): a ROW = one horizontal rail run = ONE
orientation. A landscape and a portrait module CANNOT share a row (different heights along a
horizontal rail -> different rail pairs). If, after rotate_to_south, a landscape module sits
physically left/right of a portrait module in the same run, they are SEPARATE rows. Encoded in
racking_engine.py:
- make_row() now REJECTS a non-scalar orient (must be a single 'landscape'/'portrait') and stamps
  uniform_orientation=True.
- NEW split_physical_row(module_orientations, segment_dims_inches, from_rotated_raster=...): takes
  a physical run's left-to-right per-module orientation list and splits it at every orientation
  change into separate make_row() rows (e.g. L,L,P,P,P -> a 2-wide landscape row + a 3-wide
  portrait row). segment_dims_inches optionally supplies each segment's drawn width; else uses
  count*edge (still dim-checked).
- assert_orientation_provenance() adds check (0): reject any row whose orient is not a single
  scalar. Self-test 2b proves the gate rejects a hand-built mixed-orient row.
The per-row formulas (f_attachments, f_rails, f_splice, f_combo_clamp) already consumed each row's
own orient, so once a mixed run is split the math is correct with no formula change.

### ALICIA EROH (#859354) — clean all-shingle Tesla-Gateway job; first post-gate autonomous-style run
- 39x Sirius 450W, 14 MCI-2, 2x Tesla PW3, Tesla Gateway 3, new Milbank U9551 meter. All asphalt
  shingle, K2 deck-mount only (install note: "deck-mounted ONLY, 48in spacing").
- ORIENTATION GATE first real catch: Roof3 (az 271) reads PORTRAIT as-drawn (tall stack on page)
  but is LANDSCAPE after rotating to south (1 row of 3, long edge along rail). Rotated, dim-checked,
  no flip -> all 39 landscape. Roof1 az1 (19: 7+6+6), Roof2 az181 (17: 6+6+5), Roof3 az271 (3: 1).
  7 rows total. combo-clamp cross-check EXACT (98=98) confirms module total + row count.
- J-box: all three roofs shingle -> JB-1.2 = ceil(2/4)+ceil(2/4)+ceil(1/4) = 3; JB-3 = 0. Matches 3
  drawn JB symbols.
- Breakers: Tesla Gateway. Bus-kit 60A/2P x2 (one per PW3) + 2 PW3 -> BR260 floor = max(2,2)=2.
  Install note "200A CSR breaker in TEG" -> CSR2200=1. (E) 200A GE MSP main excluded.
- upgrade_needed=Yes is a METER upgrade only (existing 200A GE MSP retained; new ground bar, remove
  MBJ). No MSP/panel line.
- Solar: mod39, MCI14, JB1.2=3, sclip234, attach116, deck464, rails33, railclamp116, splice24,
  combo98, lug7, endcap28, wclip78. Electrical: disco60 x2, hub4, RSD1, gndbar1, PW3x2, gateway1,
  U9551 meter1, BR260=2, CSR2200=1. Recalc clean.
- Install-master note (Coperniq) was unusually detailed and CONFIRMED the planset on every point
  (CSR200 in TEG, deck-only 48in, ground bar, MSP retained) -> 0 conflicts. Reinforces the rule that
  notes win and serve as a strong cross-check.


### ORIENTATION GATE NOW ENFORCED IN CODE (Evrard Roof1/Roof3 — 3rd recurrence of the same bug)
The "measure orientation from the rotated raster" rule was documented and determine_orientation()
was guarded, but NOTHING forced the per-array `rows` handed to resolve_racking() to have come from
that guarded path. On Clinton Evrard (#860613) Roof1 & Roof3 (az 268/88, 13 modules each) were read
PORTRAIT from the as-drawn frame — where a landscape module drawn sideways has height>width and thus
LOOKS portrait. All 38 modules are actually LANDSCAPE (confirmed by actually rotating: 28'-5"=341in
=~5x67.8 widest run). Same failure as Davis-Kelly Roof2 and the Shaw hardening.
FIX (structural, in racking_engine.py):
- make_row(n, orient, row_dim_inches, *, from_rotated_raster, interrupted=False): the ONLY blessed
  way to build a row. Stamps orientation_source ('rotated_raster' or 'UNVERIFIED') and a dim_check
  (modules*edge vs the drawn width callout), plus a flip test (does the OPPOSITE orientation fit the
  drawn width better?).
- assert_orientation_provenance(arrays): HARD GATE. For every row of every array raises
  OrientationGateError unless (1) az 180 OR orientation_source=='rotated_raster'; (2) dim_check passed;
  (3) no flip_suspected. resolve_racking() calls it first (enforce_orientation_gate=True default), so a
  non-180 array WITHOUT rotated-raster provenance + a passing dimension reconciliation CANNOT produce a
  BOM. Self-test in __main__ proves the gate rejects the exact Evrard misread (az268, 5 modules labeled
  portrait, 339in drawn -> all 3 checks fire). Set enforce_orientation_gate=False ONLY for formula-math
  unit tests, never a real build.
EFFECT on Evrard BOM: delivered racking unchanged (all planset-sourced). Orientation corrected to
all-landscape; row count 13->11 (Roof1/Roof3 staggered landscape pyramids 5+4+3+1 each, +Roof2 1 +Roof4
2). Formula-truth items: ground_lug 13->11, end_cap 52->44. wire_clip 76, S-clip 228 unchanged.
HOW TO FEED IT (every future build): rotate each non-180 array to south, read orientation off the
rotated raster, then build rows with make_row(..., from_rotated_raster=True, row_dim_inches=<drawn
width callout>). The drawn dimension is the real backstop — at az 90/270 the orientation eyeball is
unreliable but modules*edge==drawn-width is not.

### CLINTON EVRARD (#860613) — first MIXED shingle+metal-attachment planset
- TWO attachment SKUs on one job: K2 Multimount 107 (shingle, deck-mount) -> Solar row 33;
  S-5! Protea Bracket 31 (metal seam, garage Roof4) -> Solar row 37 (separate row, NOT merged).
  S-5! row 37 'comes with L-Foot' per PV-14 -> rows 38/39 NOT added. Both attachment types are
  PLANSET-DELIVERED. ENGINE GAP for autonomy: attachment SKU must be routed per-array by roof type,
  not globally (racking_engine currently has one attachment bucket).
- J-BOX RULE CORRECTED (user, this session): ceil(strings/4) per array, SPLIT BY ROOF TYPE.
  EZSOLAR JB-1.2 (row 25) = formula over SHINGLE arrays only; EZSOLAR JB-3 (row 26) = formula over
  METAL arrays only; TOMBSTONE (row 27) NOT used. Supersedes old 'shingle->JB1.2, rail->JB3,
  metal->TOMBSTONE' mapping. Evrard: shingle ceil(2/4)+ceil(1/4)+ceil(2/4)=3 -> JB-1.2=3; metal
  ceil(1/4)=1 -> JB-3=1. Matches the 4 drawn JB symbols (3 house + 1 garage). 'Ground always JB-3'
  stays a separate ground-mount rule.
- ATTACHMENT SPACING tweak (user): metal arrays use 45in (was 48in) for the FORMULA CROSS-CHECK only
  (delivered S-5! qty=31 is planset). At 45in computed metal attach=32 (delta +1 vs 31); at 48in it
  was 28 (delta -3) — 45in tightens telemetry. Shingle stays 48in. Spacing read per-roof from PV-3
  tables (metal seam 9in / shingle rafter 24in).
- Module wattage typo: PV-2 + PV-3 BOM call Roof4 '400W'; PV-1/PV-7 say 450W (single SKU). Built 450W x38.
- Breakers: Tesla Gateway 3. Bus-kit 60A/2P (1) + 1 PW3 -> BR260 floor=1. 100A/2P outside bus-kit
  (Gateway->MSP), rated/not(E)/not main -> CSR2100=1. (E) 100A/2P MSP main excluded.
- Expansion (2 units, both wall-mount per user): WM-kit row62=2, harness-20 row64=2.
- Deck screw policy A: 4x107=428, lag row34 blank. S-5! brackets take no deck screw.
- PAGE INDEXING lesson: PV-5 is page idx 6 here (PV-4 + PV-4.1 sit between PV-3.1 and PV-5). The
  extractor's 'PV-5 fallback page 4' would grab the wrong page — ALWAYS find pages by label, never
  fixed index.

Status: STILL IN TESTING — validating against real plansets and tweaking rules.
NOT yet implemented as a live workflow.

### MODULE SKU PLACEMENT FIX (template + rule)
Module rows 5-9 on Solar BOM are a FIXED SKU MENU (one row per module model), not a
free cell. Build = put qty in the A-cell of the MATCHING SKU row; NEVER overwrite a B/C
SKU/description during a build. A30 S-clip formula sums A5:A9 so any correct row works.
Rows: 5=ELNSM54M-HC-N-450 (450W Sirius), 6/7=Hyundai 435/440, 8=Aptos 460,
9=LR5-54HPB-415M (415W LONGI) <- B9 OVERRIDDEN ONCE on template (was HIMO5 DC LR5-54HPB).
Canonical blank template: out_roof_eroh_TEMPLATE.xlsx (qtys cleared, B9 fixed, A30 intact).
Module SKU moving forward MUST match B9 for 415W LONGI projects.
PRIOR BUG corrected: Shaw had qty on row5 with B5 overwritten to the LONGI SKU (corrupted
Sirius row); moved qty to row9, restored B5. Furlow (Sirius 450W) correctly stays on row5.

### MICROINVERTER accessory + combiner-breaker logic (Davis-Kelly gaps)
Two missing-line bugs found on the first microinverter project:
(1) Solar rows 14-18 (Enphase Engage cabling: trunk Q-12-17-240, terminator Q-TERM-10,
    seal Q-SEAL-10, female Q-CONN-10F, male Q-CONN-10M) were EMPTY despite 15 IQ8 micros.
    FIX: enphase_engage_accessories(num_modules, num_arrays, num_branch_circuits) using the
    INSTALLER'S RULES (recovered from prior chat, validated vs Sams 40-micro planset):
      row14 trunk Q-12-17-240      = modules + arrays
      row15 term cap Q-TERM-10     = (branch_circuits * 2) + 1
      row16 sealing cap Q-SEAL-10  = branch_circuits + 1
      row17 female Q-CONN-10F      = sealing caps (= branch_circuits + 1)
      row18 male Q-CONN-10M        = NOT USED (never filled)
    Branch circuits parsed from PV-5 note via parse_branch_circuits() which SUMS all
    "(0n) BRANCH CIRCUIT(S) OF m MODULES" occurrences (DK: "(01)...09 AND (01)...06" = 2).
    "Strings" and "branch circuits" are interchangeable. Sams check: 40 mod,1 array,4 bc ->
    14:41 15:9 16:5 17:5. DK: 15 mod,2 arrays,2 bc -> 14:17 15:5 16:3 17:3, row18 blank.
(2) The breakers INSIDE the Enphase IQ Combiner were not added. The combiner is a load
    center: 1 two-pole branch breaker per PV branch circuit lands on breaker rows 98-111.
    Davis-Kelly PV-5 combiner showed 2x 20A/2P (Circuit#1 + Circuit#2) + a 10A/15A IQ-Gateway
    breaker. FIX: combiner_branch_breakers(num_branch_circuits, rating) -> e.g. 2x 20A/2P =
    BR220(row102) qty2. The 10A/15A IQ-Gateway breaker ships with the combiner; not added.
    RULE: combiner internal branch breakers count (rated, not (E), not a service main).
Davis-Kelly now: Solar 14-18 filled (15/2/2/2/2); Electrical BR220=2 (combiner) + BR230=1
(interconnection). Branch-circuit count from PV-5 string map (9-mod string + 6-mod circuit = 2).

### Non-180 rotation GATE (added confirmation layer)
Before orientation logic, the engine now AUDITS every array's azimuth:
- needs_rotation_to_south(az): True for any az != 180.
- audit_arrays_for_rotation(arrays): lists each array, whether non_180, and the action
  ("ROTATE to 180 then determine orientation"). Run this pre-flight on every project.
ENFORCEMENT: RotatedArray now carries result_azimuth_deg + is_south (True only when the
post-rotation azimuth == 180). rotate_to_south() sets the array to 180 by construction.
determine_orientation() raises ValueError unless is_south=True — so a non-180 array that
has NOT been rotated to south cannot have its orientation determined. This is on top of the
prior fix (dims must be measured from the rotated raster, not caller-supplied).
Audited test set: Furlow R3/R4/R5 (90/90/270), Shaw R2 (270), Davis-Kelly R1/R2 (0/270) all
flagged for mandatory rotation; az 0 is correctly flagged (not 180). Rule: confirm non-180
arrays first; rotate each to 180; THEN determine landscape/portrait from the rotated image.

### Davis-Kelly — ROTATION-IN-SUBSTANCE fix (orientation measured from rotated raster)
ROOT CAUSE: Roof2 (az270) was called PORTRAIT but is LANDSCAPE. The rotate_to_south guard
from prior session was satisfied MECHANICALLY (a RotatedArray token existed) but DEFEATED in
substance: orientation dims (w_px/h_px) were eyeballed from the AS-DRAWN page and passed into
determine_orientation by the caller. The token proved a rotation object was created; it did
NOT prove the measured dims came from the rotated image. For az 0/180 the as-drawn read happens
to be correct, so the shortcut hides; for az 90/270 it gives the wrong frame.
THE TELL WAS PRESENT AND DISMISSED: cross-check attach delta was -9 (computed BELOW planset),
sign-flipped vs every other project, and legacy splice 0 vs planset 6. A sign-flipped/large
delta is a WRONG-READ signal, not noise. Corrected (landscape) -> attach 57 EXACT, splice 6 EXACT.
FIX (code): determine_orientation no longer accepts caller-supplied w_px/h_px. It now requires
module_bbox_detector(rotated.img)->(w_px,h_px) and measures from rotated.img via
measure_module_box_from_rotated(). The as-drawn frame is not available at that point, so the
shortcut is structurally impossible. RULE: for ANY non-180 azimuth, actually rotate the crop and
read orientation FROM the rotated image; never reason about orientation in the as-drawn frame.
Roof2 corrected: 3 rows x 3 LANDSCAPE = 9 (17'-0" runs horizontal along rail = 3*67.8 long edge).
Davis-Kelly totals: 15 landscape, 0 portrait, 6 rows. (Delivered BOM unchanged — all racking
planset-sourced — but orientation record + cross-check were wrong and are now correct.)

### Jo Shaw follow-up — MANDATORY rotation + geometric orientation + row-count check
ROOT CAUSE of Roof#1 miscount (read 10+9+7, actual 11+9*+6): counted modules by eye on
a zoomed-out crop where edge cells blurred, and never cross-checked count vs the row's
dimension callout. Total happened to equal 26 so the per-row error was silent (it skews
ground_lug/end_cap). FIX = never eyeball; cross-check every row count against its drawn dim.

New code in racking_engine.py:
- RotatedArray token: determine_orientation() ONLY accepts the output of rotate_to_south();
  passing a raw/as-drawn image raises TypeError. Rotation is now structurally impossible to
  skip (az180 still rotates, 0deg, returns a token). 
- determine_orientation(rotated, w_px, h_px): PRIMARY test height>width -> portrait else
  landscape (measured AFTER rotation, rail horizontal). CONFIRMATION: edge parallel to the
  horizontal rail must be short(portrait)/long(landscape). Emits confirmation_ok; mismatch =
  flag for human.
- check_row_count(n, orient, row_dim_in): implied = row_dim_in / (long|short edge); flags if
  |implied - n| > 0.6. Catches the 10-vs-11 (62'-7"=751in/67.79=11.08 -> 11). Use the planset
  dimension lines for every row.
Verified: raw image rejected; Roof2 h>w->portrait confirmed short||rail; Roof1 landscape
confirmed long||rail; row-count flags stated-10 (implied 11), passes stated-11.

### Jo Shaw REV A pass — orientation rule clarified + interrupted rows + battery mismatch
- ORIENTATION is defined POST-rotation by which edge is parallel to the (horizontal) rail:
  long-edge||rail = landscape (contributes 67.79" to row width); short-edge||rail = portrait
  (contributes 44.64"). Verify against drawn dim: modules*edge = row width callout.
  Roof2 az270: rotate +90 -> rails horizontal -> cells taller than wide -> PORTRAIT;
  7*44.64=312"=26'-3" matches (landscape would be 39.5ft, impossible). Do NOT judge
  orientation in as-drawn frame for non-180 azimuths.
- INTERRUPTED ROWS: a row split by a roof obstruction sets interrupted=True; feeds
  +2 ground_lug and +4 end_cap per interrupted row, and +2 attachments. Roof1 = 11 + 9(interrupted) + 6.
- CROSS-CHECK as VALIDATION: wrong reads gave attach Δ+28; corrected reads gave Δ+6,
  rails Δ+2, combo Δ+2, legacy splice EXACT(22), end-cap formula EXACT(24). Big deltas =
  read error signal; near-zero = read confirmed. This is the confidence-gate backbone.
- BATTERY MISMATCH (Coperniq data-quality): Coperniq battery field said "ENPHASE 10C"
  but description + install notes + entire planset = 1x Tesla PW3. Built to Tesla; flagged
  field as erroneous. RULE: planset + install master notes WIN over a conflicting Coperniq
  structured field; always emit a HIGH flag for the human to fix the field.
- INTERCONNECTION (load-side, not Gateway): 60A/2P breaker inside (E) Square D 200A MSP
  (opposite busbar end, 705.13) -> BR260=1. (E) 200A main excluded. MID = Tesla Backup
  Switch (1624171), no internal bus-kit breakers, so NO extra breakers + NO CSR. PCS 160A
  limit -> no MSP/meter upgrade; existing meter remains -> NO meter line. DC disco =
  SI32-PEL64R-4 (2-string 600V 4P) on detached structure.

### Furlow REV B pass — RACKING SOURCING POLICY (canonical, supersedes prior racking handling)
Pipeline still runs every time, every array: rotate_to_south -> read rows+per-row
orient -> compute ALL formulas -> apply sourcing policy below. Module module dims
default to Sirius 67.80 long / 44.65 short; generalize per module spec later.

| Item          | Delivered source       | Cross-check vs formula? |
|---------------|------------------------|-------------------------|
| Splice        | PLANSET ONLY           | No — official formula TBD by user. Legacy formula (ceil(w/172)-1)*2 matched Furlow=18 but is NOT authoritative; keep only as telemetry. |
| Attachments   | PLANSET                | Yes |
| Rails         | PLANSET                | Yes |
| Combo clamp   | PLANSET (mid+end)      | Yes |
| Deck screw    | FORMULA (truth)        | n/a — = 4 * DELIVERED(planset) attachments |
| Ground lug    | FORMULA (truth)        | n/a |
| Wire clip     | FORMULA (truth)        | n/a |
| End cap       | FORMULA (truth)        | n/a |

Rail clamps = delivered attachments (= planset). Implemented in racking_engine.py
(resolve_racking). Cross-check deltas are TELEMETRY for the confidence report, not
errors — they reflect shared-rail/shared-attachment geometry the per-row formulas
do not yet model (attach +18, rails +9, combo +10 on Furlow). Splice & end-cap
formulas reconciled EXACTLY (18, 48), which is what validated the orientation reads.

DECK SCREW POLICY DECISION (A): all attachments treated as deck-type -> deck = 4*126
= 504, lag-screw row left blank. Matches reference-BOM behavior (Rick: deck=4*attach,
lag empty). Furlow planset drew a 48 rafter / 78 deck split; policy (A) ignores it.
If user prefers (B) honor-split: lag=48, deck=312. CURRENTLY (A).

### Furlow orientation correction (process lesson)
rotate_to_south MUST run on EVERY array including az-180 ("already south" shortcut is
a bug). Initial read mis-set Roof3/4/5 to portrait and Roof3 to 2+2+2; correct reads:
ALL 40 modules landscape; Roof1 5+5+5, Roof2 2+3, Roof3 1+2+3 (stairstep), Roof4 4+4,
Roof5 3+3; 12 rows total. Splice cross-check (18) is the signal that confirmed it.

### AUTONOMY STATUS (honest)
Computed engine = solid given correct inputs. PV-3 extraction = the unvalidated risk;
in this session the human corrected the array read twice. Full Coperniq-trigger
autonomy additionally needs: (1) an event runtime/orchestrator (MCP is pull-only, not
push); (2) hands-off-validated PV-3 extractor; (3) a calibrated confidence gate — and
the gate depends on the cross-check formulas being right, else correct reads throw
false low-confidence flags. Fix formulas as inefficiencies surface (this session's
directive).

---
### PRIOR CHANGELOG
1. **PV-3 rotation, two-pass read.** Pass 1 reads azimuth/tilt/module_count;
   pass 2 rotates each roof by (180 − azimuth) to a south frame (ridge up, rails
   horizontal) and reads layout there. Method: `rotate_to_south()`.
2. **Rotation SIGN fix.** Spec (180 − az): positive = clockwise. PIL rotate() is
   CCW-positive, so the helper passes the NEGATED angle: `pil_angle = azimuth − 180`
   (az 90 → rotate(−90); az 270 → rotate(+90)). A 180° sign error preserves row
   COUNT + orientation (BOM totals stay right) but mirrors top/bottom & left/right.
   Upside-down TITLE BLOCK after rotation is cosmetic, not an array error.
3. **Per-ROW orientation (not per-plane).** A single roof face can mix
   orientations. Pass 2 returns `rows_detail` (per-row modules + orientation) and
   sums portrait/landscape across rows. Landscape = box wider than tall; portrait
   = taller than wide. (Rick States: 1×7 landscape top + 1×10 portrait bottom.)
   Tell-tale: attachments far ABOVE planset ⇒ portrait rows mis-read as landscape
   (landscape costs 3 attach, portrait 2).
4. **Row counting = horizontal rail RUNS.** One horizontal run = 1 row regardless
   of module count; stacked runs = more rows; a run broken by gap/obstruction =
   interrupted (counts in rows AND int_rows).
5. **Meter gating.** A meter line is ordered ONLY if the planset draws a NEW
   meter/socket. Utility field selects WHICH sku, never triggers the line.
   Existing meter reused / upgrade_needed = No ⇒ no meter row.
6. **Breakers — BR260 floor + CSR + inclusion filter.** Breakers block must RUN on
   every Gateway project. BR260 = max(drawn 60A bus-kit count, PW3 1707000 qty).
   CSR (112-114): a NEW breaker drawn OUTSIDE the bus-kit → CSR by rating.
   Inclusion rule (ALL must hold): (1) has a rating, (2) NOT labeled "(E)"
   [absence of "(N)" does NOT exclude — most required breakers are unlabeled],
   (3) NOT a panel/service MAIN. "Inside the MSP" is NOT a blanket exclusion;
   existing MSP branch breakers are typically UNRATED/UNLABELED (caught by cond 1).
   COUNTEREXAMPLE (Rose Sams): a rated 125A/2P interconnection breaker inside a
   NEW Homeline MSP IS ordered → row-128 fallback B='HOM2125', C='125A 2P BREAKER
   (HOMELINE)'. Homeline path triggers on Enphase 10C and some Backup-Switch jobs.
7. **Cross-check triad as first-class validator.** attachment + combo-clamp +
   end-cap deltas vs planset pin down BOTH row count AND orientation; a large
   structured divergence points to a specific misread (rows or orientation).

### Validated test projects this pass
Adam Drone (#7650/853974), Lolita Adkinson (#7500/847026),
Leslie Ebright (#7753/856454, Gateway+Expansion+non-fused),
Rick States (#7572/851644, mixed-orientation 7L+10P). All reconcile to planset
within tolerance; all recalc with 0 formula errors.

---

## 1. DATA SOURCES

### Planset (PDF) — read via Vision on rendered pages
- **PV-1 cover:** customer name, address, system size DC/AC, module make/model/qty,
  MCI/shutdown-device count (in scope block), battery make/qty, utility, mount type.
- **PV-2/PV-3 site & ground plan:** array count, strings per array, layout.
- **PV-3.x BILL OF MATERIAL:** the K2 ground-mount BOM table(s) (image, read via
  Vision). One table per ground array — MUST be summed across arrays.
- **PV-5 one-line:** disconnects (fused/non-fused, rating), DC disconnect poles,
  branch-circuit note, breakers drawn in gateway bus-kit & combiner, meter SKU,
  Tesla remote-meter blocks, equipment schedule.

### Coperniq (live connector) — project custom fields (keyNames)
- `zone` → Warehouse Zone (B2). DROPDOWN: Zone 1/2/3.
- `system_mount_type` → Roof Mount / Ground Mount / Hybrid (drives row-block switch)
- `battery` (multi) + `battery_quantity`
- `expansion_quantity`
- `utility_company` (ComEd, Ameren, We Energies, …)
- `service_type` (Overhead / Underground)
- `upgrade_needed` (Electrical Upgrade Yes/No)
- `system_size_kw_dc` / `system_size_kw_ac` (cross-check)

These are used to CROSS-CHECK the planset reads (computed value is the BOM
truth; Coperniq + drawn artifacts confirm; disagreements → warnings).

---

## 2. TEMPLATE STRUCTURE

Two sheets. Columns: A=QTY, B=SKU, C=Item Description, D=BOM category.
- **Headers** (both sheets rows 1-3): Customer Name / Warehouse Zone / Customer
  Address. Fill **Solar BOM only**; Electrical B1-B3 mirror via `='Solar BOM'!Bn`.
- **Solar BOM A30 is a live formula** `=IF(COUNT($A$5:$A$9)>0, SUM*6, "")`
  (S-clips). NEVER overwrite — fill module qty (rows 5-9), sheet computes S-clips.
- **Roof vs Ground** are separate row blocks; mount type fills one, leaves other blank.
- **Output filter:** template has AutoFilter on column A. Final step hides all
  blank-QTY rows (rows hidden, NOT deleted; clear filter to restore). Filter is
  anchored on full table A4:D{last} so the dropdown is usable in desktop Excel.
- **Output filename convention:** `BOM_<First>_<Last>.xlsx`.

---

## 3. SOLAR BOM — RULES BY ROW

### Modules (rows 5-9) — pick one by model
- ELNSM54M-HC-N-450 → row 5 (450W SIRIUS), HiS-T435NF→6, HiS-T440NF→7,
  DNA-120-BF10-460→8, HIMO5→9. Qty = module count. Drives A30 S-clip formula.

### Microinverter / Trunk block (rows 11-18)
- 11-13 micros (IQ8HC-72-M-DOM-US→11, IQ8PLUS-72-M-US→12, IQ8HC-72-M-US→13):
  SKU read from plans, no formula.
- 14 Q-12-17-240 trunk = **modules + arrays**
- 15 Q-TERM-10 terminator caps = **(branch_circuits × 2) + 1**
- 16 Q-SEAL-10 sealing caps = **branch_circuits + 1**
- 17 Q-CONN-10F female connectors = **= sealing caps**
- 18 Q-CONN-10M male = **NOT USED**
- Branch circuits read from PV-5 note: "(04) BRANCH CIRCUITS OF 10 MODULES…" → 4.

### MCI-2 / shutdown devices (row 20)
- `TSL-MCI-GEN2\xa0(1879359)` (SKU has a non-breaking space). Qty = the
  called-out shutdown-device count from PV-1 scope / PV-5 equipment schedule.
  ALWAYS present on plans. (Steele = 12.)

### J-boxes (rows 25-27)
- Rule: **ceil(strings / 4) per array, ≥1 per array, summed.**
- Roof: shingle → JB-1.2 (25), rail → JB-3 (26), metal → TOMBSTONE (27).
- **Ground mounts ALWAYS use EZSOLAR JB-3 (row 26).** (Steele 2 arrays × 2 strings → 2.)

### Roof-mount racking (rows 33-52) — formulas
(per-array; p=portrait modules, l=landscape, rows, int_rows)
- attachments = (p×2)+(l×3)+(rows×2)+(int_rows×2)
- deck screws = 4 × attachments; rail clamps = attachments
- rails = ROUNDUP(span/172)×2+1 (portrait uses SHORT span, landscape LONG)
- combo clamp (48) = (modules×2)+(rows×2)+2
- ground lugs = (int_rows×2)+rows; end caps = (rows×4)+(int_rows×4)
- wire clips = 2 × modules; S-clips = 6 × modules (template formula)
- Orientation read rotation-invariant (rail-angle vs module-long-edge-angle in
  raw frame; rail ∥ long edge = landscape). Row counting splits orientation runs.

### Ground-mount racking (rows 73-90) — read K2 BOM table(s), summed across arrays
- Read each PV-3.x BOM table via Vision; **sum quantities across all arrays.**
- Map plan part# → template row:
  - 4001370 rail → 73
  - 4000198 top cap → 76
  - 4000175 pipe bracket → 77
  - **4001221 EndCap → 78 = 2 × rail quantity, ALWAYS (even if absent on plan)**
  - clamps (4000135 mid+end, or 4000145 single) → **summed into combo clamp 4000145-US row 80**
  - 4000006-H ground lug → 82
  - **4000069 K2 wire clip → 83 = 2 × modules, ALWAYS (even if absent on plan)**
  - ground screws → 85
  - "Pipe - Length 10 ft" → **row 87 (120" Rear N/S Pipe 3")**
  - 3" pipe coupling → **row 90 (write SKU + desc), qty from plan**
- Template-driven skip: plan parts with no template row (e.g. K2 Cross Cap 4000312)
  are NOT filled.

---

## 4. ELECTRICAL BOM — RULES BY ROW

### AC disconnects (rows 5-12) + hubs (13-15)
- Read fused/non-fused + rating from one-line LABEL (not the symbol). Accepts
  "PV/ESS" or spelled-out "PHOTOVOLTAIC/ENERGY STORAGE SYSTEM".
- Non-fused: 30→5(DU221RB), 60→6(DU222RB), 100→7(DU323RB), 200→8(DU324RB).
- Fused: 60→10(D222NRB), 100→11(D223NRB), 200→12(D224NRB). (No 30A fused row.)
- Hubs = 2 × disconnect count per SIZE BUCKET (fused + non-fused combined):
  B075(13)=30A+60A, B125(14)=100A, B200(15)=200A.

### DC disconnects (rows 17-18) — by pole count
- 2-POLE = single string → row 17 (EN-EP200G). 4-POLE = two strings → row 18 (SI32).

### RSD device (row 19) — PE69-3020
- = **ceil(batteries / 3)**, min 1 if batteries exist; 0 if none (unless one-line notes).
- Battery count = PW3 + Enphase 10C + FranklinWH. **PW3 expansion EXCLUDED.**

### Lugs / grounding (rows 21-24)
- 21 PB2-300, 23 LK100ANCP, 24 SGB-386CL: SKU read from plans (qty from plan), else blank.
- **22 PK23GTACP = 1 per Tesla Energy Gateway.**

### Taps (rows 26-28) — fixed qty
- K4977-INT(26), NSI IT-3/0(27), NSI IT-250(28): if SKU on plans → **qty = flat 3**.

### PVC junction boxes (rows 30-31)
- SKU read from plans, else blank (manual).

### Enphase combiner / Envoy (rows 33-36)
- Combiner 5 → row 33 AND row 34 (cell kit CELLMODEM-07-NA-05), both = combiner count.
- Combiner 5C → row 35 (built-in cell kit, no row 34).
- IQ Envoy (36) = 1 ONLY when micros present AND no combiner of any kind (5/5C/6C).

### Enphase 10C battery block (rows 43-45)
- IQBATTERY-10-C-1P-DOM(43), X-IQ-AM1-240-6C-3BRK(44), MC-200-011-V01(45):
  SKU read with plan quantities.

### Control cable / hold-down / CT clamp (rows 47-49)
- 47 CTRL-SC3-NA-01 = **1 if any Enphase 5P or 10C battery present** (flat 1).
- 48 X-IQ-NA-HD-125A, 49 CT-200-CLAMP: SKU read with plan quantities.

### Tesla Powerwall / Gateway (rows 51-57)
- **PW3 precedence (a unit is ONE row, never two):**
  - exact `1707000-60-M-LR-2025` → row 51 (LR only)
  - any other 1707000 (e.g. -21, generic) → row 52 (Domestic default)
  - `1707000-11` (non-domestic) → row 53 NOT filled; **WARN**
- 54 Gateway (TSL-GTWY3): qty 1, sometimes 2.
- 55 Inverter (TSL-INVRTR-7600 / 1538000-45): SKU read.
- 56 Remote meter: NOT a SKU read — count "TESLA REMOTE ENERGY METER" blocks on PV-5.
- 57 Backup switch (TSL-BCKUPSWITCH): qty 1 by name.
- Gateway (54) and Backup Switch (57) never coexist → WARN if both.

### Tesla Expansion block (rows 59-65)
- 59 expansion (1807000) Domestic default, qty from plan. 60 non-dom NEVER (warn).
- Mounting kits (61 stack + 62 wall-mount) **must sum to unit count**. ≤1 stack
  ever; default wall-mount. Stack mentioned → 61=1, 62=units-1.
- Harnesses (63 -05 + 64 -20 + 65 -40) **must sum to unit count**:
  - 63 (-05 stack harness) = stack-kit count (+ any wall-mount unit explicitly
    overridden to -05; the one-line may call 1875157-05-y explicitly → use -05)
  - 65 (-40) only if mentioned exactly, max 1
  - 64 (-20) = remainder

### Panels (rows 71-79)
- 71-78 known load centers/MSPs: match new-panel SKU → row, qty 1+ (more if multiple).
- 79 special-order catch-all: unmatched SKU → write SKU to B79, desc to C79, qty 1.

### Meters (rows 81-96)
- 81-94 known utility meters (ComEd/WE/Ameren): SKU match → **qty always 1**.
  (Ameren U9551→89, U6281-XL-200-5T6→93, U6281-XL-100-5T9→92, etc.)
- 96 special-order: unmatched → B96 = "Special Order Meter: (SKU)" (replace
  "Part Number" token), C96 = "Special Order Meter: SKU", qty 1.

### Breakers (rows 98-114)
- **Inclusion rule (ALL must hold to order a breaker):**
  1. has a RATING next to the graphic. (No rating -> cannot order -> exclude.
     The existing MSP's existing branch breakers are typically UNRATED and
     UNLABELED, so this condition alone excludes most of them.)
  2. NOT labeled "(E)" (existing). **Absence of "(N)" does NOT exclude** — most
     BOM-required breakers carry NO label at all, so unlabeled != excluded.
  3. NOT the MAIN breaker of an MSP / service-disconnect combo (ships w/ panel).
  - **"Inside the MSP" is NOT a blanket exclusion.** A breaker physically in a
    panel is excluded only if it is the panel MAIN (cond. 3) or an existing
    unrated/unlabeled branch breaker (caught by cond. 1). A NEW, RATED breaker
    placed in a (new) MSP as the INTERCONNECTION breaker IS ordered.
  - **COUNTEREXAMPLE — Rose Sams:** a 125A/2P breaker inside a NEW Homeline MSP
    (the interconnection point to the AC disconnect + IQ Combiner 6C, Enphase
    10C plan) WAS required on the BOM. It is rated, not (E), and not the panel
    main, so it is included. No HOM2125 row exists -> row-128 fallback: write
    B128='HOM2125', C128='125A 2P BREAKER (HOMELINE)'. Trigger: only on Enphase
    10C plans and (occasionally) Tesla Backup-Switch plans where a new Homeline
    MSP is the interconnection point; breaker size varies.
  Net: ordered breakers are NEW, RATED breakers on the Gateway bus-kit, the
  Enphase 6C, new standalone locations, AND new interconnection breakers in a
  new MSP. Excluded: panel mains, and existing (unrated/unlabeled or (E)) breakers.
- **Bus-kit breakers DRAWN on one-line → BR row by rating/poles** (primary rule).
  2P: 15-125A → rows 101-110; 1P: 15/20/30 → 98/99/100. Covers Tesla (60A/battery)
  and Enphase (BR220/branch) since they're just drawn breakers.
- **BR260 floor:** Tesla Gateway projects → BR260 = max(drawn 60A count, battery qty).
- **CSR (112-114):** For EVERY Gateway project, check whether a breaker is drawn
  on the RIGHT side of the gateway, OUTSIDE the internal bus-kit. If present and
  rated (and not an MSP/service main — see exclusion rule below), map rating →
  CSR row (100→112 CSR2100, 125→113 CSR2125, 200→114 CSR2200), qty by rating.
  (Verified: Leslie Ebright's two breakers outside the bus-kit are both service
  mains — 100A/2P MSP main + 200A/2P meter-main-combo main → both excluded → no CSR.)
- **Enphase 6C battery breakers:** ≤2 → BR240×qty; =3 → BR240+BR280; ≥4 → 2×BR280.
- **Enphase 6C branch/PV-RSD (factory 60A):** ≤3 stays; =4 → BR280; =5 → BR2100 +
  BQ220240(111). >5 warns (6C max = 5).
- Main service panel breakers NOT included (come with the panel).
- 6C drawn breakers cross-checked vs computed; mismatch → warn (factory 60A not flagged).

### Homeline breakers (rows 116-128)
- HOM breakers only inside a Homeline MSP (Enphase 10C / Tesla Backup Switch jobs).
- 1P 15/20/30 → 116-118; 2P 15-100A → 119-127.
- 128 fallback: rating with no HOM row (e.g. 125A) → construct SKU (HOM2125) into
  B128 + desc into C128. (Sams fringe case.)

### Fuses (rows 129-144)
- FRN-R fuses 15-200A → rows 130-144. **Qty always 2 per fused disconnect.**
- Use the FUSE rating drawn INSIDE the block, NOT the disconnect rating (they differ).

### SPAN / EV / Detectors (rows 146-155)
- 146-148 SPAN MSPs: SKU read.
- 150-151 EV chargers (ChargePoint/Tesla): SKU read, qty forced 1.
- 153 heat alarm: SKU read. 154 smoke / 155 smoke+CO2 from one-line NOTES:
  CO2/carbon-monoxide mentioned → 155 ONLY (trumps 154); smoke alone → 154.

### Skipped
- Electrical rows 67-69: not filled. Rows past 155 not yet reviewed.

---

## 5. BUILD STATUS (modules in /home/claude/bom_engine/)

| Module | Covers | Validated against |
|--------|--------|-------------------|
| orientation_extractor.py | roof orientation/rows | Hacker, Drillinger, Eroh |
| template_filler.py | roof/ground row-block fill, headers, S-clip guard | Eroh, Fickas |
| ground_mount_bom.py | K2 BOM table read, clamp aggregation, template skip | Fickas, Sams, Steele |
| solar_micro_block.py | rows 11-18 + branch parser | Sams |
| electrical_bom.py | AC disco+hubs, DC disco, RSD, lugs, taps, PVC, combiner, 10C, accessory | Eroh, Fickas, Sams |
| tesla_block.py | rows 51-57 | Bennett (LR) |
| expansion_block.py | rows 59-65 | Fickas, Steele |
| panels_block.py | rows 71-79 | synthetic |
| meters_block.py | rows 81-96 | Eroh, Fickas, Sams, Bennett |
| breakers_block.py | rows 98-114 + BR260 floor + CSR + 6C cross-check | Sams, Steele |
| homeline_block.py | rows 116-128 | Sams (125A fallback) |
| fuses_block.py | rows 129-144 | synthetic + image |
| misc_block.py | rows 146-155 | synthetic |
| steele_fixes.py | MCI-2, ground J-box, K2 wire clips, pipe map, EndCap | Steele |
| filter_blank_rows.py | hide blank-QTY rows (full-table filter) | Steele |

**Validated real plansets:** Eroh (roof, 39mod, 3 arrays), Fickas (ground, 24mod,
1 PW3+1 exp), Sams (ground, 40mod, Enphase 10C+6C), Bennett (roof, LR PW3),
Steele (ground, 32mod, 2 arrays, 1 PW3+1 exp).

---

## 6. STEELE TEST RUN (#7482) — first full end-to-end + fixes found

Coperniq: Zone 3, Ground Mount, battery=Tesla PW3 qty 1, expansion 1,
utility=Ameren, service=Overhead, upgrade_needed=Yes. All matched planset.

**Six fixes surfaced (now encoded):**
1. MCI-2 missed — now reads called-out count (12).
2. Ground J-box not added — JB-3 always for ground, ceil(strings/4)/array (=2).
3. K2 wire clips — always 2×modules even if absent on plan (=64).
4. Pipe "Length 10 ft" → row 87 120" pipe (=16).
5. Pipe coupling → new line row 90, qty 8.
6. Expansion harness — explicit 1875157-05-y on one-line overrides default -20.
Plus: two 60A bus-kit breakers → BR260=2; 200A outside bus-kit → CSR2200.
Plus: ground EndCap rule 4001221 = 2×rail (=32).

**Root-cause insight:** most misses were EXTRACTION gaps (inputs hand-fed wrong),
not rule errors. The computed-quantity engine is well-validated; the unvalidated
risk is automated planset extraction of those inputs.

---

## 7. REMAINING WORK
1. **Orchestrator:** single function: planset + Coperniq dict → run all ~20 modules
   → merge {row:qty} + cell_writes (rows 79/96/128/90) + warnings → fill both
   sheets → apply_qty_filter → output BOM_First_Last.xlsx.
2. **Planset extractor** (highest risk): reliably pull MCI count, strings/array,
   drawn breakers + locations, harness SKUs, disconnect labels, meter SKU,
   remote-meter blocks — with confidence flags for human review.
3. Review template rows past 155.
4. Coperniq download/upload automation — runs in user's environment (connector +
   API key), not the dev sandbox.

---

### JOSEPH WOROSZYLO (#857222) — metal-roof S-5! SolarFoot job; THREE rule corrections (user)
- 29 Sirius 450W, 11 MCI-2, 1 PW3, 2 expansion, Tesla Gateway 3, STANDING SEAM 24ga METAL roof
  (HO self-installing metal roof via 3rd party; Coperniq comment 4192065). Ameren OH, upgrade=Yes
  (meter base -> new Milbank U9551). Roof1 az180 (20 mod, rail runs 1,6,4,3,3,3 = 6 rows), Roof2
  az360 (9 mod, stair-step 2,3,4 = 3 rows). ALL 29 LANDSCAPE (per-cell aspect ~1.55-1.58 verified).
- ATTACHMENT: PV-4 draws (N) S-5! SOLAR FOOT pedestal + SEPARATE (N) L-FOOT (PV-14 datasheet: L-foot
  NOT included). Routed Solar row 38 (S5-SOLARFOOT)=102 + row 39 (L-FOOT)=102. NOT row 37 (ProteaBracket
  which comes with L-foot). Deck screw/lag (33-35) BLANK — S-5! ships own self-drilling truss screws.

### RULE CORRECTION 1 — RAILS FORMULA (user, supersedes per-row version)
OLD f_rails was per-row ceil(row_width/172)*2 summed -> rounded up 9 separate times -> Woroszylo 34
vs planset 23 (delta +11, the worst cross-check on the job). CORRECTED:
  rails = ceil( (landscape*67.8 + portrait*44.65) / 172 ) * 2 + 1
Round up ONCE over the COMBINED span (a 172" rail spans multiple modules and continues across row
gaps within a plane). Woroszylo: ceil(29*67.8/172)*2+1 = ceil(1966.2/172=11.43->12)*2+1 = 25 vs
planset 23 (delta +2). Implemented globally over all arrays. Rails remain PLANSET-DELIVERED; this is
the corrected cross-check basis.

### RULE CORRECTION 2 — 1.066 ATTACHMENT FACTOR IS PROTEABRACKET-ONLY (user)
The 1.066 factor models S-5! ProteaBracket's TIGHTER 45" spacing ONLY. It DOES NOT apply to S-5!
SolarFoot (standard spacing — bare formula is correct) NOR to K2 shingle. resolve_racking() now takes
attachment_type ('K2_SHINGLE'/'S5_PROTEABRACKET'/'S5_SOLARFOOT'); check_1066 only computes for
S5_PROTEABRACKET, else returns applies=False. Woroszylo (SolarFoot): factor would give ceil(105*1.066)
=112 vs planset 102 (+10 overshoot) — proves it must be gated off SolarFoot.

### RULE CORRECTION 3 — TESLA GATEWAY 200A IS ALWAYS A NEW CSR (user, supersedes Woroszylo v1 + Ebright)
There is NEVER an existing/excluded breaker drawn inside the Tesla Energy Gateway. EVERY rated breaker
drawn in the Gateway OUTSIDE the internal bus-kit is a NEW CSR main breaker and MUST be ordered every
time, mapped by rating: 100->CSR2100(112), 125->CSR2125(113), 200->CSR2200(114). NO "is it the gateway
main / existing" judgment. This FIXES the Woroszylo-v1 miss (200A/2P wrongly excluded as "internal
main" -> CSR2200 was omitted). New function tesla_gateway_breakers(buskit_60a_2p_count,
battery_pw3_count, gateway_breakers_outside_buskit): BR260=max(buskit60,PW3); each outside-buskit
rating -> its CSR row. Woroszylo: BR260=1 + CSR2200=1. (Reconciles with Eroh "200A CSR in TEG" as the
universal rule, not a special note. NOTE: this REVISES the prior Ebright reading where a 200A outside
the bus-kit was called a service main and excluded — inside a TEG it is a CSR.)

### NEW CANONICAL TEMPLATE (user): BOM_TEMPLATE.xlsx
Replaces out_roof_eroh_TEMPLATE.xlsx / Evrard base. Diffs: Solar row6 = Q.PEAK DUO BLK ML-G10+ 410W
(QCELL, was Hyundai 435), row9 = HIMO5 DC LR5-54HPB (415W LONGI). Electrical row128 = HOM2125 now a
real menu row (was fallback). Rows 5/7/8 modules + A30 S-clip formula unchanged. USE THIS GOING FORWARD.

### RULE — MODULE DIMENSIONS ARE PER-MODEL, READ FROM PV-3 (user, Woroszylo follow-up)
The rail span (and the orientation dim-check) must use the ACTUAL module long/short edge, which
differs per module model (Sirius 67.80x44.65, but QCELL/Hyundai/Aptos/LONGi differ). Read from PV-3
in TWO places that must agree: the "MODULE TYPE, DIMENSIONS & WEIGHT" text block ("MODULE DIMENSIONS
= 67.80\" x 44.65\"") AND the dimensioned module graphic (e.g. 44.65" wide x 67.80" tall callout).
Implemented: new ModuleDims class (long_in/short_in, ModuleDims.from_pv3(a,b) auto-orders long>=short).
f_rails(arrays, module_dims), f_splice(arrays, module_dims), edge_along_rail(orient, module_dims),
and resolve_racking(..., module_dims=) all take it; LONG_IN/SHORT_IN remain ONLY as the Sirius
default/fallback. crosscheck['rails']['module_dims'] records the dims + source. NEVER hardcode dims
for a real build — pass the PV-3-confirmed ModuleDims.

### TEMPLATE UPDATE (user): BOM_TEMPLATE.xlsx LONGi SKU corrected
Latest BOM_TEMPLATE.xlsx sets Solar row9 LONGi SKU = 'LR5-54HPB-415M' (the value found on the plans),
replacing the prior 'HIMO5 DC LR5-54HPB'. Everything else identical (rows 5/7/8, A30 S-clip,
Electrical row128 HOM2125). This is the canonical template going forward.

### RULE CORRECTION — GATEWAY BR vs CSR IS DECIDED BY BUS-KIT MEMBERSHIP, NOT RATING (user, Dare #868257)
SUPERSEDES the earlier "every rated breaker outside the bus-kit is a CSR / rating decides" framing.
The TRUE discriminator is WHERE the breaker sits in the PV-5 one-line:
  - INSIDE the "GATEWAY INTERNAL BUS-KIT" enclosure (left-side box) -> BR breaker, rows 98-110
  - OUTSIDE it, on conductors entering the gateway from the RIGHT  -> CSR main, rows 112-114
Rating + poles (read off the plan label, e.g. "60A/2P", "100A/2P") selects the ROW via an
(amperage, poles) lookup. This reconciles the whole history: Woroszylo's 200A was OUTSIDE the
bus-kit -> CSR2200 (row 114); Dare's 100A is INSIDE the bus-kit -> BR2100 (row 109). Rating never
decided BR-vs-CSR — position did.
BR (amp,pole)->row: (15,1)98 (20,1)99 (30,1)100 (15,2)101 (20,2)102 (30,2)103 (40,2)104 (50,2)105
  (60,2)106 (70,2)107 (80,2)108 (100,2)109 (125,2)110.  CSR amp->row: 100->112, 125->113, 200->114.
New signature: tesla_gateway_breakers(buskit_breakers=[(amp,poles),...], csr_breakers=[amp,...],
  battery_pw3_count=N) -> (rows, flags). RECONCILIATION GATE (HARD): count of 60A/2P bus-kit breakers
  must equal PW3 count (one 60A/2P backup per PW3); mismatch -> HARD flag (hold + post to Teams),
  never guess. Also HARD-flags an unmapped rating or a gateway-present-but-no-breakers-classified read.
DARE RESULT: 2x BR260 (row106=2) + 1x BR2100 (row109=1), NO CSR.

### RULE CORRECTION — ROWS MODEL SIMPLIFIED: INTERRUPTED RUNS = SEPARATE ROWS (user, Dare #868257)
RETIRES the separate "interrupted rows" (B7) quantity. A rail run split by a gap/obstruction/
orientation change into k contiguous segments now counts as k ROWS. So B7 is always 0; every
former interrupted row is just another row. This makes row detection a tractable "count contiguous
module segments per rail line" problem instead of the fragile "detect a gap inside a run" judgment.
Dare: was rows=8 + interrupted=1 -> now rows=9, interrupted=0.

RACKING FORMULAS (authoritative source: user's BOM tool "Copy_of_BOM_tool.xlsx", BOM Builder sheet):
  - Attach (cross-check)  = portrait*2 + landscape*3 + rows*2        [tool C8, B7 term dropped]
  - Combo clamp (check)   = total_modules*2 + rows*2 + 2             [tool C11]
  - End cap (cross-check) = rows*4                                   [tool C12, B7 term -> 0]
  - Ground lug (TRUTH)    = rows + 1   (tool C15 = B6 + B7*2; B7=0; +1 lug default EVERY project)
  - Wire clip (TRUTH)     = 2*total_modules                         [tool C19]
  - Rail (TRUTH=plan; cross-check formula) = roundup(module-width span/172)*2+1
SOURCING: attach, combo, end cap, rails, splice = PLANSET-SOURCED (truth); the formulas above are
CROSS-CHECKS for those. Ground lug, wire clip, deck screw = FORMULA-TRUTH.
COMBO CLAMP truth = mid clamps + end clamps summed from the PLANSET BOM table (Dare 72+36=108);
the C11 formula is its cross-check (Dare formula=110, within tolerance).
END CAP truth = planset end-clamp qty (Dare 36); rows*4 formula is the cross-check.

CROSS-CHECK TOLERANCE (user, asymmetric): pass band = plan-3 <= formula <= plan+2.
  Flag HARD if formula - plan > 2 (too high) OR plan - formula > 3 (too low).
  racking_crosscheck(item, plan_value, formula_value, attachment_type=None) -> flag|None.
A row MISCOUNT large enough to matter trips the attach/combo/end-cap cross-checks (proven: rows
read as 6 or 13 vs true 9 all flag), so a bad vision read of rows fails LOUD (hold + Teams), never
silently corrupts the BOM. The only quantity rows alone determines is ground lug (+/-1, low stakes).

PROTEABRACKET 45in-SPACING DOWNGRADE (user, Nelson REVA #860742): on a LARGE metal S-5!
ProteaBracket roof the installer tightens attachment spacing from 48" to 45", adding attach points
the cross-check formula cannot predict (the formula has no spacing term and is not built on 48"
spacing). The 1.066 factor approximates this but never lands cleanly (210/1.066=197; no row count
*1.066 hits 210). Result: a benign attach UNDERSHOOT by the formula and a knock-on combo OVERSHOOT
(combo's row term is unaffected while its module term is fixed). NEITHER is a row-count error.
  STRUCTURAL FIX: racking_crosscheck() takes attachment_type; for attachment_type=='S5_PROTEABRACKET'
  the 'attachments' and 'combo_clamp' deltas are emitted as level='NOTE' (confidence report only),
  NOT a HARD hold. Plan attachment qty is delivered as truth unchanged. SCOPED so it can't mask a
  real miscount: end_cap stays HARD even on ProteaBracket; every other attachment_type (SolarFoot,
  K2 shingle, None) stays HARD on attach/combo too. resolve_racking() now emits these as
  crosscheck["tolerance_flags"]; a build with only NOTE-level flags PROCEEDS.
  Nelson: attach plan=210 formula=201 (-9) -> NOTE; combo plan=140 formula=144 (+4) -> NOTE; 0 holds.

DC DISCONNECTS (Electrical rows 17/18, discriminated by pole count in the PV-5 item description):
  row 17 = EN-EP200G-NA-02-RSD (SI16-PEL64R-2) "16A IMO DC DISCONNECT (SINGLE STRING)" = 2-POLE
  row 18 = SI32-PEL64R-4 "32A IMO DC DISCONNECT (TWO STRINGS)" = 4-POLE
  Dare: one 4-pole + one 2-pole -> row17=1, row18=1.

HONEST LIMIT ON ROW DETECTION: the vision read of row count is NOT guaranteed correct (same error
family as the orientation misreads). It is made safe by being cross-checked against 3 independent
planset-sourced values (attach, combo, end cap); any material miscount flags to Teams and holds.

### CONFIRMED ELECTRICAL/SOLAR ROWS + RULES (user, Dare #868257) — DO NOT RE-DERIVE
  Electrical row 6  = DU222RB "60A SQ-D NON-FUSED DISCONNECT"  -> qty from PLAN (Dare=4)
  Electrical row 19 = PE69-3020 "IMO Isolator 3 Pole 20A Enclosed IP66" (RSD switch)
        -> ALWAYS >=1; increases to 2 if MORE THAN 3x Powerwall 1707000 units installed. Dare(2 PW3)=1.
  Electrical row 22 = PK23GTACP "SQUARE D 23 TERM GROUND BAR KIT"
        -> qty 1 WHENEVER A GATEWAY IS INSTALLED. Dare=1.
  Electrical row 52 = TSL-PWRW3-DOM (1707000-21) Tesla Powerwall 3 -> plan qty (Dare=2)
  Electrical row 54 = TSL-GTWY3 (1841000) Tesla Energy Gateway 3   -> plan qty (Dare=1)
  Solar row 20      = TSL-MCI-GEN2 (1879359) Tesla MCI-2           -> plan qty (Dare=15)
  CONDUCTORS/CONDUIT = OUT OF SCOPE (never ordered on the BOM).
  METER = ordered ONLY if the planset draws a NEW meter/socket. Dare = existing bi-directional
        meter, no new meter drawn -> NO meter line.

### J-BOX RULE (confirmed, user, Dare #868257)
J-box qty = sum over ARRAYS (physical roof planes) of ceil(strings_on_that_roof / 4), min 1 per array.
  - ONE J-box per array (roof plane) by default.
  - A J-box holds at most 4 STRINGS; if a single array has >4 strings, that array needs a 2nd J-box
    (ceil(strings_on_roof/4)).
  - Count strings PER PHYSICAL ROOF (not per inverter, not system-wide).
SKU by roof type: shingle -> EZSOLAR JB-1.2 (Solar row 25); all other roof types + ground -> EZSOLAR
  JB-3 (Solar row 26). (Metal -> JB-3.)
DARE: 3 roofs, 6 strings total (none >4 on a single roof) -> 1 J-box each -> 3x JB-3 (row 26).

### RAIL CLAMP + BLANK-ROW FILTER (confirmed, user, Dare #868257)
RAIL CLAMP (Solar row 46, 4000770 K2 Rail Clamp) = 1 PER ATTACHMENT. Implemented as a live formula
  '=A37' (references the attachment qty row). Matches BOM tool C13 = C8. Dare: 154.
BLANK-ROW FILTER (filter_blank_rows.py): hide every data row (row 5+) whose col-A qty is empty OR
  zero, on BOTH Solar BOM and Electrical BOM. Exempt: formula rows (e.g. A30 S-clip) kept if they
  hold a formula. MECHANISM: a REAL AutoFilter anchored on the FULL table range (A4:D<last>, NOT
  just col A) + matching row .hidden flags. Rows are FILTERED, not deleted -- the user can clear the
  filter in Excel and all hidden rows reappear. (Prior bug: hid via row_dimensions only with the
  AutoFilter scoped to col A, so unfiltering didn't reveal rows.)

### OUTPUT MUST BE STATIC VALUES, NOT FORMULAS (user, Dare — Protected View bug)
Excel Protected View does NOT calculate formulas, and openpyxl writes formulas without a cached
result, so any formula cell renders BLANK until the user clicks "Enable Editing". This hit the
S-clip cell (template-native '=IF(COUNT(A5:A9)>0,SUM(A5:A9)*6,"")', Solar row 30) and the rail
clamp ('=A37', Solar row 46) — both showed blank in Protected View.
FIX: the engine writes COMPUTED NUMERIC VALUES for every qty cell, including overwriting the
template's S-clip formula. Rules unchanged, just materialized:
  - S-clips (Solar row 30) = 6 * total_modules  (Dare 45*6 = 270), written as a number.
  - Rail clamp (Solar row 46) = attachment qty (Dare 154), written as a number (not '=A37').
NO formulas in column A of either sheet in the delivered BOM. (Confirmed 0 formula cells.)

### DISCONNECT B-HUBS (Electrical rows 13/14/15) — driven by AC DISCONNECTS, NOT DC (user, Dare)
B-hubs are sized to the AC non-fused disconnects (rows 6 etc.), two hubs per disconnect (line+load):
  row 13 = B075  "3/4 B Hub SQ-D (30/60A Disco)" = 2 * (count_60A + count_30A AC disconnects)
  row 14 = B125  "1-1/4\" B Hub SQ-D (100A Disco)" = 2 * count_100A AC disconnects
  row 15 = B200  "2\" B Hub SQ-D (200A Disco)"     = 2 * count_200A AC disconnects
NOT contingent on DC disconnect (rows 17/18) quantities. f_disconnect_hubs({amp:count}) -> rows.
DARE: 4x 60A AC disconnects -> row 13 = 8; rows 14/15 blank.

---

## SESSION ADDENDUM — Marilyn Roland REVA (#913 S 10th St, Herrin IL) — SHINGLE / BACKUP SWITCH / MIXED-ORIENTATION

Reference project for: K2 shingle deck-mount + Tesla Backup Switch (no Gateway) + mixed L/P orientation.

### STRUCTURAL FIX: electrical blocks moved out of per-project scripts into `electrical_engine.py`
Every electrical block is now a function that decides its own applicability and returns {row:qty}+flags:
ac_disconnects, dc_disconnects, rsd_device, ground_bar, supply_side_taps, tesla_core,
tesla_expansion, homeline_interconnection. Consolidated builder runs them ALL unconditionally.
This kills the "per-project script misses whole blocks" failure class (it bit Roland: expansion
rows 59/61/63, NSI IT-3/0, and fuses were all initially omitted).

### STRUCTURAL FIX: J-box (`racking_engine.f_jboxes`)
count = sum over PHYSICAL ROOF PLANES of max(1, ceil(strings_on_plane/4)).
FLOOR = number of roof planes (>=1 box per plane); a plane with >4 strings adds boxes.
shingle -> JB-1.2 row 25; all other roof + ground -> JB-3 row 26.
Roland: 3 planes, each <=4 strings -> 3x JB-1.2.

### Mixed orientation resolved by cross-check, not eyeball (orientation gate paid off)
PV-3 plan-truth attach=78, rails=17, combo=mid28+end40=68. az 91/271/01 are sideways planes
(rails run vertical on page; rotate (180-az) -> horizontal -> modules LANDSCAPE along rail).
All-landscape attach formula overshot by +6..+18 across every plausible row count -> SIGNAL that
modules are NOT all landscape. Solving attach=L*3+P*2+rows*2 AND combo=(L+P)*2+rows*2+2 AND
rails=ceil((L*LONG+P*SHORT)/172)*2+1 simultaneously against the three plan anchors gives the unique
TRIPLE-EXACT solution: 12 landscape + 12 portrait, 9 rows (all three deltas 0). 24 Sirius 450W.
LESSON: when the all-landscape attach formula is far above plan, that gap IS the portrait count;
back out L/P from the plan anchors rather than trusting the as-drawn page orientation.

### Electrical rules exercised / reconfirmed (Backup Switch, NO Gateway)
- AC disco: one-line draws FUSED 60A -> D222RB row 10 + 2x FRN-R-60 row 138. (No ground bar:
  PK23GTACP is GATEWAY-ONLY; Backup-Switch jobs get NONE.)
- DC disco by pole: 4-pole (two strings) row18 + 2-pole (single string) row17.
- Hub: 2 per 60A disco -> B075 row13 = 2.
- RSD ceil(batteries/3)=1 (PW3 expansion EXCLUDED from battery count).
- NSI IT-3/0 supply-side tap mentioned on one-line -> row 27 flat qty 3 (same flat-3 rule as K4977).
- PW3 1707000 generic -> row 52 Domestic. Tesla Backup Switch 1624171 -> row 57 (IS on the BOM).
- Expansion 1807000 -> row 59. One-line explicitly calls EXPANSION HARNESS 1875157-05-X and
  "CONNECTED PARALLEL... WITH EXPANSION HARNESS" => STACKED: stack kit row 61=1, -05 harness row 63=1.
  (Mount kits must sum to units; harnesses must sum to units.)
- Meter EXISTING + MSP EXISTING (E) -> neither on BOM. No gateway breakers (no gateway).

### Roland validated BOM
Solar: 24 Sirius450 / 8 MCI-2 / 3 JB-1.2 / 144 S-clip / 78 K2-shingle-attach / 78 lag / 312 deck /
17 rail / 78 rail-clamp / 2 splice / 68 combo / 10 ground-lug / 36 end-cap / 48 wire-clip.
Electrical: 1 D222RB(60A fused) / 2 B075 / 1 DC-2P(17) / 1 DC-4P(18) / 1 RSD / 3 NSI IT-3/0 /
1 PW3(52) / 1 Backup Switch(57) / 1 Expansion(59) / 1 stack kit(61) / 1 -05 harness(63) / 2 FRN-R-60.

### ROLAND CORRECTIONS (round 2) — 4 error classes fixed structurally
1. DECK vs RAFTER SCREW (rows 34/35) — `racking_engine.f_k2_shingle_screws(attach, deck_mounted,
   rafter_mounted)`. MUTUALLY EXCLUSIVE PER ATTACHMENT by fastening type read from PV-3/PV-4:
   rafter/truss-mounted -> 1 lag (4000170 r34) each; deck-mounted -> 4 deck screws (4000310 r35) each.
   deck+rafter MUST = attach. Roland all-deck -> r34=0, r35=312. (Was wrongly populating both.)
2. ORIENTATION via cross-check is NOT self-validating — 3 unknowns vs 3 anchors WILL fit a wrong
   (L,P,rows). My 12L/12P/9row "triple-exact" was a FALSE fit; truth = 10L/14P. attach=L*3+P*2+rows*2
   is the cleaner ROW anchor (rows term isolated); it lands EXACT at rows=10 with 10L/14P. So
   end_cap=rows*4=40, ground_lug=rows+1=11 (both pure formula-truth off the row count). LESSON: when
   as-drawn read and back-solve disagree, RE-COUNT rail runs from the rotated raster; never let a
   3-anchor fit certify orientation by itself.
3. DC DISCONNECTS — only when DRAWN on the one-line / in the equipment schedule. Roland has NONE
   (strings -> MCI-2 RSDs -> JB -> PW3, no DC disco device). Do NOT carry over Williford's 2P/4P read.
4. EXPANSION MOUNT — stacking is a PHYSICAL-mount fact, NOT inferable from a harness P/N. Default
   WALL-MOUNT (kit 1978069 r62 + -20 harness r64). Only stack on an explicit stack callout. A bare
   1875157-05 mention does NOT imply stacked. (Was wrongly stacking Roland off the -05 P/N.)

ROLAND FINAL: Solar 24/8/3 JB-1.2/144 S-clip/78 attach/0 lag/312 deck/17 rail/78 rail-clamp/2 splice/
68 combo/11 ground-lug/40 end-cap/48 wire-clip. Electrical 1 D222RB(60A fused)/2 B075/1 RSD/3 NSI IT-3/0/
1 PW3/1 Backup Switch/1 Expansion/1 WM kit(62)/1 -20 harness(64)/2 FRN-R-60. NO DC disco, NO ground bar,
NO gateway breakers, NO meter, NO MSP.

### END CAP SOURCING CORRECTION + ENGINE-PATH DISCIPLINE
- END CAP (row 51) DELIVERED = PLANSET END-CLAMP qty (TRUTH). rows*4 is the CROSS-CHECK only.
  (resolve_racking now delivers planset["end_clamps"]; f_end_cap = cross-check.) Was wrongly
  delivering the formula — number matched by coincidence on Roland (both 40) but the RULE was inverted.
  Ground lug (rows+1) is the ONLY racking qty still riding purely on the row count.
- DISCIPLINE: every output is now built by CALLING the engine functions (resolve_racking,
  f_k2_shingle_screws, f_jboxes, electrical_engine.*), NOT by hardcoding numbers into the sheet.
  An audit harness (inspect.getsource checks) confirms each fix is present in engine source so a
  fixed error class cannot silently reappear via a bypassing build script.
- Roland reconciles with rows=10: attach=L*3+P*2+rows*2 = 10*3+14*2+20 = 78 (EXACT, match), end_cap
  cross-check 40 vs plan 40 (delta 0), ground_lug=11.

### JONATHAN MUNOZ REVA (#15936 E 580 N RD) — first K2 GROUND MOUNT + Tesla Gateway 3
52 Sirius 450W, 18 MCI-2, K2 ground mount (rows 73-90), 2 PW3 + 1 expansion, Tesla BACKUP GATEWAY 3,
2x non-fused 60A AC disco, DC = 2x 4-pole + 2x 2-pole, new Milbank U9551 Ameren meter (utility upgrade).

### STRUCTURAL FN: `racking_engine.k2_ground_mount(bom_table, has_enphase_micros, module_count)`
Reads the PV-3.1 K2 BOM table straight to template rows 73-90. FOUR GM RULES (user, Munoz), all in code:
  1. NEVER include HEYClip SunRunner Cable Clip SS (4000382) on a GM BOM — dropped.
  2. ONLY include CR Micro inverter & OPT 13mm Hex (4000629-H, row 81) when project has ENPHASE
     micro-inverters. Tesla-MCI GM (like Munoz) -> excluded.
  3. NEVER include K2 Cross Cap (4000312) on a GM BOM — dropped.
  4. Pipe Coupling 3" (Third Party) -> ALWAYS create new SKU in ROW 84: B84=C84="Pipe Coupling 3\"
     (Third Party)", qty from plans (returned via cell_overrides).
GM table->row map: 4001370/216"rail->73, 4000708/172"rail->74, 4001196 splice->75, 4000198 top cap->76,
4000175 pipe bracket->77, 4001221 endcap->78, 4000145 combo(mid+end summed)->80, 4000006-H ground lug->82,
ground screw->85, ns_pipe_front_60->86, ns_pipe_rear_120->87, ew_pipe(10ft)->88, diag_brace->89.

### Gateway breakers (bus-kit membership rule, confirmed again)
Bus-kit: 2x 60A/2P (matches 2 PW3, hard gate PASS) -> 2x BR260 (r106). Outside bus-kit: 200A/2P ->
CSR2200 (r114). J-box ground rule: JB-3 (r26), 1 plane / 6 strings -> ceil(6/4)=2. S-clips still apply
on GM (6*modules=312) — they fasten modules to rails regardless of roof/ground.

MUNOZ FINAL: Solar 52/18 MCI/2 JB-3/312 S-clip/26 rail(73)/12 topcap(76)/52 pipe-bracket(77)/130 combo(80)/
13 ground-lug(82)/14 pipe-coupling-3"(84 NEW SKU)/12 ground-screw(85)/21 ew-pipe(88).
Electrical: 2 DU222RB(60A nf)/4 B075/2 DC-2P/2 DC-4P/1 RSD/1 ground bar/2 PW3/1 Gateway3/1 expansion/
1 WM kit/1 -20 harness/1 Ameren meter(89)/2 BR260/1 CSR2200.

### K2 GROUND MOUNT — 3 MORE RULES (user, computed/mapping; in k2_ground_mount())
  5. END CAP (4001221, row 78) = total RAIL qty (rows 73+74) * 2, ALWAYS. COMPUTED, not table-read
     (table value ignored). Munoz: 26 rail * 2 = 52.
  6. WIRE CLIP (4000069, row 83) = module_count * 2, ALWAYS. COMPUTED, not table-read. Munoz: 52*2=104.
  7. "Pipe - Length 10 ft (Third Party)" IS the 120" Rear N/S Pipe 3" -> ROW 87 (was wrongly row 88).
     Munoz: 21.
Engine now skips any table end-cap/wire-clip line (_COMPUTED_SKIP) and computes 78 & 83; ew_pipe/pipe_10ft
map to row 87.

### EXPANSION HARNESS / MOUNT KIT — CORRECTED (user, Munoz image) — SUPERSEDES prior expansion rule
tesla_expansion(unit_count, harness_pn, mount) — TWO INDEPENDENT READS:
  A) HARNESS (rows 63/64/65) = read the EXPLICIT harness P/N off the plan one-line. NEVER default,
     NEVER infer from mount. '1875157-05-X' -> row 63 (-05 stack, 1.64ft); '-20' -> row 64 (wall,
     6.56ft); '-40' -> row 65 (wall, 13.12ft). qty = unit_count. Missing P/N -> HARD flag.
  B) MOUNT KIT (61 stack / 62 wall) applied ONLY when the expansion DESCRIPTION explicitly says
     "wall mount" or "stack"/"stacked". Description that only says "connected parallel" (Munoz,
     Roland) -> NO kit row. No default kit.
The harness P/N and mount keyword are SEPARATE signals: -05 harness does NOT imply a stack kit;
absence of a mount word does NOT suppress the harness. This corrects BOTH the Roland round-2 rule
(which defaulted wall-mount -> -20 r64) AND the earlier Roland stack inference. Munoz & Roland both
print 1875157-05-X with "connected parallel" -> row 59 + row 63 only, no kit.

### EXPANSION — FINAL RULE (user) — kit is MANDATORY
tesla_expansion(unit_count, harness_pn, mount):
  A) HARNESS (r63/64/65) = ALWAYS the EXPLICIT plan P/N (-05→63, -20→64, -40→65); harness MATCHES
     THE PLANS, never defaulted/inferred. qty=unit_count.
  B) MOUNT KIT (r61 stack / r62 wall) — there is ALWAYS a kit, one per unit. DEFAULT WALL-MOUNT
     (r62) UNLESS the description says "stack"/"stacked" (then r61). Mandatory regardless of harness.
Corrects the prior "no kit if description silent" mistake. Munoz & Roland: harness -05 (r63) +
wall-mount kit (r62 default) + unit (r59).

### LARRY LACKEY REVA (#221 E North St) — shingle roof + Gateway 3 + 2 PW3 + 1 expansion (validated)
58 Sirius 450W, 2 roofs (32 az269 + 26 az89, both sideways→landscape after rotation), all deck-mount.
Orientation solved from plan anchors: combo 134 → rows=8; attach 184 → 52L+6P (attach & combo EXACT).
Rails formula 47 vs plan 51 (formula LOWER by 4 = acceptable, waste; deliver plan 51). end_cap=plan 36,
ground_lug=rows+1=9, wire_clip=2*58=116, S-clip=348, deck screw=4*184=736 (all deck), rail_clamp=184.
J-box shingle JB-1.2: 2 planes (4 strings + 3 strings, each ≤4) → 2. Gateway bus-kit = 2x60A/2P + 1x100A/2P
→ 2 BR260 + 1 BR2100, NO CSR (nothing outside bus-kit; line goes "to existing equipment"). Matches Dare.
Expansion 1875157-05-X → harness r63 + wall-mount kit r62 (default, no stack word). 2 non-fused 60A disco,
4 B075, RSD 1, ground bar 1 (gateway), existing meter/main combo → NO meter line.

### J-BOX FIX + MOUNT-KIT MASTER-NOTE PRECEDENCE (user, Lackey)
- J-BOX: the per-plane formula max(1,ceil(strings_on_plane/4)) was already correct; the BUG was the
  INPUT string-per-plane count. Lackey Roof #1 carries 5 strings (#1-#5 route on the left plane),
  ceil(5/4)=2; Roof #2 = 2 strings -> 1; total 3 (was wrongly 2). Extractor must count strings PER
  PLANE from PV-3.1 routing, not split by module count.
- EXPANSION MOUNT KIT precedence = resolve_expansion_mount(plan_mount, master_notes):
  1. PLANS FIRST (truth): planset description says stack/wall -> use it.
  2. MASTER NOTE (Coperniq project custom fields, parse for stack/wall): design_notes,
     installation_notes, field_installation_notes.
  3. DEFAULT: neither -> WALL MOUNT.
  Harness still ALWAYS matches the explicit plan P/N (independent of mount). Lackey: plan silent,
  master note unavailable in this session -> defaulted wall (row 62) + -05 harness (row 63); flag to
  check master note in the automated pipeline (orchestrator passes master_notes from get_project).
Coperniq master-note keyNames: design_notes, installation_notes, field_installation_notes.

### EXTRACTOR STRUCTURAL CHANGES (user, Lackey) — built into extractor.py, run EVERY project
1. PV-3.1 STRING MAP — new _extract_string_map() step. A STRING = one dashed line of a SINGLE color;
   the STRING LEGEND maps color->string#. Strings NEVER cross roof planes. The step counts DISTINCT
   string COLORS confined to each physical plane -> strings_per_plane{plane:count}. This (not module
   count) drives J-boxes: per plane max(1,ceil(strings_on_plane/4)). ArrayInfo gains roof_plane +
   strings_on_plane; PlansetData gains strings_per_plane. Lackey: {1:5, 2:2} -> 2+1 = 3 JB.
2. MASTER-NOTE MOUNT RESOLUTION wired into extract(pdf, coperniq_project): every project now calls
   master_notes_from_coperniq(project) (strips HTML/&nbsp; from design_notes/installation_notes/
   field_installation_notes) then resolve_expansion_mount(plan_mount, master_notes). PlansetData gains
   expansion_mount_kit. Precedence: plan keyword (truth) -> master note -> default wall. The pipeline
   passes the get_project() dict so this is automatic, not per-project.
PROJECT 852515 (Lackey, #7605): pulled get_project — the 3 master-note fields are UNSET/empty ->
resolve falls to DEFAULT WALL. Lackey wall-mount kit (row 62) is therefore correct; no change.

### MASTER NOTE IS A FORM, NOT A CUSTOM FIELD (user, Lackey correction)
The Master Note is a Coperniq FORM (name "Master Note"), not project.custom.design_notes (which is
empty). Fetch path (MCP): list_project_forms(project_id) -> find name=="Master Note" ->
get_form(form_id). Form 1343636 (Lackey) carries TEXT fields "Design notes:" and "Additional Notes:".
master_notes_from_coperniq(project=None, form=None) now WALKS form.formLayouts[].fields[], matching
field names design notes / additional notes / installation notes; strips HTML; falls back to
project.custom only if the form lacks them. extract(pdf, coperniq_project, master_note_form) passes
the fetched form. resolve_expansion_mount scans design_notes + additional_notes + install notes.
LACKEY 852515: note says "PW3 Expansion wired to leader PW3 with stack kit (1978070) and harness
(1875157-05-y)" + "PW3+PW3 exp stacked units" -> STACK. Corrected: row 61 stack kit (was wrongly
defaulted wall row 62) + -05 harness row 63 + unit row 59.
NOTE also contains (future structural mining): NO CSR in TEG, 100A relocated to TEG internal
panelboard, MSP lands on TEG backed-up lugs, deck-mount ONLY 48" spacing, RSD PE69-3020, existing
Eaton meter/main + Siemens 200A MSP retained (no meter line), ground bar may be needed in MSP.

### CAROL/HARRY BRAND REVA (#304 S Kenney St) — K2 GROUND MOUNT + Gateway 3, NO expansion (validated)
40 Sirius 450W, 16 MCI-2, K2 ground mount, 2 PW3, Tesla Backup Gateway 3, 2x non-fused 60A AC disco,
2x 4-pole DC disco, new Milbank U9551 Ameren bi-directional meter. NO expansion unit (mount/harness
N/A). Single ground plane, 4 strings (#1-#4, 10 mod each) -> JB-3 ceil(4/4)=1.
GM block: 20 rail(73)/10 topcap(76)/40 pipe-bracket(77)/40 endcap(78=20*2)/100 combo(80=60+40)/
10 ground-lug(82)/80 wire-clip(83=40*2)/10 pipe-coupling-3"(84 NEW SKU)/10 ground-screw(85)/
18 rear-N-S-pipe(87). 4 GM exclusions applied (no HEYClip/CrossCap/micro-lug). S-clips 240.
Gateway bus-kit 2x60A/2P -> 2 BR260; outside 200A/2P -> CSR2200 (matches Munoz). Hard gate 2=2 PW3.
Electrical: 2 DU222RB/4 B075/2 DC-4P(r18)/1 RSD(PE69-3020)/1 ground bar/2 PW3/1 Gateway3/1 meter(89).
No Coperniq record ID supplied this session -> master note not pulled (no expansion, so moot).

## METER SOCKET / BASE (Electrical rows 81-96) — user, Nelson REVA #860742
A meter line is ordered ONLY when the planset draws a NEW meter/socket/base (PV-1 scope line, e.g.
"UPGRADE METER BASE TO NEW MILBANK U9551-RXL-QG-5T9-AMS", and/or a NEW meter on the PV-5 one-line).
EXISTING meter -> NO meter line. The SKU is matched by EXACT part number — NEVER inferred. The meter
table is hardcoded verbatim from BOM_TEMPLATE.xlsx Electrical BOM col B rows 81-96 in
electrical_engine._METER_SKU_ROW so the engine resolves the row HEADLESS (no Excel read at runtime):
  81 U8949-RL-TG-KK-CECHA (ComEd base) · 82 U8436-O-CECHA (ComEd meter) · 83 NU8980-O-200-KK-CECHA
  (ComEd pedestal) · 84 U5168-XTL-100-KK-CECHA (ComEd M/M 100A) · 85 U5168-XTL-200-KK-CECHA (200A) ·
  87 U1773-XL-TG-KK (WE socket 200A) · 89 U9551-RXL-QG-5T9-AMS (Ameren OH lever bypass) <- Nelson ·
  90 S40405-02QG (Siemens sub for U9551) · 91 U6281-XL-100-5T6-AMS · 92 U6281-XL-100-5T9 ·
  93 U6281-XL-200-5T6-AMS · 94 U6281-XL-200-5T9 · 96 Special Order Meter (unmapped P/N).
  meter_socket(new_meter_drawn, meter_pn) -> ({row:1}, flags). Match (case/space-normalized, hyphens
  kept) -> that row. Unmapped P/N -> row 96 + HARD flag (human supplies SKU; NOT substituted with a
  close match). new_meter_drawn True but no P/N -> HARD flag (read it; never guess). Nelson:
  new_meter_drawn=True, "U9551-RXL-QG-5T9-AMS" -> row 89 qty 1.

## MASTER NOTE IS MANDATORY + HARDCODED (user, Nelson REVA)
The Coperniq "Master Note" form MUST be checked on EVERY project by the engine itself — never left to
a human/Claude step. electrical_engine.resolve_master_note(project_id, list_project_forms, get_form)
hardcodes the fetch: list_project_forms(project_id) -> find form whose name=="master note"
(case-insensitive, trimmed) -> get_form(id) -> master_notes_from_coperniq(form=...). The two MCP
tools are injected by the runner so it stays headless. tesla_expansion_resolved(unit_count, harness_pn,
project_id, list_project_forms, get_form, plan_mount=None) is the pipeline entry point for expansion:
it ALWAYS calls resolve_master_note then resolve_expansion_mount (precedence plans -> master note ->
default WALL), so the mount kit (row 61 stack / 62 wall) can never be chosen without consulting the
note. A missing/failed Master Note form falls back to default WALL but emits a HARD report flag
(master_note_form_missing / _fetch_failed / _list_failed) — it is recorded, never silently assumed.

## GENERAL PRINCIPLE (user, Nelson REVA): NO RUNTIME INFERENCE
Every resolution the engine performs outside Claude must be hardcoded (lookup tables, explicit
precedence, exact matches) — the engine may not "guess." Where data is genuinely ambiguous or missing,
the engine emits a HARD flag and holds (NeedsHumanExtraction), it does not invent a value.

## GATEWAY CSR MAIN — MASTER-NOTE CROSS-CHECK (user, Nelson REVA #860742)
A CSR main breaker landing INTO the gateway is INDEPENDENT of the per-PW3 60A/2P bus-kit breakers —
they coexist (Nelson: 2x 60A/2P BR260 r106 + 1x 200A CSR2200 r114, all in the TEG). The CSR is NOT
counted toward the per-PW3 reconciliation gate.

WHY IT WAS MISSED ORIGINALLY: the 200A/2P line entering the gateway was misclassified as the EXCLUDED
existing-MSP house main ("(E) MAIN BREAKER TO HOUSE 240V 200A/2P"), so csr_breakers came in EMPTY.
The only gate was the 60A/2P-vs-PW3 count, which PASSED (2==2) and thus MASKED the dropped main. There
was no check that the gateway's service main was actually present. On a whole-home-backup topology the
200A is a CSR main (200A MSP -> backed-up lugs in TEG; TEG -> load-side lugs in new meter), NOT the
excluded existing main.

STRUCTURAL FIX: tesla_gateway_breakers(buskit_breakers, csr_breakers, battery_pw3_count,
master_note_csr=None). The Master Note almost always states whether a CSR is in the TEG and its size
(user). It is used as a CROSS-CHECK ONLY (plans remain authoritative for the delivered breaker; the
note never adds one). RECONCILIATION GATE 2: if the note names a CSR amperage absent from the
plan-classified csr_breakers (or the size differs) -> HARD flag csr_plan_vs_note_mismatch -> HOLD &
verify PV-5. This catches a dropped/misclassified CSR main every time. Dare (no CSR, note silent) is
unaffected. Nelson delivered: 2x BR260 (r106) + 1x CSR2200 (r114).

NOTE on the broader plan-vs-note rule: for equipment VALUES the plan states discretely (harness P/N,
meter SKU, counts) the plan wins and a conflict is a NOTE (electrical_engine.plan_vs_note). The CSR is
a special case: the note disagreeing means a main may be LOST, which is high-stakes, so its cross-check
is a HARD hold rather than a soft NOTE. Both share the principle: plans authoritative, note is a check.

## WAREHOUSE ZONE — header B2 (user, Nelson REVA #860742)
The warehouse zone fills header B2 (Solar BOM; Electrical mirrors via ='Solar BOM'!B2). It lives in
TWO Coperniq places that must agree: (1) project.custom["zone"] e.g. ["Zone 3"] (AUTHORITATIVE,
clean single value) and (2) the first line of project.description HTML e.g.
"<strong><u>Zone 3</u></strong>" (cross-check). electrical_engine.warehouse_zone(project) is
hardcoded: custom['zone'] wins; description parsed via regex zone\s*([0-9]+) as fallback/cross-check.
Conflict -> NOTE (deliver property value). Neither present -> HARD + blank B2 (no guess). Nelson:
both say Zone 3 -> B2 "Zone 3", no flag. Header B1 = planset name of record; B3 = address.

## ROW SEGMENTATION + ORIENTATION (user, Carmen Meyer #877571)
A "row" = a MAXIMAL CONTIGUOUS horizontal run of modules along a rail line. Interrupted/"retired"
rows no longer exist: a horizontal GAP between modules SPLITS into separate qualified rows. A top
tier of [6]-gap-[2]-gap-[1] = THREE rows (not one row of 9). A horizontal RAIL RUN is NOT a row —
each row carries TWO horizontal rail runs, so never infer row count by counting/dividing rail runs.
Orientation is judged PER MODULE from the rotated south-frame raster (portrait if cell h>w).
  IMPLEMENTATION: orientation_detector._segment_by_gaps() splits each detected band into contiguous
  runs (a cell whose width > 1.4x the median module width is a GAP, not a module). detect_rows_and_
  orientations() now emits one qualified row per run. Meyer roof = [6,2,1,11] PORTRAIT, 4 rows,
  az240. (My initial landscape/4-tier read was wrong; the colored site-plan crop showed portrait +
  the 6/2/1/11 split.)

SEGMENTATION VALIDATOR — END CLAMPS / 4 = ROW COUNT (Option B, user). end_cap = rows*4 EXACTLY
(2 ends x 2 rails per row). The planset prints end-clamp qty, so plan_end_clamps/4 is the
AUTHORITATIVE row count; the engine's segmented row count MUST equal it or resolve_racking() emits
HARD row_segmentation_mismatch. This is the reliable row anchor (attach is only +-2/row, too weak to
separate adjacent counts: 4row=+1 vs 2row=-3 both near tolerance; end clamps give 16 vs 8, obvious).
attach remains the ORIENTATION validator (all-landscape misread on Meyer = +21, screams).

NO SPLICE FORMULA (user, Meyer). f_splice() returns None — splice is planset-truth only. The prior
legacy formula emitted a phantom computed value (e.g. 10) that looked like a real cross-check; that
is removed. resolve_racking crosscheck reports splice computed=None.

RAILS = planset-truth, NOTE-only cross-check (user, Meyer). rails delivered from planset ALWAYS;
f_rails still computes, but a mismatch is a NOTE (rails_formula_discrepancy), never HARD. Per-row
rail rounding runs high on segmented layouts (each short run forced to a 2-rail minimum) — benign.

## SPECIAL-ORDER LINES CARRY THE ACTUAL P/N (user, Meyer #877571)
METER (rows 81-96): unmapped meter P/N -> Special Order row 96, AND the verbatim P/N is stamped into
the line ("Special Order Meter: <pn>"). meter_socket() now returns (rows, flags, special_order),
where special_order={row: text} tells the writer to overwrite that row's SKU/description cell.
Meyer: U3358-O-KK not in table -> row 96 "Special Order Meter: U3358-O-KK" + HARD flag.

MSP (rows 73-79): main_service_panel(new_msp_drawn, msp_pn) — exact match against _MSP_SKU_ROW
(73 HOM48L125GRB LC, 74 HOM2040M100PC, 75 HOM3060M100PC, 76 HOM3060M200PC, 77 HOM4080M200PC,
78 HOM816M200PFTRB). Unmapped -> Special Order MSP row 79 with P/N stamped ("Special Order MSP:
<pn>") + HARD flag. NEW MSP only when planset specifies one; existing "can remain" -> no line.
Returns (rows, flags, special_order). Meyer: existing 100A Sylvania remains -> NO MSP line.

NOTE: meter_socket() signature changed to a 3-tuple (rows, flags, special_order). Any caller must
unpack three values and merge special_order into the writer's cell-override map.

## CANONICAL SHEET WRITER — bom_writer.py (user, Carmen Meyer #877571)
Sheet-writing is NO LONGER done in per-project ad-hoc scripts. ALL BOM writing goes through the
hardcoded bom_writer.py so every headless run fills BOM_TEMPLATE.xlsx identically. It owns:
  - write_bom(template, out, customer_name, warehouse_zone, customer_address, solar_rows,
    electrical_rows, solar_special_order, electrical_special_order, apply_filter=True)
  - header B1/B2/B3 on Solar BOM only (Electrical mirrors via the template's ='Solar BOM'!Bn refs)
  - STATIC quantities only (never formula cells — Protected View blanks uncached formulas)
  - special-order P/N stamping: special_order={row: text} writes the verbatim part into the line's
    DESCRIPTION cell (col C), leaving the template's "Special Order …:" label in the SKU cell.
    Fed by the 3rd return of meter_socket() (row 96) and main_service_panel() (row 79).
  - blank-row AutoFilter via filter_blank_rows.apply_qty_filter (A:D)
  - output_filename(first,last) -> BOM_First_Last.xlsx; merge_block()/merge_special() accumulators
STANDING RULE: any future change to how the sheet is filled (new special-order line, header field,
formatting) is made IN bom_writer.py, never in a one-off script, so it applies to all projects.
Verified on Meyer: row 96 qty 1, description "Special Order Meter: U3358-O-KK", 0 formula errors.

## SOLAR MODULE SKU + CONFIRMED SUBSTITUTION (user, Carmen Meyer #877571)
The planset module model (PV-1/PV-3/PV-5/PV-7) is matched by EXACT SKU against Solar rows 5-9
(5 ELNSM54M-HC-N-450 Sirius, 6 Q.PEAK DUO 410 QCELL, 7 HiS-T440NF(BK) Hyundai, 8 DNA-120-BF10-460W
Aptos, 9 LR5-54HPB-415M LonGi). electrical_engine.solar_module(module_pn, module_count):
  - exact match -> {row: count}
  - CONFIRMED substitution (_MODULE_CONFIRMED_SUB, user-authorized) -> substitute row + NOTE
  - unmapped, no confirmed sub -> HARD hold (human adds SKU/row; NEVER a blind close-wattage guess)
ALWAYS flags when the plan module is not a table SKU (never silent). Confirmed sub for Meyer:
HiS-T430NF(BK) 430W -> row 7 HiS-T440NF(BK) (440W version of same module), user-confirmed; emits a
NOTE recording the swap. Add future authorized swaps to _MODULE_CONFIRMED_SUB.

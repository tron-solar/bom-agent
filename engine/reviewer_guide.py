"""Reviewer guidance for confidence-report flags — Word-doc ONLY.

Maps a confidence flag `item` to (bom_location, what_to_check) so the Word renderer can add a
"What to check" column to the flags table. This guidance lives in the Word doc ONLY — it is NEVER
written into the confidence JSON (the JSON stays the machine-readable source of truth).

Structural, not per-project: the renderer looks up guidance by the flag's `item` (or item prefix);
it never special-cases a project. Any item with no mapping returns the visible FALLBACK marker so a
missing mapping is obvious in the doc — never blank, never silently dropped.
"""
from __future__ import annotations

FALLBACK_LOCATION = "—"
FALLBACK_CHECK = "⚠ Reviewer guidance not yet defined for this flag"

# The racking family — the orientation cross-check NOTE plus every advisory `racking_xcheck:*`
# sub-flag — collapses to ONE reviewer line (the engine still emits the granular flags; this is a
# DOC-ONLY collapse so the racking guidance shows once, not once per sub-flag).
_RACKING_GUIDANCE = (
    "Solar BOM — racking rows",
    "Confirm the racking quantities (attachments, clamps, rails, end caps) match the planset "
    "layout and BOM table.",
)

# Exact flag item -> (bom_location, what_to_check).
GUIDE: dict[str, tuple[str, str]] = {
    "jbox_count": (
        "Solar BOM — row 25 (JB-1.2) or row 26 (JB-3)",
        "Confirm J-box count and type match the roof type and string count."),
    "extractor_missing:micro_accessories": (
        "Electrical BOM — rows 11–18",
        "Not auto-filled. If the planset uses microinverters, manually add the micro SKU and "
        "branch-circuit count."),
    "extractor_missing:homeline_msp_breaker": (
        "Electrical BOM — row 128",
        "Not auto-filled. Confirm or add the Homeline MSP interconnection breaker."),
    "buskit_text_vs_vision": (
        "Electrical BOM",
        "Confirm all necessary breakers are added."),
    "csr_text_vs_vision": (
        "Electrical BOM",
        "Confirm the CSR/main breaker(s) are added with the correct rating and poles."),
    "harness_pn_source": (
        "Solar BOM — expansion harness row (63/64/65)",
        "Confirm the expansion harness P/N matches the planset."),
    "mci_count": (
        "Solar BOM — row 20",
        "Confirm MCI-2 count matches the PV-3 BOM table."),
}


def is_racking_family(item: str) -> bool:
    """The flags that collapse to the single racking reviewer line."""
    item = item or ""
    return item == "racking_orientation_crosscheck" or item.startswith("racking_xcheck:")


def guidance_for(item: str) -> tuple[str, str]:
    """(bom_location, what_to_check) for a flag item. Racking family -> the one collapsed racking
    line; mapped item -> its entry; anything else -> the visible fallback marker (never blank)."""
    if is_racking_family(item):
        return _RACKING_GUIDANCE
    return GUIDE.get(item or "", (FALLBACK_LOCATION, FALLBACK_CHECK))


def reviewer_rows(flags) -> list[tuple[str, str, str, str, str]]:
    """Renderer helper: turn the ordered confidence flags into flags-table rows
    (level, item, message, bom_location, what_to_check).

    The racking family is COLLAPSED to a single row — shown once at the position of the first racking
    flag; later racking flags are de-duped out so the same racking guidance row never repeats. Every
    non-racking flag maps 1:1 and gets its guidance (or the visible fallback marker) in the last two
    columns."""
    rows: list[tuple[str, str, str, str, str]] = []
    racking_done = False
    for fl in flags or []:
        item = fl.get("item", "") or ""
        level = fl.get("level", "") or ""
        message = fl.get("msg") or fl.get("detail") or ""
        if is_racking_family(item):
            if racking_done:
                continue                              # de-dupe: racking guidance shown once
            racking_done = True
            loc, chk = _RACKING_GUIDANCE
            rows.append((level, "racking", message, loc, chk))
        else:
            loc, chk = guidance_for(item)
            rows.append((level, item, message, loc, chk))
    return rows

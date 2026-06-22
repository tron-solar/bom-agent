# Engine changes — Nelson #860742 & Meyer #877571 session

## racking_engine.py
- racking_crosscheck(attachment_type=): ProteaBracket 45" attach/combo deltas -> NOTE (not HARD); end_cap stays HARD; other attachment types unchanged.
- tesla_gateway_breakers(master_note_csr=): Master Note CSR mention is a cross-check; catches a dropped/misclassified CSR main (HARD csr_plan_vs_note_mismatch). CSR independent of PW3 gate.
- f_splice() -> None (no splice formula; planset-truth only; phantom value removed).
- resolve_racking: rails demoted to NOTE (rails_formula_discrepancy), planset-truth always delivered.
- resolve_racking: END-CLAMP/4 segmentation validator (Option B) — segmented row count must equal plan_end_clamps/4 or HARD row_segmentation_mismatch. attach is the orientation validator (tolerance-aware signal).

## electrical_engine.py
- warehouse_zone(project): header B2 from custom['zone'] (truth) / description (cross-check+fallback); HARD if missing.
- meter_socket(): returns (rows, flags, special_order); unmapped meter -> row 96 with verbatim P/N stamped + HARD.
- main_service_panel(): MSP rows 73-79; exact match; unmapped -> row 79 with P/N stamped + HARD; existing-remains -> no line.
- solar_module(): module rows 5-9 exact match; confirmed substitution (430->440 Hyundai, user) -> NOTE; unmapped -> HARD. Always flags non-table module.
- resolve_master_note() / tesla_expansion_resolved(): mandatory hardcoded Master Note fetch; plans authoritative, note is a check (plan_vs_note NOTE on conflict).

## orientation_detector.py
- _segment_by_gaps(): a band splits into separate qualified rows wherever modules are not contiguous (gap > 1.4x median cell). Row != rail run.

## bom_writer.py (NEW)
- Canonical hardcoded sheet writer: header, static qtys, special-order P/N stamping, blank-row filter, BOM_First_Last.xlsx naming. ALL sheet-writing goes through here so changes persist on every headless run.

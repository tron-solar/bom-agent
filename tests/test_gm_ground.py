"""K2 ground-mount rows 73-89: STRICT VERBATIM table read (no computed values) + part-number-first
normalization. Ritchason #857524 Table 1 (the flattened PV-3.1 image Vision reads correctly):
rail 4001370=26, end cap 4001221=52, wire clip 4000069=104, two 4000135 cross-clamp rows (78+52),
cross cap 4000312=52 (excluded). Regression guards: rail must be 26 (not 26+52=78 from the old
'CR80'-in-description collision), end cap 52 (not rails*2=156), wire clip 104 (not module_count*2)."""
from engine.extractor import PlansetExtractor
from engine.racking_engine import k2_ground_mount

# Verbatim Vision read of Ritchason's PV-3.1 "Table 1 Bill of materials".
RITCHASON_TABLE1 = [
    {"part_number": "4001370", "description": 'SPD CrossRail 80, 216" Rail, Mill', "quantity": 26},
    {"part_number": "4000175", "description": 'K2 3" Pipe - Bracket Kit', "quantity": 52},
    {"part_number": "4000198", "description": "GM Residential Top Cap 3, Kit", "quantity": 12},
    {"part_number": "4000135", "description": "K2 Cross Clamp Set, Mill as Mid Clamp", "quantity": 78},
    {"part_number": "4000135", "description": "K2 Cross Clamp Set, Mill as End Clamp", "quantity": 52},
    {"part_number": "4000006-H", "description": "K2 Ground Lug, 13mm Hex Set", "quantity": 13},
    {"part_number": "4000312", "description": "K2 Cross Cap", "quantity": 52},
    {"part_number": "4000069", "description": "Wire Management Clip, TC", "quantity": 104},
    {"part_number": "4001221", "description": "EndCap, Black, CR80", "quantity": 52},
    {"part_number": None, "description": 'Pipe Coupling 3" (Third Party)', "quantity": 14},
    {"part_number": None, "description": "Pipe - Length 10 ft (ThirdParty)", "quantity": 21},
    {"part_number": None, "description": "Ground Screw", "quantity": 12},
]


def _ext():
    return PlansetExtractor.__new__(PlansetExtractor)   # no __init__ (needs no API key for parsing)


def test_gm_table_normalizes_by_part_number_no_collision():
    bom, unresolved = _ext()._build_gm_table_from_rows(RITCHASON_TABLE1)
    assert unresolved == []
    assert bom["rail_216"] == 26        # FIX 2: rail reads its OWN row (was 26+52=78, end-cap misrouted)
    assert bom["end_cap"] == 52         # end cap now keyed correctly (not swallowed by the CR80 rail check)
    assert bom["wire_clip"] == 104
    assert bom["combo_clamp"] == 130    # two 4000135 cross-clamp rows summed (78 + 52)


def test_gm_rows_are_strict_table_read():
    bom, _ = _ext()._build_gm_table_from_rows(RITCHASON_TABLE1)
    rows, overrides, flags = k2_ground_mount(bom, has_enphase_micros=False, module_count=52)
    assert rows == {
        73: 26,    # rail 4001370 (FIX 2: 26, not 78)
        77: 52,    # pipe bracket
        76: 12,    # top cap
        80: 130,   # combo (mid+end)
        82: 13,    # ground lug
        83: 104,   # wire clip — TABLE-READ (FIX 1: not module_count*2)
        78: 52,    # end cap  — TABLE-READ (FIX 1: not rail_qty*2 = 156)
        84: 14,    # pipe coupling (new SKU)
        87: 21,    # 10ft pipe -> rear N/S
        85: 12,    # ground screw
    }
    assert overrides.get(84) == ('Pipe Coupling 3" (Third Party)', 'Pipe Coupling 3" (Third Party)')
    assert not any(f.get("item") == "gm_line_unmapped" for f in flags)
    # 4000312 K2 Cross Cap excluded entirely (no row for it)
    assert 79 not in rows and all(r in {73, 76, 77, 78, 80, 82, 83, 84, 85, 87} for r in rows)


def test_end_cap_description_not_mistaken_for_rail():
    # the exact collision: "EndCap, Black, CR80" must key to end_cap, NOT rail_216
    assert PlansetExtractor._normalize_gm_key("4001221", "EndCap, Black, CR80") == "end_cap"
    assert PlansetExtractor._normalize_gm_key("4001370", 'SPD CrossRail 80, 216" Rail') == "rail_216"
    # even with a blank part number, the end-cap description resolves to end_cap (not rail)
    assert PlansetExtractor._normalize_gm_key(None, "EndCap, Black, CR80") == "end_cap"

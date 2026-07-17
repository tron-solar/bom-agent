"""Disconnect counting: the equipment-schedule text CLARIFIES the count when present (fixing the
PV-5 one-line Vision over-count), and is a no-op when absent. Ritchason #857524 (single array, 2 PW3)
must yield AC non-fused = 2 (DU222RB row 6, not 4) and DC = 2 four-pole + 2 two-pole (SI32 row 18 = 2,
not 3; EN-EP200G row 17 = 2)."""
from engine.extractor import PlansetExtractor
from engine.electrical_engine import ac_disconnects, dc_disconnects

# Verbatim excerpt of Ritchason's PV-5.1 EQUIPMENT SCHEDULE (single ground array, 2 PW3).
RITCHASON_SCHEDULE = """EQUIPMENT
QTY
DESCRIPTION
RATINGS
SOLAR PV MODULES
52
SIRIUS SOLAR ELNSM54M-HC-N 450W
450W
AC DISCONNECT
02
PHOTOVOLTAIC/ENERGY STORAGE SYSTEM
NON-FUSED AC DISCONNECT, 240V, 2-POLE
60A
DC DISCONNECT
02
DC DISCONNECT, 240V, 4-POLE
600V
DC DISCONNECT
02
DC DISCONNECT, 240V, 2-POLE
600V
EXPANSION UNIT
01
01 TESLA POWERWALL 3 EXPANSION UNIT
"""


def test_ritchason_schedule_parses_counts_and_types():
    ac, dc = PlansetExtractor._parse_disconnect_schedule(RITCHASON_SCHEDULE)
    assert ac == [{"amp": 60, "fused": False, "fuse_amp": None},
                  {"amp": 60, "fused": False, "fuse_amp": None}]          # 2 AC, 60A, non-fused
    assert sorted(d["poles"] for d in dc) == [2, 2, 4, 4]                 # 2 four-pole + 2 two-pole


def test_ritchason_engine_rows_are_2_not_4_and_3():
    ac, dc = PlansetExtractor._parse_disconnect_schedule(RITCHASON_SCHEDULE)
    ac_rows, _ = ac_disconnects(ac)
    dc_rows, _ = dc_disconnects(dc)
    assert ac_rows[6] == 2          # DU222RB 60A non-fused: 2, NOT 4
    assert dc_rows[18] == 2         # SI32-PEL64R-4 (4-pole): 2, NOT 3
    assert dc_rows[17] == 2         # EN-EP200G (2-pole): 2, NOT 0


def test_absent_schedule_is_a_no_op():
    # no EQUIPMENT-column disconnect rows -> empty lists so the caller keeps the Vision read
    ac, dc = PlansetExtractor._parse_disconnect_schedule("SOME ROOF PLAN\nMODULES\n52\n")
    assert ac == [] and dc == []


def test_prose_mention_is_not_counted():
    # a code note that merely mentions "AC DISCONNECT" mid-sentence must NOT be parsed as a device
    prose = ("HEIGHT OF THE AC DISCONNECT SHALL NOT EXCEED 6'-7\".\n"
             "DISCONNECTING MEANS SHALL BE LOCATED IN A VISIBLE LOCATION.\n")
    ac, dc = PlansetExtractor._parse_disconnect_schedule(prose)
    assert ac == [] and dc == []


def test_fused_schedule_row_marks_fused():
    sched = "AC DISCONNECT\n01\n60A FUSED AC DISCONNECT, 240V, 2-POLE\n60A\n"
    ac, _ = PlansetExtractor._parse_disconnect_schedule(sched)
    assert ac == [{"amp": 60, "fused": True, "fuse_amp": None}]           # 'FUSED' (no 'NON-') -> fused


# ---- reconciliation: schedule authoritative when present, Vision corroboration-only ----
def test_reconcile_schedule_overrides_vision_with_note_not_hard():
    sac, sdc = PlansetExtractor._parse_disconnect_schedule(RITCHASON_SCHEDULE)
    vac = [{"amp": 60, "fused": False, "fuse_amp": None}] * 4             # Vision over-count (4)
    vdc = [{"poles": 4}] * 3                                              # Vision: 3 four-pole, missed 2-pole
    ac, dc, flags = PlansetExtractor._reconcile_disconnects(vac, vdc, sac, sdc, 7)
    assert ac == sac and dc == sdc                                       # schedule wins
    assert not any(f["level"] == "HARD" for f in flags)                  # corroboration only, never HARD
    items = {f["item"] for f in flags}
    assert items == {"ac_disconnect_count_clarified", "dc_disconnect_count_clarified"}
    ac_rows, _ = ac_disconnects(ac)
    dc_rows, _ = dc_disconnects(dc)
    assert ac_rows[6] == 2 and dc_rows[18] == 2 and dc_rows[17] == 2     # 2/2/2, NOT 4/3/0


def test_reconcile_no_schedule_falls_back_to_vision_with_note():
    vac = [{"amp": 60, "fused": False, "fuse_amp": None}] * 2
    vdc = [{"poles": 4}, {"poles": 2}]
    ac, dc, flags = PlansetExtractor._reconcile_disconnects(vac, vdc, [], [], None)
    assert ac == vac and dc == vdc                                       # Vision stands
    assert not any(f["level"] == "HARD" for f in flags)
    items = {f["item"] for f in flags}
    assert items == {"ac_disconnect_count_from_oneline", "dc_disconnect_count_from_oneline"}

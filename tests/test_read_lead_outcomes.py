"""
test_read_lead_outcomes.py — the CRM-outcome -> disposition mapping contract (Job 8).
No DB: pure mapping + domain invariants for BOTH writer paths (legacy .outcome funnel
and the Caller Desk precise meta.disposition).
"""
from scripts.read_lead_outcomes import OUTCOME_MAP, CALLER_DESK_MAP, resolve_disposition

# must match migrations/018_lead_outcomes.sql CHECK constraint
DISPOSITION_DOMAIN = {"interested", "callback", "wrong_number", "dnc", "dead"}


def test_known_outcomes_map_as_specified():
    assert OUTCOME_MAP["connected"] == "interested"
    assert OUTCOME_MAP["voicemail"] == "callback"
    assert OUTCOME_MAP["bad_number"] == "wrong_number"
    assert OUTCOME_MAP["do_not_call"] == "dnc"
    assert OUTCOME_MAP["deceased"] == "dead"


def test_every_mapped_value_is_in_the_migration_domain():
    assert set(OUTCOME_MAP.values()) <= DISPOSITION_DOMAIN
    assert set(v for v in CALLER_DESK_MAP.values() if v is not None) <= DISPOSITION_DOMAIN


def test_unmapped_outcome_is_honest_null_not_coerced():
    assert OUTCOME_MAP.get("some_novel_outcome") is None
    assert OUTCOME_MAP.get("") is None


# ---- Caller Desk (precise meta.disposition) contract -----------------------------

def test_caller_desk_map_pins_all_four_rulings():
    # the four the operator pinned, verbatim
    assert CALLER_DESK_MAP["interested_promote"] == "interested"
    assert CALLER_DESK_MAP["not_interested"] == "dead"      # decline -> down, never a raise
    assert CALLER_DESK_MAP["dnc_request"] == "dnc"          # TCPA suppression captured
    assert CALLER_DESK_MAP["no_answer"] is None             # not reached -> neutral, no fact
    # plus the two soft-contact keys
    assert CALLER_DESK_MAP["contacted"] == "callback"
    assert CALLER_DESK_MAP["callback"] == "callback"


def test_caller_desk_dnc_request_lands_as_dnc():
    # a Caller Desk DNC: the funnel puts 'dnc_request' in .outcome and meta carries it;
    # resolve_disposition -> 'dnc' is the exact value read_lead_outcomes upserts into
    # landos.lead_outcomes.disposition (so a DNC is NEVER dropped again).
    meta = {"source": "caller_desk", "disposition": "dnc_request"}
    assert resolve_disposition("dnc_request", meta) == "dnc"


def test_caller_desk_precise_disposition_beats_the_lossy_funnel():
    # the inversion guard: mig-054 collapses not_interested -> outcome='connected'.
    # Reading .outcome would give 'interested' (a RAISE); we must read meta -> 'dead'.
    meta = {"source": "caller_desk", "disposition": "not_interested"}
    assert OUTCOME_MAP["connected"] == "interested"          # what the lossy path would say
    assert resolve_disposition("connected", meta) == "dead"  # what we actually record


def test_legacy_rows_still_use_the_outcome_funnel():
    # a non-caller-desk row (no meta / other source) falls back to the .outcome vocabulary
    assert resolve_disposition("connected", None) == "interested"
    assert resolve_disposition("bad_number", {"source": "other"}) == "wrong_number"
    assert resolve_disposition("no_answer", None) is None    # legacy no_answer unmapped -> skip

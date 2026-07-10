"""
test_read_lead_outcomes.py — the CRM-outcome -> disposition mapping contract (Job 8).
No DB: pure mapping + domain invariants.
"""
from scripts.read_lead_outcomes import OUTCOME_MAP

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


def test_unmapped_outcome_is_honest_null_not_coerced():
    assert OUTCOME_MAP.get("some_novel_outcome") is None
    assert OUTCOME_MAP.get("") is None

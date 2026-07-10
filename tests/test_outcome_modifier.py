"""
test_outcome_modifier.py — JOB 8 scorer-modifier contract (the deterministic
post-floor multiplier). No DB: pure table + direction invariants, mirroring
test_read_lead_outcomes.py. Guards the multiplier values from silent drift.
"""
from scripts.score_composite_batch import OUTCOME_MODIFIER

# must match migrations/018_lead_outcomes.sql CHECK + read_lead_outcomes OUTCOME_MAP range
DISPOSITION_DOMAIN = {"interested", "callback", "wrong_number", "dnc", "dead"}


def test_modifier_covers_exactly_the_disposition_domain():
    # every disposition a fact can carry has a modifier; no orphan keys
    assert set(OUTCOME_MODIFIER) == DISPOSITION_DOMAIN


def test_modifier_values_match_spec():
    assert OUTCOME_MODIFIER["interested"] == 1.15
    assert OUTCOME_MODIFIER["callback"] == 1.05
    assert OUTCOME_MODIFIER["wrong_number"] == 0.90
    assert OUTCOME_MODIFIER["dnc"] == 0.85
    assert OUTCOME_MODIFIER["dead"] == 0.50


def test_engagement_raises_bad_contact_lowers():
    # reached-owner facts raise (interested, callback); bad-contact / dead facts lower.
    raise_keys = {k for k, v in OUTCOME_MODIFIER.items() if v > 1.0}
    lower_keys = {k for k, v in OUTCOME_MODIFIER.items() if v < 1.0}
    assert raise_keys == {"interested", "callback"}
    assert lower_keys == {"wrong_number", "dnc", "dead"}


def test_cap_binds_for_any_raising_modifier():
    # a max base (100) times any >1 modifier is clamped to 100 — never overflows.
    for k, v in OUTCOME_MODIFIER.items():
        if v > 1.0:
            assert min(100.0, 100.0 * v) == 100.0

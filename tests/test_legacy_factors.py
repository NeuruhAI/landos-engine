"""
test_legacy_factors.py — P-28: the four legacy ranking factors (pure, injected clock).
"""
from datetime import date

import pytest

from app.legacy_factors import (LEGACY_DEFAULT_WEIGHTS, LEGACY_FACTORS,
                                absentee_factor, legacy_rank, opportunity_factor,
                                out_of_state_factor, receipt_canonical,
                                tenure_factor, validate_weights)

AS_OF = date(2026, 7, 21)   # injected clock — never the wall clock


# ---- individual factors ----------------------------------------------------

def test_absentee_factor_mirrors_the_tier_base():
    # one source of truth: TIER_BASE/100, so the legacy factor can never drift
    assert absentee_factor("out_of_state")["value"] == 0.9
    assert absentee_factor("out_of_county")["value"] == 0.7
    assert absentee_factor("out_of_town")["value"] == 0.5
    assert absentee_factor("owner_occupied")["value"] == 0.2
    assert absentee_factor(None)["value"] == 0.4          # unknown, honest middle
    assert absentee_factor("nonsense")["value"] == 0.4


def test_out_of_state_is_the_binary_legacy_flag():
    assert out_of_state_factor("out_of_state")["value"] == 1.0
    for tier in ("out_of_county", "out_of_town", "owner_occupied", None):
        assert out_of_state_factor(tier)["value"] == 0.0


def test_opportunity_factor_is_oz_membership():
    hit = opportunity_factor({"name": "Tract 26021-9701", "provenance_status": "verified"})
    assert hit["value"] == 1.0 and "oz_tract=Tract 26021-9701" in hit["evidence"]
    assert opportunity_factor(None) == {"value": 0.0, "evidence": []}


def test_tenure_ramp():
    assert tenure_factor("2023-01-15", AS_OF)["value"] == 0.0        # < 5y floor
    assert tenure_factor("2001-07-21", AS_OF)["value"] == 1.0        # 25y ceiling
    mid = tenure_factor("2011-07-21", AS_OF)["value"]                # 15y -> midpoint
    assert mid == pytest.approx(0.5, abs=0.01)
    assert "held 15.0y since 2011-07-21" in tenure_factor("2011-07-21", AS_OF)["evidence"][0]


def test_tenure_honest_on_missing_or_future_dates():
    assert tenure_factor(None, AS_OF) == \
        {"value": 0.0, "evidence": ["tenure unknown (no transfer date)"]}
    assert tenure_factor("not-a-date", AS_OF)["value"] == 0.0
    assert tenure_factor("2027-01-01", AS_OF)["value"] == 0.0        # future = not ranked
    assert tenure_factor(date(1990, 3, 1), AS_OF)["value"] == 1.0    # date objects too


def test_tenure_clock_is_injected():
    # same lead, different as_of -> different tenure; nothing reads the wall clock
    early = tenure_factor("2010-01-01", date(2016, 1, 1))["value"]
    late = tenure_factor("2010-01-01", date(2036, 1, 1))["value"]
    assert early < late == 1.0


# ---- the composite ranking -------------------------------------------------

def _lead(tier=None, acquired=None):
    return {"absentee_tier": tier, "acquired_date": acquired}


def test_full_legacy_stack_scores_100():
    r = legacy_rank(_lead("out_of_state", "1995-06-01"), AS_OF,
                    oz_hit={"name": "T-1"},
                    weights={"absentee": 0.0})   # absentee tops out at 0.9 — exclude
    assert r["score"] == 100.0


def test_empty_lead_scores_only_the_unknown_absentee_middle():
    r = legacy_rank(_lead(), AS_OF)
    rows = {f["factor"]: f for f in r["factors"]}
    assert rows["out_of_state"]["value"] == 0.0
    assert rows["opportunity"]["value"] == 0.0
    assert rows["tenure"]["value"] == 0.0
    assert rows["absentee"]["value"] == 0.4
    assert r["score"] == pytest.approx(100 * 0.35 * 0.4, abs=0.01)


def test_weights_validation_idiom():
    assert validate_weights(None) == LEGACY_DEFAULT_WEIGHTS
    with pytest.raises(ValueError, match="unknown legacy factor"):
        validate_weights({"tenur": 0.5})
    with pytest.raises(ValueError, match="negative"):
        validate_weights({"tenure": -1})
    with pytest.raises(ValueError, match="all legacy weights are zero"):
        validate_weights({f: 0 for f in LEGACY_FACTORS})


def test_rank_is_deterministic_and_receipt_tamper_evident():
    a = legacy_rank(_lead("out_of_county", "2005-05-05"), AS_OF)
    b = legacy_rank(_lead("out_of_county", "2005-05-05"), AS_OF)
    assert receipt_canonical(a) == receipt_canonical(b)
    moved = legacy_rank(_lead("out_of_county", "2005-05-05"), date(2027, 7, 21))
    assert moved["receipt"]["chain_sha256"] != a["receipt"]["chain_sha256"]
    reweighted = legacy_rank(_lead("out_of_county", "2005-05-05"), AS_OF,
                             weights={"tenure": 0.9})
    assert reweighted["weights_sha256"] != a["weights_sha256"]


def test_factor_rows_reconcile_to_the_score():
    r = legacy_rank(_lead("out_of_state", "2008-01-01"), AS_OF, oz_hit={"name": "T"})
    assert r["score"] == pytest.approx(
        round(sum(f["points"] for f in r["factors"]), 2), abs=0.02)
    pts = [(-f["points"], f["factor"]) for f in r["factors"]]
    assert pts == sorted(pts)

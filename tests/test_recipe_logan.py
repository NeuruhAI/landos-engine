"""
test_recipe_logan.py — P-25: the Logan high-priority recipe (pure, fixture-only).

Each factor lane fires on its mapped evidence and stays silent otherwise; scoring
is configurable but typo-proof; the explanation receipt is deterministic and
tamper-evident; and the obituary lane (review-only, P-23) can never move a score.
"""
import pytest

from app.recipe_logan import (FACTORS, LOGAN_DEFAULT_WEIGHTS, receipt_canonical,
                              score_logan, validate_weights)


def _score(types=(), tier=None, weights=None):
    return score_logan({"absentee_tier": tier}, types, weights)


# ---- factor lanes ----------------------------------------------------------

def test_each_signal_lane_fires_alone():
    for types, factor in [({"tax_delinquent"}, "tax_delinquency_lien"),
                          ({"taxsale"}, "tax_delinquency_lien"),
                          ({"code_violation"}, "code_violation"),
                          ({"vacancy"}, "vacant_boarded"),
                          ({"boarded"}, "vacant_boarded"),
                          ({"condemned"}, "physical_distress"),
                          ({"field_pin"}, "physical_distress"),
                          ({"probate_in"}, "probate_title"),
                          ({"heirs_of_tangled_title"}, "probate_title")]:
        r = _score(types)
        fired = {f["factor"] for f in r["factors"] if f["points"] > 0}
        assert fired == {factor}, (types, fired)


def test_absentee_lane_is_graded_by_tier():
    assert _score(tier="out_of_state")["score"] > _score(tier="out_of_county")["score"]
    assert _score(tier="out_of_county")["score"] > _score(tier="out_of_town")["score"]
    assert _score(tier="owner_occupied")["score"] == 0.0
    assert _score(tier=None)["score"] == 0.0


def test_full_stack_scores_100():
    r = _score({"tax_delinquent", "code_violation", "vacancy", "condemned",
                "probate_in"}, tier="out_of_state")
    assert r["score"] == 100.0
    assert [f["factor"] for f in r["factors"] if f["points"] > 0]


def test_no_evidence_scores_zero_with_empty_evidence():
    r = _score()
    assert r["score"] == 0.0
    assert all(f["evidence"] == [] for f in r["factors"])


def test_unknown_signal_types_are_ignored_not_guessed():
    assert _score({"weird_signal", "layoff_notice"})["score"] == 0.0


def test_obituary_never_moves_the_score():
    # P-23 mint-safety extends to prioritization: the review-only lane is inert here
    assert _score({"obituary"})["score"] == 0.0
    with_ob = _score({"probate_in", "obituary"})
    without = _score({"probate_in"})
    assert with_ob["score"] == without["score"]


# ---- configurable scoring --------------------------------------------------

def test_default_weights_cover_exactly_the_six_factors():
    assert set(LOGAN_DEFAULT_WEIGHTS) == set(FACTORS)
    assert validate_weights(None) == LOGAN_DEFAULT_WEIGHTS


def test_weight_override_changes_the_score_deterministically():
    types = {"code_violation"}
    heavy = _score(types, weights={"code_violation": 0.85})
    light = _score(types, weights={"code_violation": 0.01})
    assert heavy["score"] > light["score"]


def test_unknown_factor_is_refused():
    with pytest.raises(ValueError, match="unknown Logan factor"):
        validate_weights({"code_violations": 0.5})   # typo'd name fails loudly


def test_negative_and_non_numeric_weights_are_refused():
    with pytest.raises(ValueError, match="negative"):
        validate_weights({"code_violation": -0.1})
    with pytest.raises(ValueError, match="not a number"):
        validate_weights({"code_violation": "high"})


def test_all_zero_weights_are_refused():
    with pytest.raises(ValueError, match="all Logan weights are zero"):
        validate_weights({f: 0.0 for f in FACTORS})


# ---- deterministic explanation receipt -------------------------------------

def test_receipt_is_byte_identical_across_runs_and_input_order():
    a = _score(["probate_in", "tax_delinquent", "vacancy"], tier="out_of_state")
    b = _score(["vacancy", "tax_delinquent", "probate_in"], tier="out_of_state")
    assert receipt_canonical(a) == receipt_canonical(b)
    assert a["receipt"]["chain_sha256"] == b["receipt"]["chain_sha256"]


def test_receipt_moves_when_evidence_or_weights_move():
    base = _score({"tax_delinquent"})
    assert _score({"tax_delinquent", "vacancy"})["receipt"]["chain_sha256"] \
        != base["receipt"]["chain_sha256"]
    reweighted = _score({"tax_delinquent"}, weights={"vacant_boarded": 0.30})
    assert reweighted["receipt"]["chain_sha256"] != base["receipt"]["chain_sha256"]
    assert reweighted["weights_sha256"] != base["weights_sha256"]


def test_factor_rows_explain_the_points():
    r = _score({"tax_delinquent", "code_violation"}, tier="out_of_town")
    rows = {f["factor"]: f for f in r["factors"]}
    assert rows["tax_delinquency_lien"]["evidence"] == ["tax_delinquent"]
    assert rows["code_violation"]["evidence"] == ["code_violation"]
    assert rows["absentee_out_of_state"]["evidence"] == ["absentee_tier=out_of_town"]
    # points and score reconcile
    assert r["score"] == pytest.approx(
        round(sum(f["points"] for f in r["factors"]), 2), abs=0.02)
    # ordering: points desc, then name
    pts = [(-f["points"], f["factor"]) for f in r["factors"]]
    assert pts == sorted(pts)

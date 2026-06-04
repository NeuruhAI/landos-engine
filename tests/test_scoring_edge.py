from app.scoring import score_edge, EDGE_DEFAULT_WEIGHTS, EDGE_PROFILE

SUBS = ("location_score", "future_potential", "dev_fit", "prep_lift_inverse")


def test_profile_name():
    assert EDGE_PROFILE == "benton_harbor_edge"


def test_weights_match_spec():
    base = EDGE_DEFAULT_WEIGHTS["base"]
    assert abs(sum(base.values()) - 1.0) < 1e-9
    assert base["location_score"] == 0.30
    assert base["future_potential"] == 0.25
    assert base["dev_fit"] == 0.25
    assert base["prep_lift_inverse"] == 0.20


def test_all_max_gives_100():
    r = score_edge({"base": {k: 1.0 for k in SUBS}}, EDGE_DEFAULT_WEIGHTS)
    assert r["base_score"] == 100.0


def test_location_only_weighting():
    r = score_edge({"base": {"location_score": 1.0}}, EDGE_DEFAULT_WEIGHTS)
    assert r["base_score"] == 30.0
    assert r["sub_scores"]["location_score"] == 1.0


def test_tax_delinquent_is_near_zero():
    base_in = {"base": {"future_potential": 1.0}}
    off = score_edge(base_in, EDGE_DEFAULT_WEIGHTS)["base_score"]
    on = score_edge({**base_in, "distress": {"tax_delinquent": True}}, EDGE_DEFAULT_WEIGHTS)["base_score"]
    # tax_delinquent weight 0.0001 -> max +0.01 pts on a 0..100 scale: near-zero.
    assert 0 <= (on - off) <= 0.02


def test_subscore_clamped():
    r = score_edge({"base": {"location_score": 5.0}}, EDGE_DEFAULT_WEIGHTS)
    assert r["base_score"] == 30.0

"""
test_composite_inputs.py — the assembler's HARD-RULE contract: MISSING is
first-class (NULL, low confidence), never a fabricated 0.
"""
from app.scoring import DEFAULT_WEIGHTS as W, score_parcel
from app.composite_inputs import assemble


def _lead(**kw):
    base = {"absentee_tier": None, "owner_type": None, "skip_traced": False,
            "is_deceased": None, "distress_tokens": [], "acreage": None,
            "market_ppa": None, "list_ppa": None, "comps_n": 0}
    base.update(kw)
    return base


# ---- distress: grounded / unmodeled / absent are three DISTINCT states --------
def test_distress_modeled_token_is_grounded():
    a = assemble(_lead(distress_tokens=["tax_delinquent"]), W)
    assert a["coverage"]["distress"]["status"] == "grounded"
    assert a["inputs"]["distress"] == {"tax_delinquent": True}
    assert a["null_components"]["distress"] is False  # component stored, not NULL


def test_foreclosure_maps_to_pre_foreclosure():
    a = assemble(_lead(distress_tokens=["foreclosure"]), W)
    assert a["inputs"]["distress"] == {"pre_foreclosure": True}
    assert a["coverage"]["distress"]["modeled_tokens"] == ["foreclosure"]


def test_environmental_and_brownfield_map_to_environmental_key():
    """Part 201 + brownfield both map to the single 'environmental' distress key
    (default v3, weight 0.15): grounded, component = 0.15*100 = 15, NOT NULL."""
    for tok in ("environmental_part201", "brownfield"):
        a = assemble(_lead(distress_tokens=[tok]), W)
        cov = a["coverage"]["distress"]
        assert cov["status"] == "grounded", tok
        assert a["inputs"]["distress"] == {"environmental": True}, tok
        assert a["null_components"]["distress"] is False, tok
        assert score_parcel(a["inputs"], W)["distress_component"] == 15.0, tok


def test_unmapped_distress_token_stays_unmodeled_not_zero():
    """A token with NO model slot must still NULL the component and record the
    signal — never assert 'no distress' by scoring 0."""
    a = assemble(_lead(distress_tokens=["zoning_dispute"]), W)
    cov = a["coverage"]["distress"]
    assert cov["status"] == "grounded_unmodeled"
    assert cov["present_tokens"] == ["zoning_dispute"]
    assert cov["modeled_tokens"] == []
    assert a["inputs"]["distress"] == {}          # nothing fabricated
    assert a["null_components"]["distress"] is True  # stored as NULL, NOT 0


def test_token_mapped_to_key_absent_from_weights_is_unmodeled():
    """Cross-check: if the active weights lack the mapped key, the token is
    unmodeled (NULL) — never silently grounded-but-0. environmental under a v2-style
    weights dict (no 'environmental' key) must be unmodeled."""
    v2like = {k: v for k, v in W.items()}
    v2like["distress"] = {k: v for k, v in W["distress"].items() if k != "environmental"}
    a = assemble(_lead(distress_tokens=["environmental_part201"]), v2like)
    assert a["coverage"]["distress"]["status"] == "grounded_unmodeled"
    assert a["null_components"]["distress"] is True


def test_no_distress_token_is_absent():
    a = assemble(_lead(), W)
    assert a["coverage"]["distress"]["status"] == "absent"
    assert a["null_components"]["distress"] is True


# ---- arbitrage: absent without comps, grounded with market+list --------------
def test_arbitrage_absent_without_comps():
    a = assemble(_lead(acreage=40), W)  # acreage alone is not enough
    assert a["coverage"]["arbitrage"]["status"] == "absent"
    assert a["null_components"]["arbitrage"] is True


def test_arbitrage_grounded_with_market_and_list():
    a = assemble(_lead(acreage=10, market_ppa=9000, list_ppa=3000, comps_n=4), W)
    assert a["coverage"]["arbitrage"]["status"] == "grounded"
    assert a["null_components"]["arbitrage"] is False
    assert score_parcel(a["inputs"], W)["arbitrage_component"] > 0


# ---- owner: always grounded (tier+type), age never, govt != entity -----------
def test_owner_grounded_but_age_unobserved():
    a = assemble(_lead(absentee_tier="out_of_state", owner_type="individual"), W)
    cov = a["coverage"]["owner"]
    assert cov["status"] == "grounded"
    assert a["inputs"]["owner"]["out_of_state"] is True
    assert "age_senior" in cov["unobserved_subsignals"]   # no age source, ever
    assert "age_band" not in a["inputs"]["owner"]         # not fabricated as 'other'
    assert a["null_components"]["owner"] is False


def test_govt_is_not_entity():
    a = assemble(_lead(owner_type="govt"), W)
    assert a["inputs"]["owner"]["is_entity"] is False


def test_unknown_tier_leaves_out_of_state_unobserved():
    a = assemble(_lead(absentee_tier="unknown_situs", owner_type="entity"), W)
    assert "out_of_state" not in a["inputs"]["owner"]
    assert "out_of_state" in a["coverage"]["owner"]["unobserved_subsignals"]


def test_no_succession_only_when_deceased_known():
    seen = assemble(_lead(skip_traced=True, is_deceased=True), W)
    assert seen["inputs"]["owner"]["no_succession"] is True
    unknown = assemble(_lead(skip_traced=False, is_deceased=None), W)
    assert "no_succession" not in unknown["inputs"]["owner"]


# ---- confidence rises as more axes become modeled-grounded -------------------
def test_confidence_monotonic_in_grounded_axes():
    owner_only = assemble(_lead(absentee_tier="out_of_town", owner_type="entity",
                                distress_tokens=["zoning_dispute"]), W)["confidence"]
    plus_distress = assemble(_lead(absentee_tier="out_of_town", owner_type="entity",
                                   distress_tokens=["tax_delinquent"]), W)["confidence"]
    assert plus_distress > owner_only

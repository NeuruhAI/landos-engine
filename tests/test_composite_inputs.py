"""
test_composite_inputs.py — the assembler's HARD-RULE contract: MISSING is
first-class (NULL, low confidence), never a fabricated 0.
"""
from datetime import date

from app.scoring import DEFAULT_WEIGHTS as W, score_parcel
from app.composite_inputs import assemble

AS_OF = date(2026, 6, 19)


def _lead(**kw):
    base = {"absentee_tier": None, "owner_type": None, "skip_traced": False,
            "is_deceased": None, "distress_tokens": [], "acreage": None,
            "comps": [], "as_of": AS_OF}
    base.update(kw)
    return base


def _comp(price, acreage, mi=1.0, sale="2025-06-01"):
    return {"sale_price": price, "acreage": acreage, "sale_date": sale, "distance_mi": mi}


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


def test_lis_pendens_maps_to_pre_foreclosure():
    a = assemble(_lead(distress_tokens=["lis_pendens"]), W)
    assert a["inputs"]["distress"] == {"pre_foreclosure": True}
    assert a["coverage"]["distress"]["modeled_tokens"] == ["lis_pendens"]


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


def test_requires_bea_badge_and_gross_of_remediation():
    """Part-201/brownfield -> requires_bea + contamination_kind, and the arbitrage axis
    is flagged GROSS-of-remediation so a comp spread is never read as a clean net."""
    p = assemble(_lead(distress_tokens=["environmental_part201"]), W)
    assert p["requires_bea"] is True and p["contamination_kind"] == "part201"
    assert p["coverage"]["arbitrage"]["gross_of_remediation"] is True
    assert p["coverage"]["arbitrage"]["remediation_note"]
    b = assemble(_lead(distress_tokens=["brownfield"]), W)
    assert b["requires_bea"] is True and b["contamination_kind"] == "brownfield"
    clean = assemble(_lead(distress_tokens=["tax_delinquent"]), W)
    assert clean["requires_bea"] is False and clean["contamination_kind"] is None
    assert clean["coverage"]["arbitrage"]["gross_of_remediation"] is False
    assert clean["coverage"]["arbitrage"]["remediation_note"] is None


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


# ---- arbitrage (basis A): comps-only feasibility; absent->NULL without comps ----
def test_arbitrage_absent_without_comps():
    a = assemble(_lead(acreage=40), W)  # as_of set, but no comp set
    assert a["coverage"]["arbitrage"]["status"] == "absent"
    assert a["null_components"]["arbitrage"] is True
    assert a["inputs"]["arbitrage"] == {}  # nothing fabricated


def test_arbitrage_grounded_from_comps_feasibility():
    comps = [_comp(80000, 10), _comp(90000, 11), _comp(75000, 9)]  # 3 comps -> 'normal'
    a = assemble(_lead(acreage=10, comps=comps), W)
    cov = a["coverage"]["arbitrage"]
    assert cov["status"] == "grounded"
    assert a["null_components"]["arbitrage"] is False
    assert "feasibility" in a["inputs"]["arbitrage"]
    assert cov["arv"] is not None and cov["comps_n"] == 3
    assert score_parcel(a["inputs"], W)["arbitrage_component"] > 0


def test_arbitrage_carry_absent_never_fabricated():
    comps = [_comp(80000, 10), _comp(90000, 11)]
    a = assemble(_lead(acreage=10, comps=comps), W)
    cov = a["coverage"]["arbitrage"]
    assert "carry_feasible_90d" in cov["missing_inputs"]      # flagged absent
    assert "carry_feasible_90d" not in a["inputs"]["arbitrage"]  # not fabricated as 0
    assert cov["comp_confidence"] == "low (<3 comps)"         # 2 comps -> low confidence


def test_arbitrage_subdivision_floored_absent_no_zoning_source():
    """subdivision is NOT derived from acreage (that would fabricate the missing
    zoning input) — it stays absent/floored like carry; arbitrage grounds on
    feasibility alone."""
    comps = [_comp(80000, 10), _comp(90000, 11)]
    a = assemble(_lead(acreage=40, comps=comps), W)
    arb, cov = a["inputs"]["arbitrage"], a["coverage"]["arbitrage"]
    assert "subdivisions_to_2x" not in arb               # not fabricated from acreage
    assert "subdivisions_to_2x" in cov["missing_inputs"]
    assert cov["subdivision_basis"] is None
    assert arb == {"feasibility": arb["feasibility"]}     # feasibility is the only arb input
    assert score_parcel(a["inputs"], W)["arbitrage_component"] > 0  # grounded on feasibility x 0.50


def test_comps_without_acreage_do_not_ground_per_acre():
    comps = [{"sale_price": 80000, "acreage": None, "sale_date": "2025-06-01", "distance_mi": 1.0}]
    a = assemble(_lead(acreage=10, comps=comps), W)
    assert a["coverage"]["arbitrage"]["status"] == "absent"   # per-acre ARV needs comp acreage
    assert a["null_components"]["arbitrage"] is True


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

"""
test_jarvis_brief.py — Jarvis brief composition: honest NULLs, composite labelling,
arbitrage null-vs-0, final_lead_score never conflated.
"""
from app.jarvis_brief import compose_brief, JARVIS_VERSION

AS_OF = "2026-06-19T00:00:00+00:00"


def test_full_brief_composite_labeled_arbitrage_null():
    b = compose_brief({
        "controlling_human": "Peter Colovos", "portfolio_hub": "Colovos — hub: 3 parcels",
        "recipe": "R3", "owner_type": "entity",
        "score_owner": 45, "score_distress": 25,
        "score_signals": {"composite_score": 21.25}, "score_arbitrage": None,
        "alt_use_fitness": 100, "requires_bea": True, "contamination_kind": "part201",
        "kin": [{"name": "X", "relationship": "heir", "is_heir": True}],
    }, AS_OF)
    assert b["version"] == JARVIS_VERSION
    assert b["scores"]["owner"] == 45.0 and b["scores"]["distress"] == 25.0
    assert b["scores"]["arbitrage"] is None              # never 0
    assert b["scores"]["alt_use_fitness"] == 100.0
    assert b["scores"]["signals"] == {"composite_score": 21.25}
    assert b["contamination"] == {"requires_bea": True, "kind": "part201"}
    assert b["narrative"] is None
    assert "composite O45/D25/A—" in b["headline"]       # arbitrage shown as — (null), not 0
    assert "BEA:part201" in b["headline"]
    assert len(b["kin"]) == 1


def test_missing_is_null_not_zero():
    b = compose_brief({"score_owner": None, "score_distress": None, "score_arbitrage": None,
                       "alt_use_fitness": None, "requires_bea": False, "contamination_kind": None},
                      AS_OF)
    assert b["scores"]["owner"] is None and b["scores"]["arbitrage"] is None
    assert b["scores"]["alt_use_fitness"] is None
    assert b["contamination"]["requires_bea"] is False
    assert "A—" in b["headline"] and "alt-use —" in b["headline"]
    assert b["kin"] == []


def test_arbitrage_zero_distinct_from_null():
    # if arbitrage ever IS a grounded 0 (infeasible), it shows 0 — distinct from null/—
    b = compose_brief({"score_arbitrage": 0, "score_owner": 10}, AS_OF)
    assert b["scores"]["arbitrage"] == 0.0
    assert "/A0 " in b["headline"]


def test_final_lead_score_never_in_brief():
    b = compose_brief({"score_owner": 45, "final_lead_score": 999}, AS_OF)
    assert "final_lead_score" not in b              # absentee rank never carried/conflated
    assert "999" not in str(b)

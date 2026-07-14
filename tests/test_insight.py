"""
test_insight.py — DS-07 AI Insights. Pure (no DB, no model): the honesty law (every claim
maps to a field; a null field yields no sentence; a thin record yields an empty brief), the
deterministic do_not_say rails, the grounding hash, model routing, and the spend-meter gates.
The live endpoint + bulk run are pinned in the commit receipt.
"""
import uuid

import pytest

from app import config
from app.insight import grounding, compose, model, meter


# ---- grounding: honest absence + stable hash ---------------------------------------

def _row(**over):
    base = dict(
        lead_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        source_lead_id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        owner_name=None, parcel_apn=None, county_fips=None, signal_tags=None,
        score_distress=None, score_owner=None, score_arbitrage=None, score_signals=None,
        parcel_meta=None, amount_owed=None, assessed_total=None, card_state=None, stage=None,
        portfolio_hub=None, controlling_human=None, contamination_kind=None, requires_bea=None,
        commercial_agent_flag=None, situs_address=None, dnc=None, phone_1=None, vintage_flag=None)
    base.update(over)
    return base


class FakeCur:
    def __init__(self, row, disps=()):
        self._row, self._disps, self._mode = row, disps, None

    def execute(self, sql, params=None):
        self._mode = "disp" if "lead_outcomes" in sql else "row"

    def fetchone(self):
        return self._row if self._mode == "row" else None

    def fetchall(self):
        return [{"disposition": d} for d in self._disps]


def test_null_fields_produce_no_facts():
    g = grounding.build(FakeCur(_row()), "11111111-1111-1111-1111-111111111111")
    # a record with every optional field null -> only the ids resolve, no groundable facts
    assert g["facts"] == {}


def test_every_fact_carries_its_field():
    g = grounding.build(FakeCur(_row(owner_name="Jane Doe", amount_owed=1200,
                                     parcel_meta={"owner_out_of_state": True})),
                        "11111111-1111-1111-1111-111111111111")
    assert g["facts"]["owner_name"]["field"] == "acquisition_leads.owner_name"
    assert g["facts"]["amount_owed"]["value"] == 1200.0
    assert g["facts"]["owner_out_of_state"]["field"] == "acquisition_leads.parcel_meta.owner_out_of_state"


def test_hash_changes_with_a_value_and_is_stable_otherwise():
    a = grounding.hash_facts({"x": {"value": 1, "field": "f"}})
    b = grounding.hash_facts({"x": {"value": 1, "field": "OTHER"}})   # field change: same hash
    c = grounding.hash_facts({"x": {"value": 2, "field": "f"}})       # value change: new hash
    assert a == b and a != c


def test_bad_lead_id_resolves_to_none():
    assert grounding.build(FakeCur(_row()), "not-a-uuid") is None


# ---- compose: the do_not_say rails (always deterministic) ---------------------------

def _facts(**kv):
    return {"facts": {k: {"value": v, "field": "f"} for k, v in kv.items()}}


def test_govt_rail_fires():
    dns = compose.do_not_say(_facts(vintage_flag_govt=True)["facts"])
    assert any("government/treasurer" in d.lower() or "never pitch" in d.lower() for d in dns)


def test_hub_holder_rail_fires_and_names_the_count():
    dns = compose.do_not_say(_facts(hub_parcel_count=8)["facts"])
    assert any("8 parcels" in d and "PORTFOLIO HOLDER" in d for d in dns)


def test_dnc_rail_fires():
    dns = compose.do_not_say(_facts(dnc=True)["facts"])
    assert any("not callable" in d.lower() for d in dns)


def test_thin_record_is_empty_brief():
    b = compose.compose(_facts())          # no facts at all
    assert compose.is_empty(b)
    assert b["do_not_say"] == []


def test_angle_hub_is_portfolio_not_distress():
    a = compose.angle(_facts(hub_parcel_count=5, amount_owed=1000)["facts"])
    assert "portfolio" in a.lower() and "distress" in a.lower()


def test_angle_senior_is_legacy():
    a = compose.angle(_facts(senior_score=60, tenure_years=30)["facts"])
    assert "legacy" in a.lower() or "succession" in a.lower()


def test_what_we_know_only_states_present_facts_and_caps_at_three():
    facts = _facts(owner_name="X Co", tenure_years=20, senior_score=55, cluster_size=3,
                   same_surname_family=True, amount_owed=999, acreage=12)["facts"]
    text = compose.what_we_know(facts)
    assert text.count(".") <= 3          # 2-3 sentences
    assert "invent" not in text.lower()


# ---- model routing + parsing -------------------------------------------------------

def test_route_high_score_needs_a_paid_key(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    assert model.route({"high_score": True}) == "ollama"    # no key -> local
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    assert model.route({"high_score": True}) == "claude"
    assert model.route({"high_score": False}) == "ollama"


def test_estimate_cost_local_is_zero():
    assert model.estimate_cost("sys", "user", "ollama") == 0.0
    assert model.estimate_cost("sys", "user", "claude") > 0.0


def test_parse_parts_tolerates_prose_wrapper():
    p = model._parse_parts('here you go: {"what_we_know": "a", "angle": "b"} thanks')
    assert p == {"what_we_know": "a", "angle": "b"}
    with pytest.raises(model.ModelOffline):
        model._parse_parts("no json here")


# ---- meter gates -------------------------------------------------------------------

class SpendCur:
    def __init__(self, month=0.0):
        self._month = month

    def execute(self, sql, params=None):
        self._last = sql

    def fetchone(self):
        return {"s": self._month, "n": 0}


def test_kill_switch_blocks(monkeypatch):
    monkeypatch.setattr(config, "INSIGHT_KILL_SWITCH", True)
    with pytest.raises(meter.BudgetExceeded):
        meter.check(SpendCur(), 0.0)


def test_per_run_cap_blocks(monkeypatch):
    monkeypatch.setattr(config, "INSIGHT_KILL_SWITCH", False)
    monkeypatch.setattr(config, "INSIGHT_PER_RUN_CAP_USD", 0.10)
    with pytest.raises(meter.BudgetExceeded):
        meter.check(SpendCur(), 0.50)


def test_monthly_cap_blocks(monkeypatch):
    monkeypatch.setattr(config, "INSIGHT_KILL_SWITCH", False)
    monkeypatch.setattr(config, "INSIGHT_PER_RUN_CAP_USD", 1.0)
    monkeypatch.setattr(config, "INSIGHT_MONTHLY_CAP_USD", 25.0)
    with pytest.raises(meter.BudgetExceeded) as e:
        meter.check(SpendCur(month=24.9), 0.5)      # 24.9 + 0.5 > 25
    assert "budget reached" in e.value.reason


def test_local_generation_never_trips_dollar_caps(monkeypatch):
    monkeypatch.setattr(config, "INSIGHT_KILL_SWITCH", False)
    monkeypatch.setattr(config, "INSIGHT_MONTHLY_CAP_USD", 0.0)
    meter.check(SpendCur(month=999), 0.0)           # est_cost 0 -> no raise

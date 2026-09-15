"""The spine-backed free contact routes (T6 / I2 / I3) and the engine cache, per target.

A fake read-only connection answers the executors' own SQL, so the court proves the
decision logic against the real statements without touching a database.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.free_first import guard as G
from app.free_first.jobs import open_job
from app.free_first.ledger import FreeFirstLedger
from app.free_first.policy import exhaustion
from app.free_first.route_executors import (ESTATE_ROWS_SQL, PARTY_COUNT_SQL, PARTY_PHONES_SQL, ROUTE_ATTEMPTS_SQL,
                                           SpineRoutes, default_executors, mask_phone)
from app.free_first.runner import run_free_ladder

ROW = "11111111-1111-1111-1111-111111111111"
OBS = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)


class Cursor:
    def __init__(self, conn):
        self.conn, self.rows = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.conn.calls.append((sql, params))
        if sql == ESTATE_ROWS_SQL:
            self.rows = [(ROW,)] if params["apn"] in self.conn.estate else []
        elif sql == ROUTE_ATTEMPTS_SQL:
            self.rows = self.conn.attempts.get(params["route"], [])
        elif sql == PARTY_PHONES_SQL:
            roles = params["roles"]
            self.rows = [p for p in self.conn.phones if roles is None or p[1] in roles]
        elif sql == PARTY_COUNT_SQL:
            roles = params["roles"]
            self.rows = [(len({p for p, r in self.conn.parties if roles is None or r in roles}),)]
        else:
            raise AssertionError(f"unexpected SQL: {sql[:60]}")

    def fetchall(self):
        return self.rows


class Spine:
    def __init__(self, *, estate=("1737078351",), attempts=None, phones=(), parties=()):
        self.estate, self.attempts, self.phones, self.parties = set(estate), dict(attempts or {}), list(phones), list(parties)
        self.calls, self.read_only = [], False

    def cursor(self):
        return Cursor(self)


def job(tmp_path, fact="contact.phone", apn="1737078351"):
    ledger = FreeFirstLedger(tmp_path / "ff.jsonl")
    j = open_job(ledger, tenant_id="t14", fact=fact, target={"target_key": f"lead:{apn}", "apn": apn},
                 state="NC", county_fips="37183", requested_by={"kind": "court"})
    return ledger, j


PHONE = ("party-1", "OWNER", "pp-1", "+19195550101", "mobile", "HIGH", OBS, "ev-1", "NCSBE_PUBLIC_VOTER_FILE", OBS, OBS)


def test_t6_hit_is_the_lanes_own_phone_masked_and_dated_by_the_voter_file(tmp_path):
    spine = Spine(attempts={"T6": [("HIT", "NCSBE_PUBLIC_VOTER_FILE", "2026-08-31", OBS, "run-1",
                                    {"phones": 1, "resolution": "PHONE_APPENDED_HIGH"})]}, phones=[PHONE])
    ledger, j = job(tmp_path)
    run = run_free_ladder(ledger, j.job_id, {"route.T6": SpineRoutes(spine).t6}, worker_id="court")
    t6 = next(a for a in ledger.job_state(j.job_id).attempts if a["step_id"] == "route.T6")
    ev = t6["evidence_found"][0]
    assert t6["outcome"] == "HIT" and run.exhaustion["verdict"] == "RESOLVED_FREE"
    assert ev["value"]["phone_masked"] == "***-***-0101" and "+19195550101" not in str(ev)
    assert ev["source_effective_date"] == "2026-08-31" and ev["confidence"] == "HIGH"
    assert ev["value"]["contact_evidence_id"] == "ev-1"


def test_t6_no_hit_carries_the_lanes_resolution_and_the_ladder_goes_to_a_human(tmp_path):
    spine = Spine(attempts={"T6": [("NO_HIT", "NCSBE_PUBLIC_VOTER_FILE", "2026-08-31", OBS, "run-1",
                                    {"phones": 0, "resolution": "DECEASED_OWNER_NEEDS_HEIR_IDENTITY"})]})
    ledger, j = job(tmp_path)
    run = run_free_ladder(ledger, j.job_id, SpineRoutes(spine).executors(), worker_id="court")
    state = ledger.job_state(j.job_id)
    by_step = {a["step_id"]: a for a in state.attempts}
    assert by_step["route.T6"]["evidence_not_found"]["resolution"] == "DECEASED_OWNER_NEEDS_HEIR_IDENTITY"
    assert by_step["route.I2"]["outcome"] == "NO_HIT" and by_step["route.I3"]["outcome"] == "NOT_APPLICABLE"
    ex = exhaustion(state)
    # free evidence exhausted by the machine; the manual people-search route is the human's step
    assert ex.machine_exhausted and ex.verdict == "HUMAN_STEP_PENDING" and ex.pending_human == ["route.T10"]
    assert run.human_steps and run.human_steps[0]["step_id"] == "route.T10"
    with G.bound_job(ledger, j.job_id), pytest.raises(G.PaidCallBlocked) as blocked:
        G.clear_paid_call("tracerfy", items=[("contact.phone", j.target_key)], est_cost_usd=0.02, caller="court")
    assert any("HUMAN_STEP_PENDING:route.T10" in r for r in blocked.value.reasons)


def test_a_parcel_the_lane_never_covered_keeps_the_free_path_open(tmp_path):
    spine = Spine(estate=())
    ledger, j = job(tmp_path)
    run = run_free_ladder(ledger, j.job_id, SpineRoutes(spine).executors(), worker_id="court")
    outcomes = {a["step_id"]: a["outcome"] for a in ledger.job_state(j.job_id).attempts}
    assert outcomes == {"route.I2": "UNWIRED", "route.I3": "UNWIRED", "route.T6": "UNWIRED"}
    assert run.exhaustion["verdict"] == "FREE_PATH_OPEN"


def test_i2_reaches_a_phone_through_the_identity_graph_and_i3_only_heirs(tmp_path):
    heir_phone = ("party-2", "HEIR", "pp-2", "+19195550199", "mobile", "MEDIUM", OBS, "ev-2", "NCSBE_PUBLIC_VOTER_FILE",
                  OBS, OBS)
    spine = Spine(attempts={"T6": [("NO_HIT", "NCSBE_PUBLIC_VOTER_FILE", "2026-08-31", OBS, "r", {})]},
                  phones=[heir_phone], parties=[("party-2", "HEIR")])
    routes = SpineRoutes(spine)
    ledger, j = job(tmp_path)
    run_free_ladder(ledger, j.job_id, routes.executors(), worker_id="court")
    by_step = {a["step_id"]: a for a in ledger.job_state(j.job_id).attempts}
    assert by_step["route.I2"]["outcome"] == "HIT"          # graph route answers first (INTERNAL scope)
    assert by_step["route.I2"]["evidence_found"][0]["value"]["role"] == "HEIR"
    assert "route.I3" not in by_step                         # the ladder stops at the first free HIT


def test_t6_for_a_mailing_fact_is_unwired_not_a_false_no_hit(tmp_path):
    spine = Spine(attempts={"T6": [("NO_HIT", "NCSBE_PUBLIC_VOTER_FILE", "2026-08-31", OBS, "r", {})]})
    ledger, j = job(tmp_path, fact="contact.mailing_address")
    run_free_ladder(ledger, j.job_id, {"route.T6": SpineRoutes(spine).t6}, worker_id="court")
    t6 = next(a for a in ledger.job_state(j.job_id).attempts if a["step_id"] == "route.T6")
    assert t6["outcome"] == "UNWIRED"


def test_local_cache_reads_the_engine_cache_when_given_a_connection(tmp_path):
    class CacheCursor:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            assert "from landos.owner_leads" in sql and params[1:] == ("18141", "APN-9")

        def fetchall(self):
            return [{"lead_id": "L1", "county_fips": "18141", "parcel_apn": "APN-9", "owner_name": "CACHED OWNER",
                     "situs_address": "1 MAIN"}]

    class CacheConn:
        def cursor(self, **kw):
            return CacheCursor()

    ledger = FreeFirstLedger(tmp_path / "ff.jsonl")
    j = open_job(ledger, tenant_id="t14", fact="parcel.owner_identity",
                 target={"target_key": "parcel:18141:APN-9", "apn": "APN-9"}, state="IN", county_fips="18141",
                 requested_by={"kind": "court"})
    run = run_free_ladder(ledger, j.job_id, default_executors("t14", engine_conn=CacheConn()), worker_id="court")
    a = ledger.job_state(j.job_id).attempts[0]
    assert a["outcome"] == "HIT" and run.exhaustion["verdict"] == "RESOLVED_FREE"
    ev = a["evidence_found"][0]
    assert ev["as_of_basis"] == "UNKNOWN" and ev["freshness_state"] == "UNKNOWN"   # a cache row is not dated by its bookkeeping


def test_masking_never_leaks_a_number():
    assert mask_phone("+19195550101") == "***-***-0101" and mask_phone(None) == "***"


def test_t6_applies_in_every_nc_county_and_an_uncovered_county_stays_open(tmp_path):
    """NCSBE publishes the voter file statewide: a phone ladder in an NC county the lane
    has not reached (Pender) carries T6 / I2 / I3 and stays FREE_PATH_OPEN -- it is never
    vacuously 'machine exhausted' with only the manual step left."""
    from app.free_first.knowledge import plan_ladder
    from app.fips import state_county_fips
    assert len(state_county_fips("NC")) == 100
    steps = {s.step_id for s in plan_ladder("contact.phone", state="NC", county_fips="37141").steps}
    assert {"route.T6", "route.I2", "route.I3", "route.T10"} <= steps
    ledger = FreeFirstLedger(tmp_path / "ff.jsonl")
    j = open_job(ledger, tenant_id="t14", fact="contact.phone", target={"target_key": "parcel:37141:X1", "apn": "X1"},
                 state="NC", county_fips="37141", requested_by={"kind": "court"})
    run = run_free_ladder(ledger, j.job_id, SpineRoutes(Spine(estate=())).executors(), worker_id="court")
    assert run.exhaustion["verdict"] == "FREE_PATH_OPEN" and not run.exhaustion["machine_exhausted"]
    with G.bound_job(ledger, j.job_id), pytest.raises(G.PaidCallBlocked) as blocked:
        G.clear_paid_call("tracerfy", items=[("contact.phone", j.target_key)], est_cost_usd=0.02, caller="court")
    assert any("FREE_PATH_OPEN:route.I2,route.I3,route.T6" in r for r in blocked.value.reasons)
    t6 = next(s for s in run.exhaustion["steps"] if s["step_id"] == "route.T6")
    assert t6["status"] == "UNWIRED" and t6["detail"] == "parcel is not in the estate the T6 lane covers"


def test_a_job_opened_before_the_coverage_grew_is_extended_not_left_exhausted(tmp_path, monkeypatch):
    from app.free_first import knowledge
    ledger = FreeFirstLedger(tmp_path / "ff.jsonl")
    narrow = {**knowledge.ROUTE_COVERAGE, **{r: (knowledge.ROUTE_COVERAGE[r][0], frozenset({"37183"}))
                                             for r in ("T6", "I2", "I3")}}
    monkeypatch.setattr(knowledge, "ROUTE_COVERAGE", narrow)
    target = {"target_key": "parcel:37141:X2", "apn": "X2"}
    j = open_job(ledger, tenant_id="t14", fact="contact.phone", target=target, state="NC", county_fips="37141",
                 requested_by={"kind": "court"})
    assert {s["step_id"] for s in j.steps} == {"route.T10"}
    monkeypatch.undo()
    again = open_job(ledger, tenant_id="t14", fact="contact.phone", target=target, state="NC", county_fips="37141",
                     requested_by={"kind": "court"})
    assert {"route.T6", "route.I2", "route.I3"} <= {s["step_id"] for s in again.steps}
    assert exhaustion(again).verdict == "FREE_PATH_OPEN"

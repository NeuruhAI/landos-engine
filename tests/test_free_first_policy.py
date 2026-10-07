"""FREE-FIRST policy court: the seven properties the founder's rule needs, proven on the
real ledger / policy / guard / authority code (no network, no database).

 1. paid is blocked while a free source remains
 2. paid is allowed after genuine exhaustion plus a confirmed authorization
 3. paid is blocked when exhaustion exists but spend is unauthorized (or forged)
 4. a retry / restart / handoff never erases free-source history
 5. jurisdiction-specific routes win where canonical knowledge exists
 6. provenance survives normalization
 7. downstream workers cannot bypass the gate (tests/test_free_first_call_sites.py
    proves it at every paid call site; the binding cases are here)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.free_first import authority as A
from app.free_first import guard as G
from app.free_first.jobs import (AttemptRefused, open_job, record_attempt, record_human_outcome,
                                 request_spend_authorization)
from app.free_first.knowledge import plan_ladder
from app.free_first.ledger import FreeFirstLedger, LedgerCorrupt
from app.free_first.policy import exhaustion
from app.free_first.provenance import ProvenanceRefused, dated_from_record, make_evidence, utc_now_iso
from app.free_first.runner import AttemptResult, run_free_ladder

TENANT = "t14-free-first-court"
REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# a deterministic spend authority for the court (the runtime default is FailClosed)
# ---------------------------------------------------------------------------

class KeyedCourtAuthority:
    """Signs what it authorizes; confirms only its own signatures. A SPEND_AUTHORIZED
    line written by anyone without the key does not confirm."""
    name = "COURT_KEYED"

    def __init__(self, secret: bytes = b"court-founder-secret", ttl_hours: int = 1):
        self.secret, self.ttl = secret, ttl_hours

    def _sig(self, body: dict) -> str:
        unsigned = {k: v for k, v in body.items() if k != "authority_ref"}
        return hmac.new(self.secret, json.dumps(unsigned, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def authorize(self, request: A.SpendRequest) -> A.SpendAuthorization:
        now = datetime.now(timezone.utc)
        grant = A.SpendAuthorization(
            authorization_id="auth_" + request.job_id[-8:], job_id=request.job_id, fact=request.fact,
            providers=sorted(request.providers), max_total_usd=request.max_total_usd,
            max_per_call_usd=request.max_per_call_usd, waives=dict(request.waives), authority=self.name,
            authority_ref={}, issued_at=now.isoformat(), expires_at=(now + timedelta(hours=self.ttl)).isoformat(),
            requested_by=request.requested_by, facts_sha256="court")
        return replace(grant, authority_ref={"sig": self._sig(grant.to_dict())})

    def confirm(self, authorization, *, now):
        body = {k: v for k, v in authorization.items() if k not in ("seq", "entry_sha", "at")}
        if not hmac.compare_digest((authorization.get("authority_ref") or {}).get("sig", ""), self._sig(body)):
            raise A.AuthorityRefused("SIGNATURE_INVALID")


@pytest.fixture()
def court_authority():
    auth = KeyedCourtAuthority()
    A.install_authority_for_tests(auth)
    yield auth
    A.install_authority_for_tests(None)


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.delenv(G.ENV_LEDGER, raising=False)
    monkeypatch.delenv(G.ENV_JOB, raising=False)
    monkeypatch.delenv(G.ENV_JOBS, raising=False)
    return FreeFirstLedger(tmp_path / "free_first.jsonl")


WORKER = {"worker_id": "court-worker-1", "pid": 1, "host": "court"}


def berrien_owner_job(ledger, apn="11-01-0001-0001-00-1"):
    """Berrien MI: the engine's chain is its verified county REST tier only (config.py keeps
    local_cache out of the A2 cohort counties)."""
    return open_job(ledger, tenant_id=TENANT, fact="parcel.owner_identity",
                    target={"target_key": f"parcel:26021:{apn}", "entity_kind": "parcel", "apn": apn},
                    state="MI", county_fips="26021", requested_by={"kind": "court", "id": "t14"})


def not_found(query="apn=X"):
    return {"query": query, "result_count": 0, "searched_at": utc_now_iso()}


def exhaust_automated(ledger, job_id):
    for step in ledger.job_state(job_id).steps:
        if step["mode"] == "AUTOMATED":
            now = utc_now_iso()
            record_attempt(ledger, job_id, step_id=step["step_id"], outcome="NO_HIT", started_at=now,
                           finished_at=now, worker=WORKER, evidence_not_found=not_found(step["step_id"]))


def exhaust_human(ledger, job_id):
    for step in ledger.job_state(job_id).steps:
        if step["mode"] == "MANUAL":
            record_human_outcome(ledger, job_id, step_id=step["step_id"], outcome="NO_HIT", actor="operator:court",
                                 evidence_not_found={"searched": step["step_id"], "result_count": 0})


# ---------------------------------------------------------------------------
# 1. paid blocked when a free source remains
# ---------------------------------------------------------------------------

def test_1_paid_is_blocked_while_any_free_source_remains(ledger, court_authority):
    """Wake NC mailing address: the engine's cache, T6 (NCSBE) and T8 (Wake open data) are free."""
    job = open_job(ledger, tenant_id=TENANT, fact="contact.mailing_address",
                   target={"target_key": "party:wake:owner-1"}, state="NC", county_fips="37183",
                   requested_by={"kind": "court"})
    automated = [s["step_id"] for s in job.steps if s["mode"] == "AUTOMATED"]
    assert automated == ["parcel.local_cache", "route.T6", "route.T8"]
    # even a confirmed authorization (waiving every human route) cannot buy past an open machine path
    manual = {s["step_id"]: "court: human routes waived" for s in job.steps if s["mode"] == "MANUAL"}
    request_spend_authorization(ledger, job.job_id, providers=["tracerfy"], max_total_usd=1.0,
                                max_per_call_usd=0.5, requested_by="founder:court", waives=manual)
    with G.bound_job(ledger, job.job_id):
        with pytest.raises(G.PaidCallBlocked):       # tracerfy cannot establish a mailing address at all
            G.clear_paid_call("tracerfy", items=[("contact.mailing_address", job.target_key)], est_cost_usd=0.1,
                              caller="court")
    berrien = berrien_owner_job(ledger)
    request_spend_authorization(ledger, berrien.job_id, providers=["reportall"], max_total_usd=1.0,
                                max_per_call_usd=0.5, requested_by="founder:court")
    with G.bound_job(ledger, berrien.job_id):
        with pytest.raises(G.PaidCallBlocked) as blocked:
            G.clear_paid_call("reportall", items=[("parcel.owner_identity", berrien.target_key)],
                              est_cost_usd=0.1, caller="court")
    assert any(r.endswith("FREE_PATH_OPEN:parcel.county_rest.26021") for r in blocked.value.reasons)
    # one free source tried, others left: still blocked, and exactly the open ones are named
    now = utc_now_iso()
    record_attempt(ledger, job.job_id, step_id="route.T6", outcome="NO_HIT", started_at=now, finished_at=now,
                   worker=WORKER, evidence_not_found=not_found("ncsbe name+address"))
    state = ledger.job_state(job.job_id)
    from app.free_first.policy import decide_paid
    decision = decide_paid(state, "reportall", est_cost_usd=0.1, authorization=state.authorizations[-1])
    assert "FREE_PATH_OPEN:parcel.local_cache,route.T8" in decision.reasons and not decision.allow
    decisions = [e for e in ledger.entries() if e["kind"] == "PAID_DECISION"]
    assert len(decisions) == 2 and not any(d["body"]["allow"] for d in decisions)


def test_1b_a_manual_free_route_is_a_free_path_too(ledger, court_authority):
    """NC Wake phone: T6 / I2 / I3 automated, T10 manual. Machine exhaustion alone is not enough."""
    job = open_job(ledger, tenant_id=TENANT, fact="contact.phone", target={"target_key": "party:wake:1"},
                   state="NC", county_fips="37183", requested_by={"kind": "court"})
    exhaust_automated(ledger, job.job_id)
    ex = exhaustion(ledger.job_state(job.job_id))
    assert ex.machine_exhausted and ex.verdict == "HUMAN_STEP_PENDING" and ex.pending_human == ["route.T10"]
    request_spend_authorization(ledger, job.job_id, providers=["tracerfy"], max_total_usd=1.0,
                                max_per_call_usd=0.05, requested_by="founder:court")
    with G.bound_job(ledger, job.job_id), pytest.raises(G.PaidCallBlocked) as blocked:
        G.clear_paid_call("tracerfy", items=[("contact.phone", job.target_key)], est_cost_usd=0.02, caller="court")
    assert any("HUMAN_STEP_PENDING:route.T10" in r for r in blocked.value.reasons)


def test_1c_a_free_hit_means_nothing_is_bought(ledger, court_authority):
    job = berrien_owner_job(ledger)
    ev = make_evidence(fact="parcel.owner_identity", source_id="county_rest:26021", value={"owner_name": "X"},
                       retrieved_at=utc_now_iso(), confidence="HIGH", locator="court", raw_payload={"x": 1},
                       live_query=True)
    now = utc_now_iso()
    record_attempt(ledger, job.job_id, step_id="parcel.county_rest.26021", outcome="HIT", started_at=now,
                   finished_at=now, worker=WORKER, evidence_found=[ev])
    request_spend_authorization(ledger, job.job_id, providers=["reportall"], max_total_usd=1.0,
                                max_per_call_usd=0.5, requested_by="founder:court")
    with G.bound_job(ledger, job.job_id), pytest.raises(G.PaidCallBlocked) as blocked:
        G.clear_paid_call("reportall", items=[("parcel.owner_identity", job.target_key)], est_cost_usd=0.1,
                          caller="court")
    assert any("FACT_RESOLVED_FREE:parcel.county_rest.26021" in r for r in blocked.value.reasons)


# ---------------------------------------------------------------------------
# 2. paid allowed after genuine exhaustion plus authorization -- and bounded by it
# ---------------------------------------------------------------------------

def test_2_paid_is_allowed_only_after_exhaustion_and_a_confirmed_authorization(ledger, court_authority):
    job = berrien_owner_job(ledger)
    exhaust_automated(ledger, job.job_id)
    assert exhaustion(ledger.job_state(job.job_id)).verdict == "EXHAUSTED"
    request_spend_authorization(ledger, job.job_id, providers=["reportall"], max_total_usd=0.25,
                                max_per_call_usd=0.10, requested_by="founder:court", reason="court")
    with G.bound_job(ledger, job.job_id):
        with G.paid_call("reportall", items=[("parcel.owner_identity", job.target_key)], est_cost_usd=0.10,
                         caller="court") as clearance:
            clearance.settle(cost_usd=0.10, outcome="COMPLETED", evidence_found=[{"owner": "found by vendor"}])
        state = ledger.job_state(job.job_id)
        assert state.paid_decisions[-1]["allow"] is True and state.spent_usd() == 0.10
        # bounded: the per-call cap and the total budget both hold
        with pytest.raises(G.PaidCallBlocked) as over_call:
            G.clear_paid_call("reportall", items=[("parcel.owner_identity", job.target_key)], est_cost_usd=0.20,
                              caller="court")
        assert any("OVER_PER_CALL_CAP" in r for r in over_call.value.reasons)
        G.clear_paid_call("reportall", items=[("parcel.owner_identity", job.target_key)], est_cost_usd=0.10,
                          caller="court").settle(cost_usd=0.10, outcome="COMPLETED")
        with pytest.raises(G.PaidCallBlocked) as over_budget:
            G.clear_paid_call("reportall", items=[("parcel.owner_identity", job.target_key)], est_cost_usd=0.10,
                              caller="court")
        assert any("OVER_BUDGET" in r for r in over_budget.value.reasons)
        # a provider the authorization does not name is still blocked
        with pytest.raises(G.PaidCallBlocked) as other:
            G.clear_paid_call("regrid", items=[("parcel.owner_identity", job.target_key)], est_cost_usd=0.01,
                              caller="court")
        assert any("SPEND_UNAUTHORIZED" in r for r in other.value.reasons)


def test_2b_an_unpriced_provider_needs_a_per_call_cap(ledger, court_authority):
    job = berrien_owner_job(ledger)
    exhaust_automated(ledger, job.job_id)
    request_spend_authorization(ledger, job.job_id, providers=["reportall"], max_total_usd=1.0,
                                max_per_call_usd=None, requested_by="founder:court")
    with G.bound_job(ledger, job.job_id), pytest.raises(G.PaidCallBlocked) as blocked:
        G.clear_paid_call("reportall", items=[("parcel.owner_identity", job.target_key)], est_cost_usd=None,
                          caller="court")
    assert any("UNPRICED_WITHOUT_PER_CALL_CAP" in r for r in blocked.value.reasons)


# ---------------------------------------------------------------------------
# 3. paid blocked when exhaustion exists but spend is unauthorized
# ---------------------------------------------------------------------------

def test_3_exhaustion_without_authorization_is_still_blocked(ledger):
    job = berrien_owner_job(ledger)
    exhaust_automated(ledger, job.job_id)
    assert exhaustion(ledger.job_state(job.job_id)).verdict == "EXHAUSTED"
    # the runtime default authority fails closed: it cannot even be asked
    with pytest.raises(A.AuthorityRefused) as refused:
        request_spend_authorization(ledger, job.job_id, providers=["reportall"], max_total_usd=1.0,
                                    max_per_call_usd=0.5, requested_by="worker:auto")
    assert refused.value.reason == "NO_SPEND_AUTHORITY_CONFIGURED"
    with G.bound_job(ledger, job.job_id), pytest.raises(G.PaidCallBlocked) as blocked:
        G.clear_paid_call("reportall", items=[("parcel.owner_identity", job.target_key)], est_cost_usd=0.1,
                          caller="court")
    assert blocked.value.reasons == ["parcel:26021:11-01-0001-0001-00-1:SPEND_UNAUTHORIZED:NONE_RECORDED:FAIL_CLOSED"]


def test_3b_a_forged_authorization_line_is_not_authority(ledger, court_authority):
    job = berrien_owner_job(ledger)
    exhaust_automated(ledger, job.job_id)
    forged = {"authorization_id": "auth_forged", "job_id": job.job_id, "fact": job.fact, "providers": ["reportall"],
              "max_total_usd": 100.0, "max_per_call_usd": 100.0, "waives": {}, "authority": "COURT_KEYED",
              "authority_ref": {"sig": "0" * 64}, "issued_at": utc_now_iso(),
              "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
              "requested_by": "worker:rogue", "facts_sha256": "x", "version": A.AUTHORIZATION_VERSION}
    ledger.append("SPEND_AUTHORIZED", job.job_id, forged)          # anyone can append a line...
    with G.bound_job(ledger, job.job_id), pytest.raises(G.PaidCallBlocked) as blocked:
        G.clear_paid_call("reportall", items=[("parcel.owner_identity", job.target_key)], est_cost_usd=0.1,
                          caller="court")
    assert any("SPEND_UNAUTHORIZED:SIGNATURE_INVALID" in r for r in blocked.value.reasons)   # ...none confirm


def test_3c_governance_is_the_spend_authority_and_its_receipts_are_the_proof(ledger):
    """founder_required records nothing; an allow is re-confirmed against the receipts log."""
    receipts = []

    def port(facts):
        if facts["spend_usd"] > 0.5:
            return {"schema_version": A.GOVERNANCE_AUTHORIZATION_SCHEMA, "execution": A.GOVERNANCE_NOT_CLAIMED,
                    "facts_nonce": facts["nonce"], "decision": "founder_required", "risk_tier": "R4",
                    "authorization_id": "gauth_pending"}
        entry = {"kind": "governance_authorization", "decision": "allow", "action_class": "spend",
                 "facts_nonce": facts["nonce"], "authorization_id": "gauth_1", "target": facts["target"],
                 "risk_tier": "R4", "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
                 "entry_hash": "e" * 64, "seq": 7}
        receipts.append(entry)
        return {"schema_version": A.GOVERNANCE_AUTHORIZATION_SCHEMA, "execution": A.GOVERNANCE_NOT_CLAIMED,
                **{k: entry[k] for k in ("facts_nonce", "decision", "action_class", "authorization_id", "target",
                                         "risk_tier", "expires_at")},
                "decision_receipt_hash": entry["entry_hash"], "receipt_seq": 7}

    gov = A.GovernanceSpendAuthority(authorize_port=port, receipts_reader=lambda: receipts, source_sha="court")
    A.install_authority_for_tests(gov)
    try:
        job = berrien_owner_job(ledger)
        exhaust_automated(ledger, job.job_id)
        with pytest.raises(A.AuthorityRefused) as founder:
            request_spend_authorization(ledger, job.job_id, providers=["reportall"], max_total_usd=5.0,
                                        max_per_call_usd=1.0, requested_by="worker:auto")
        assert founder.value.reason == "GOVERNANCE_FOUNDER_REQUIRED"
        assert not ledger.job_state(job.job_id).authorizations
        request_spend_authorization(ledger, job.job_id, providers=["reportall"], max_total_usd=0.25,
                                    max_per_call_usd=0.25, requested_by="founder:session")
        with G.bound_job(ledger, job.job_id):
            G.clear_paid_call("reportall", items=[("parcel.owner_identity", job.target_key)], est_cost_usd=0.1,
                              caller="court").settle(cost_usd=0.1, outcome="COMPLETED")
            receipts.clear()                                   # Governance no longer holds the receipt
            with pytest.raises(G.PaidCallBlocked) as blocked:
                G.clear_paid_call("reportall", items=[("parcel.owner_identity", job.target_key)],
                                  est_cost_usd=0.1, caller="court")
        assert any("GOVERNANCE_RECEIPT_NOT_FOUND" in r for r in blocked.value.reasons)
    finally:
        A.install_authority_for_tests(None)


# ---------------------------------------------------------------------------
# 4. retry does not erase free-source history
# ---------------------------------------------------------------------------

def test_4_retries_restarts_and_handoffs_keep_every_attempt(ledger, tmp_path):
    job = berrien_owner_job(ledger)
    calls = []

    def flaky(step, state):
        calls.append(step["step_id"])
        if len(calls) == 1:
            raise TimeoutError("county GIS timed out")
        return AttemptResult("NO_HIT", evidence_not_found=not_found("retry"))

    run1 = run_free_ladder(ledger, job.job_id, {"county_rest": flaky}, worker_id="worker-a")
    assert [a["outcome"] for a in run1.attempted] == ["ERROR"]
    # restart: a new ledger object (a new process would do the same) and a different worker
    reopened = FreeFirstLedger(ledger.path)
    again = berrien_owner_job(reopened)                        # a later run asks the same question
    assert again.job_id == job.job_id and len(again.attempts) == 1
    run2 = run_free_ladder(reopened, job.job_id, {"county_rest": flaky}, worker_id="worker-b")
    assert [a["outcome"] for a in run2.attempted] == ["NO_HIT"]
    state = reopened.job_state(job.job_id)
    assert [a["outcome"] for a in state.attempts] == ["ERROR", "NO_HIT"]
    assert {a["worker"]["worker_id"] for a in state.attempts} == {"worker-a", "worker-b"}
    # a third run skips the exhausted step: it is never re-run, never re-billed to history
    run3 = run_free_ladder(reopened, job.job_id, {"county_rest": flaky}, worker_id="worker-c")
    assert run3.attempted == [] and run3.skipped[0]["status"] == "EXHAUSTED"
    assert calls == ["parcel.county_rest.26021", "parcel.county_rest.26021"]
    # nothing can shorten history: a truncated or edited ledger refuses to open
    lines = ledger.path.read_text().splitlines()
    (tmp_path / "truncated.jsonl").write_text("\n".join(lines[:1] + lines[2:]) + "\n")
    with pytest.raises(LedgerCorrupt):
        FreeFirstLedger(tmp_path / "truncated.jsonl")
    edited = lines[:]
    edited[1] = edited[1].replace('"ERROR"', '"NOT_APPLICABLE"')
    (tmp_path / "edited.jsonl").write_text("\n".join(edited) + "\n")
    with pytest.raises(LedgerCorrupt):
        FreeFirstLedger(tmp_path / "edited.jsonl")


def test_4b_a_stale_attempt_stays_on_record_but_the_source_must_be_retried(ledger):
    job = berrien_owner_job(ledger)
    old = (datetime.now(timezone.utc) - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for step in job.steps:
        record_attempt(ledger, job.job_id, step_id=step["step_id"], outcome="NO_HIT", started_at=old,
                       finished_at=old, worker=WORKER, evidence_not_found=not_found())
    ex = exhaustion(ledger.job_state(job.job_id))
    assert ex.verdict == "FREE_PATH_OPEN" and set(ex.open_automated) == {s["step_id"] for s in job.steps}
    assert len(ledger.job_state(job.job_id).attempts) == len(job.steps)


def test_4d_a_source_this_runtime_cannot_run_keeps_paid_blocked(ledger, court_authority):
    """St. Joseph IN: the only free step is the engine cache. A runtime without the engine
    database records it UNWIRED -- the free path is not exhausted, so paid stays blocked."""
    job = open_job(ledger, tenant_id=TENANT, fact="parcel.owner_identity", target={"target_key": "parcel:18141:Z",
                   "apn": "Z"}, state="IN", county_fips="18141", requested_by={"kind": "court"})
    run = run_free_ladder(ledger, job.job_id, {}, worker_id="w")
    assert [a["outcome"] for a in run.attempted] == ["UNWIRED"] and run.exhaustion["verdict"] == "FREE_PATH_OPEN"


def test_4c_a_newly_learned_free_source_is_owed_an_attempt_before_paid(ledger, court_authority):
    job = open_job(ledger, tenant_id=TENANT, fact="parcel.owner_identity", target={"target_key": "parcel:18141:X"},
                   state="IN", county_fips="18141", requested_by={"kind": "court"})
    exhaust_automated(ledger, job.job_id)
    assert exhaustion(ledger.job_state(job.job_id)).verdict == "EXHAUSTED"
    learned = {"source_id": "in.stjoseph.assessor.gis", "facts": ["parcel.owner_identity"], "scope": "COUNTY",
               "mode": "AUTOMATED", "source_url": "https://example.invalid/gis"}
    grown = open_job(ledger, tenant_id=TENANT, fact="parcel.owner_identity", target={"target_key": "parcel:18141:X"},
                     state="IN", county_fips="18141", requested_by={"kind": "court"}, extra_free_sources=[learned])
    assert grown.job_id == job.job_id and "extra.in.stjoseph.assessor.gis" in [s["step_id"] for s in grown.steps]
    assert exhaustion(grown).open_automated == ["extra.in.stjoseph.assessor.gis"]


# ---------------------------------------------------------------------------
# 5. jurisdiction-specific routes win where canonical knowledge exists
# ---------------------------------------------------------------------------

def test_5_jurisdiction_knowledge_orders_the_ladder(ledger):
    generic = {"source_id": "national.people.index", "facts": ["contact.mailing_address"], "scope": "NATIONAL",
               "mode": "AUTOMATED", "order": 0}
    claims_county = {"source_id": "caller.county.guess", "facts": ["contact.mailing_address"], "scope": "COUNTY",
                     "mode": "AUTOMATED"}
    wake = plan_ladder("contact.mailing_address", state="NC", county_fips="37183",
                       extra_free_sources=[generic, claims_county])
    ids = [s.step_id for s in wake.steps]
    # Wake's own automated routes come first, the caller's county claim after them, generic last
    assert ids.index("route.T6") < ids.index("extra.caller.county.guess")
    assert ids.index("route.T8") < ids.index("extra.caller.county.guess")
    assert ids.index("extra.caller.county.guess") < ids.index("extra.national.people.index")
    assert ids.index("route.T7") < ids.index("route.T1")        # county ROD before statewide eCourts
    assert wake.step("route.T1").procedure.startswith("NC_ECOURTS_ESTATE_SMART_SEARCH")
    # a county with a verified parcel tier: the tier outranks a generic source
    berrien = plan_ladder("parcel.owner_identity", state="MI", county_fips="26021",
                          extra_free_sources=[{**generic, "facts": ["parcel.owner_identity"]}])
    assert [s.step_id for s in berrien.steps] == ["parcel.county_rest.26021", "extra.national.people.index"]
    # a county without canonical knowledge has no county step to promote
    stjoe = plan_ladder("parcel.owner_identity", state="IN", county_fips="18141")
    assert [s.step_id for s in stjoe.steps] == ["parcel.local_cache"]
    # a caller cannot drop a canonical step by re-declaring it
    again = plan_ladder("parcel.owner_identity", state="MI", county_fips="26021",
                        extra_free_sources=[{"source_id": "x", "facts": ["contact.phone"]}])
    assert [s.step_id for s in again.steps] == ["parcel.county_rest.26021"]


def test_5b_the_runner_attempts_the_jurisdiction_route_first(ledger):
    order = []
    job = open_job(ledger, tenant_id=TENANT, fact="parcel.owner_identity", target={"target_key": "parcel:26021:Y",
                   "apn": "Y"}, state="MI", county_fips="26021", requested_by={"kind": "court"},
                   extra_free_sources=[{"source_id": "national.generic", "facts": ["parcel.owner_identity"],
                                        "scope": "NATIONAL", "mode": "AUTOMATED"}])

    def ex(name):
        def _run(step, state):
            order.append(name)
            return AttemptResult("NO_HIT", evidence_not_found=not_found(name))
        return _run

    run_free_ladder(ledger, job.job_id, {"county_rest": ex("county"), "national.generic": ex("generic")},
                    worker_id="w")
    assert order == ["county", "generic"]


# ---------------------------------------------------------------------------
# 6. provenance survives normalization
# ---------------------------------------------------------------------------

def test_6_three_clocks_and_freshness_survive_the_ledger(ledger):
    job = berrien_owner_job(ledger)
    record = {"owner": "DOE JOHN", "list_date": "2026-08-01", "updated_at": "2026-09-15T00:00:00Z"}
    observed, effective = dated_from_record(record, observed_key=None, effective_key="list_date")
    ev = make_evidence(fact="parcel.owner_identity", source_id="nc.wake.taxdelinquent", value={"owner": "DOE JOHN"},
                       retrieved_at="2026-09-15T12:00:00Z", confidence="HIGH", locator="https://x/list.csv#row=9",
                       raw_payload=record, observed_at=observed, source_effective_date=effective,
                       today=date(2026, 9, 15))
    assert (ev.source_effective_date, ev.source_as_of, ev.as_of_basis) == ("2026-08-01", "2026-08-01",
                                                                           "SOURCE_EFFECTIVE_DATE")
    assert ev.freshness_state == "FRESH" and ev.staleness_days == 45 and ev.retrieved_at == "2026-09-15T12:00:00Z"
    now = utc_now_iso()
    record_attempt(ledger, job.job_id, step_id="parcel.county_rest.26021", outcome="HIT", started_at=now,
                   finished_at=now, worker=WORKER, evidence_found=[ev])
    replayed = FreeFirstLedger(ledger.path).job_state(job.job_id).attempts[-1]["evidence_found"][0]
    assert replayed == ev.to_dict()
    # a bookkeeping timestamp can never date a fact
    with pytest.raises(ProvenanceRefused):
        dated_from_record(record, observed_key="updated_at", effective_key=None)
    with pytest.raises(ProvenanceRefused):
        dated_from_record(record, observed_key=None, effective_key="ingested_at")
    # retrieval time is not the fact's date: without a source date or a live query, freshness is UNKNOWN
    undated = make_evidence(fact="contact.mailing_address", source_id="s", value="1 MAIN", confidence="MEDIUM",
                            retrieved_at="2026-09-15T12:00:00Z", locator="l", raw_payload=None)
    assert (undated.source_as_of, undated.as_of_basis, undated.freshness_state) == (None, "UNKNOWN", "UNKNOWN")
    live = make_evidence(fact="contact.mailing_address", source_id="county_rest:26021", value="1 MAIN",
                         confidence="HIGH", retrieved_at="2026-09-15T12:00:00Z", locator="l", raw_payload=None,
                         live_query=True, today=date(2026, 9, 15))
    assert (live.as_of_basis, live.observed_at, live.freshness_state) == ("LIVE_QUERY_OBSERVATION",
                                                                          "2026-09-15T12:00:00Z", "FRESH")


def test_6b_an_attempt_must_document_what_it_did_not_find(ledger):
    job = berrien_owner_job(ledger)
    now = utc_now_iso()
    with pytest.raises(AttemptRefused):
        record_attempt(ledger, job.job_id, step_id="parcel.county_rest.26021", outcome="NO_HIT", started_at=now,
                       finished_at=now, worker=WORKER)
    with pytest.raises(AttemptRefused):
        record_attempt(ledger, job.job_id, step_id="parcel.county_rest.26021", outcome="HIT", started_at=now,
                       finished_at=now, worker=WORKER)
    with pytest.raises(AttemptRefused):     # a MANUAL step is a human's to record
        wake = open_job(ledger, tenant_id=TENANT, fact="contact.phone", target={"target_key": "p"}, state="NC",
                        county_fips="37183", requested_by={"kind": "court"})
        record_attempt(ledger, wake.job_id, step_id="route.T10", outcome="NO_HIT", started_at=now, finished_at=now,
                       worker=WORKER, evidence_not_found=not_found())


# ---------------------------------------------------------------------------
# 7. downstream workers cannot bypass the gate (binding cases; call sites in the next court)
# ---------------------------------------------------------------------------

def test_7_no_job_no_other_target_and_no_partial_batch(ledger, court_authority, monkeypatch):
    monkeypatch.setenv(G.ENV_LEDGER, str(ledger.path))
    with pytest.raises(G.PaidCallBlocked) as unbound:
        G.clear_paid_call("tracerfy", items=[("contact.phone", "lead:1")], est_cost_usd=0.02, caller="worker")
    assert unbound.value.reasons == ["NO_FREE_FIRST_JOB"]
    assert [e["body"]["reasons"] for e in ledger.entries("ffj_unbound")] == [["NO_FREE_FIRST_JOB"]]
    a = berrien_owner_job(ledger, "A")
    b = berrien_owner_job(ledger, "B")
    exhaust_automated(ledger, a.job_id)
    request_spend_authorization(ledger, a.job_id, providers=["reportall"], max_total_usd=1, max_per_call_usd=0.5,
                                requested_by="founder:court")
    with G.bound_job(ledger, a.job_id):
        with pytest.raises(G.PaidCallBlocked) as other:          # bound to A, paying for B
            G.clear_paid_call("reportall", items=[("parcel.owner_identity", b.target_key)], est_cost_usd=0.1,
                              caller="worker")
        assert other.value.reasons == [f"TARGET_NOT_BOUND:{b.target_key}"]
    with G.bound_jobs(ledger, [a.job_id, b.job_id]):
        with pytest.raises(G.PaidCallBlocked) as batch:          # A is cleared, B is not: nothing is bought
            G.clear_paid_call("reportall", items=[("parcel.owner_identity", a.target_key),
                                                  ("parcel.owner_identity", b.target_key)],
                              est_cost_usd=0.2, caller="worker")
        assert any(r.startswith(f"{b.target_key}:FREE_PATH_OPEN") for r in batch.value.reasons)
    assert not ledger.job_state(a.job_id).settlements


def test_7b_a_worker_subprocess_inherits_the_gate_through_its_env(ledger, court_authority):
    job = berrien_owner_job(ledger)
    code = ("from app.free_first import guard as G\n"
            "try:\n"
            "    G.clear_paid_call('reportall', items=[('parcel.owner_identity', %r)], est_cost_usd=0.1, caller='sub')\n"
            "    print('ALLOWED')\n"
            "except G.PaidCallBlocked as e:\n"
            "    print('BLOCKED', '|'.join(e.reasons))\n") % job.target_key
    env = {**os.environ, **G.binding_env(ledger, [job.job_id]), "PYTHONPATH": str(REPO)}
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("BLOCKED") and "FREE_PATH_OPEN" in out.stdout and "SPEND_UNAUTHORIZED" in out.stdout
    decision = ledger.job_state(job.job_id).paid_decisions[-1]
    assert decision["binding"] == "env" and decision["caller"] == "sub" and decision["allow"] is False

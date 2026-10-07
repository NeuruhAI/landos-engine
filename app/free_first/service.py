"""Free-first research as one engine service: one fact about one target, on the engine's own
ladder, executors and paid gate.

Two callers, one implementation:
  * in-process, by a runtime that binds this checkout (the lead factory's research stage);
  * over loopback, by POST /v1/free-first/research -- a worker whose receipts must stay
    127.0.0.1-only reaches county GIS, the DeedSonar spine and the engine cache through this
    organ, never directly.

Caller-supplied evidence may answer only the caller's OWN extra steps (sources it added to
the ladder). It can never close a canonical step: canonical routes are answered by the
engine's executors or stay UNWIRED, so no caller can talk the gate into paying.

The paid probe asks the gate exactly as a real paid call would -- ReportAll through its
real HTTP choke point (ReportAllOwnerNormalizer._search), other providers through the guard
entry their call sites use -- and records the decision on the ledger. A cleared probe that
reaches a vendor would be a gate defect; the probe never intends to spend.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from . import guard as G
from .jobs import open_job
from .knowledge import paid_providers_for
from .ledger import FreeFirstLedger
from .provenance import make_evidence
from .runner import AttemptResult, Executor, run_free_ladder

RESEARCH_VERSION = "landos.free_first.research.v1"


def supplied_evidence_executor(supplied: Mapping[str, Mapping[str, Any]]) -> Executor:
    """Answer a caller's extra step from evidence the caller already holds (custody reads).

    ``supplied[step_source_id]`` = {"outcome": HIT|NO_HIT|NOT_APPLICABLE, "value": ...,
    "retrieved_at": ..., "source_effective_date": ..., "confidence": ..., "locator": ...,
    "evidence_not_found": {...}}."""
    def _run(step: Mapping[str, Any], state) -> AttemptResult:
        item = supplied.get(step["source_id"])
        if item is None:
            return AttemptResult("UNWIRED", error={"class": "UNWIRED", "permanent": False,
                                                   "message": f"no supplied evidence for {step['source_id']}"})
        outcome = item.get("outcome")
        if outcome == "HIT":
            ev = make_evidence(fact=state.fact, source_id=step["source_id"], value=item.get("value"),
                               retrieved_at=item["retrieved_at"], confidence=item.get("confidence") or "MEDIUM",
                               locator=str(item.get("locator") or step["source_id"]), raw_payload=item.get("value"),
                               observed_at=item.get("observed_at"),
                               source_effective_date=item.get("source_effective_date"),
                               live_query=bool(item.get("live_query")), source_record_id=item.get("source_record_id"))
            return AttemptResult("HIT", evidence_found=[ev])
        if outcome in ("NO_HIT", "NOT_APPLICABLE"):
            return AttemptResult(outcome, evidence_not_found=dict(item.get("evidence_not_found") or
                                                                   {"source": step["source_id"]}))
        return AttemptResult("UNWIRED", error={"class": "UNWIRED", "permanent": False,
                                               "message": f"supplied outcome {outcome!r} is not answerable"})
    return _run


def probe_paid_path(ledger: FreeFirstLedger, job_id: str, *, fact: str, target: Mapping[str, Any],
                    county_fips: str, caller: str) -> List[Dict[str, Any]]:
    out = []
    with G.bound_job(ledger, job_id):
        for provider in paid_providers_for(fact):
            name = provider.provider
            try:
                if name == "reportall" and fact == "parcel.owner_identity":
                    from app.normalizers.reportall_owner import COUNTY_CONFIG, ReportAllOwnerNormalizer
                    if county_fips not in COUNTY_CONFIG:
                        out.append({"provider": name, "via": "catalog", "allowed": False,
                                    "reasons": [f"NOT_CONFIGURED_FOR_COUNTY:{county_fips}"]})
                        continue
                    ReportAllOwnerNormalizer("free-first-probe", county_fips,
                                             client_key=os.environ.get("REPORTALL_CLIENT_KEY"))._search(
                        {"parcel_id": target.get("apn"), "rpp": 5, "page": 1}, f"apn={target.get('apn')}")
                    out.append({"provider": name, "via": "call_site", "allowed": True,
                                "defect": "GATE_CLEARED_A_PROBE"})
                    continue
                G.clear_paid_call(name, items=[(fact, target["target_key"])], est_cost_usd=None, caller=caller)
                out.append({"provider": name, "via": "guard.clear_paid_call", "allowed": True,
                            "defect": "GATE_CLEARED_A_PROBE"})
            except G.PaidCallBlocked as blocked:
                out.append({"provider": name, "via": "call_site" if name == "reportall" else "guard.clear_paid_call",
                            "allowed": False, "reasons": blocked.reasons, "decision_shas": blocked.decision_shas})
            except RuntimeError as exc:           # cleared, then a vendor error: never expected in a probe
                out.append({"provider": name, "via": "call_site", "allowed": True, "error": str(exc)[:200],
                            "defect": "GATE_CLEARED_A_PROBE"})
    return out


def research(*, ledger: FreeFirstLedger, tenant_id: str, fact: str, target: Mapping[str, Any], state: str,
             county_fips: str, requested_by: Mapping[str, Any], worker_id: str,
             executors: Optional[Mapping[str, Executor]] = None,
             extra_free_sources: Iterable[Mapping[str, Any]] = (),
             supplied_evidence: Optional[Mapping[str, Mapping[str, Any]]] = None,
             extra_executors: Optional[Mapping[str, Executor]] = None,
             parent_receipt: Optional[Mapping[str, Any]] = None, probe_paid: bool = True) -> Dict[str, Any]:
    """``extra_executors`` (in-process callers only) may answer the caller's own EXTRA steps
    and nothing else; ``supplied_evidence`` is the same thing carried over the wire."""
    job = open_job(ledger, tenant_id=tenant_id, fact=fact, target=target, state=state, county_fips=county_fips,
                   requested_by=requested_by, parent_receipt=parent_receipt,
                   extra_free_sources=list(extra_free_sources))
    extras = [s for s in job.steps if s["source_kind"] == "EXTRA"]
    run_executors: Dict[str, Executor] = dict(executors or {})
    if supplied_evidence:
        answer = supplied_evidence_executor(supplied_evidence)
        run_executors.update({s["step_id"]: answer for s in extras})
    for step in extras:
        fn = (extra_executors or {}).get(step["step_id"]) or (extra_executors or {}).get(step["source_id"])
        if fn is not None:
            run_executors[step["step_id"]] = fn
    ladder = run_free_ladder(ledger, job.job_id, run_executors, worker_id=worker_id, parent_receipt=parent_receipt)
    paid = probe_paid_path(ledger, job.job_id, fact=fact, target=target, county_fips=county_fips,
                           caller=f"{worker_id}:probe") if probe_paid else []
    state_after = ledger.job_state(job.job_id)
    verdict = ladder.exhaustion["verdict"]
    return {
        "schema_version": RESEARCH_VERSION, "fact": fact, "job_id": job.job_id, "target_key": target["target_key"],
        "ladder": [{"step_id": s["step_id"], "scope": s["scope"], "mode": s["mode"]} for s in state_after.steps],
        "attempted": ladder.attempted, "skipped": ladder.skipped, "human_steps": ladder.human_steps,
        "exhaustion": {k: ladder.exhaustion[k] for k in ("verdict", "machine_exhausted", "resolved_by",
                                                          "open_automated", "pending_human")},
        # the business state: free evidence exhausted with a human route left is HUMAN_REQUIRED, not a failure
        "research_state": {"RESOLVED_FREE": "RESOLVED_FREE", "HUMAN_STEP_PENDING": "HUMAN_REQUIRED",
                           "EXHAUSTED": "FREE_EXHAUSTED", "FREE_PATH_OPEN": "FREE_PATH_OPEN"}[verdict],
        "resolved_evidence": [ev for a in state_after.attempts if a["outcome"] == "HIT" for ev in a["evidence_found"]],
        "paid": paid, "spent_usd": state_after.spent_usd(),
        "ledger": {"path": str(ledger.path), "tail_sha": ledger.tip()["tail_sha"]},
    }


def default_ledger_path() -> str:
    return os.path.expanduser(os.environ.get(G.ENV_LEDGER) or "~/.neuruh/landos-engine/free-first/ledger.jsonl")

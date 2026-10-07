"""Free-first job lifecycle: open (idempotent), record attempts and human outcomes,
record a spend authorization, close. Every write is one ledger entry.

A job is one enrichment question (fact) about one target in one jurisdiction. The
ladder is planned by the engine at open time (knowledge.plan_ladder) and can only
grow: re-opening the job after the engine learns a new free source appends the new
steps (LADDER_EXTENDED), so a source learned tomorrow is owed an attempt before any
paid call, even for a job opened today.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .authority import AuthorityRefused, SpendAuthority, SpendRequest, configured_authority
from .knowledge import AUTOMATED, MANUAL, plan_ladder
from .ledger import FreeFirstLedger, JobState, job_id_for
from .policy import HUMAN_OUTCOMES, OUTCOMES, exhaustion
from .provenance import Evidence, utc_now_iso


class AttemptRefused(ValueError):
    """The attempt record does not document what the policy needs to trust it."""


def open_job(ledger: FreeFirstLedger, *, tenant_id: str, fact: str, target: Mapping[str, Any], state: str,
             county_fips: str, requested_by: Mapping[str, Any], parent_receipt: Optional[Mapping[str, Any]] = None,
             extra_free_sources: Iterable[Mapping[str, Any]] = ()) -> JobState:
    if not target.get("target_key"):
        raise ValueError("target.target_key is required")
    ladder = plan_ladder(fact, state=state, county_fips=county_fips, extra_free_sources=list(extra_free_sources))
    job_id = job_id_for(tenant_id=tenant_id, fact=fact, target_key=target["target_key"],
                        county_fips=ladder.county_fips)
    if ledger.has_job(job_id):
        current = ledger.job_state(job_id)
        known = {s["step_id"] for s in current.steps}
        added = [s.to_dict() for s in ladder.steps if s.step_id not in known]
        if added:
            ledger.append("LADDER_EXTENDED", job_id, {"added_steps": added, "ladder_digest": ladder.digest,
                                                      "requested_by": dict(requested_by)})
            return ledger.job_state(job_id)
        return current
    ledger.append("JOB_OPENED", job_id, {
        "tenant_id": tenant_id, "fact": fact, "target": dict(target),
        "jurisdiction": {"state": ladder.state, "county_fips": ladder.county_fips},
        "ladder": ladder.to_dict(), "requested_by": dict(requested_by),
        "parent_receipt": dict(parent_receipt) if parent_receipt else None,
    })
    return ledger.job_state(job_id)


def _evidence_rows(items: Sequence[Any]) -> List[Dict[str, Any]]:
    out = []
    for ev in items:
        row = ev.to_dict() if isinstance(ev, Evidence) else dict(ev)
        for key in ("evidence_id", "fact", "source_id", "retrieved_at", "freshness_state", "confidence",
                    "content_sha256"):
            if not row.get(key):
                raise AttemptRefused(f"evidence is missing {key}")
        out.append(row)
    return out


def record_attempt(ledger: FreeFirstLedger, job_id: str, *, step_id: str, outcome: str, started_at: str,
                   finished_at: str, worker: Mapping[str, Any], evidence_found: Sequence[Any] = (),
                   evidence_not_found: Optional[Mapping[str, Any]] = None, error: Optional[Mapping[str, Any]] = None,
                   parent_receipt: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """One AUTOMATED free-source attempt. Free steps never cost money: cost is 0 by rule."""
    state = ledger.job_state(job_id)
    step = next((s for s in state.steps if s["step_id"] == step_id), None)
    if step is None:
        raise AttemptRefused(f"{step_id} is not on job {job_id}'s ladder")
    if step["mode"] != AUTOMATED:
        raise AttemptRefused(f"{step_id} is MANUAL: record a human outcome, not a machine attempt")
    if outcome not in OUTCOMES:
        raise AttemptRefused(f"outcome {outcome!r} not in {sorted(OUTCOMES)}")
    found = _evidence_rows(evidence_found)
    if outcome == "HIT" and not found:
        raise AttemptRefused("a HIT must carry the evidence it found")
    if outcome in ("NO_HIT", "NOT_APPLICABLE", "BLOCKED") and not evidence_not_found:
        raise AttemptRefused(f"{outcome} must document what was searched and not found (evidence_not_found)")
    if outcome == "ERROR" and not (error and error.get("class")):
        raise AttemptRefused("an ERROR must carry error.class / error.message / error.permanent")
    if not worker.get("worker_id"):
        raise AttemptRefused("worker.worker_id is required (who ran it)")
    body = {
        "step_id": step_id, "source_id": step["source_id"], "source_kind": step["source_kind"],
        "access": "FREE", "outcome": outcome, "started_at": started_at, "finished_at": finished_at,
        "jurisdiction": state.opened["jurisdiction"], "target": state.opened["target"], "fact": state.fact,
        "evidence_found": found, "evidence_not_found": dict(evidence_not_found or {}),
        "error": dict(error or {}), "cost_usd": 0.0, "worker": dict(worker),
        "parent_receipt": dict(parent_receipt or {"kind": "free_first.job", "ref": job_id,
                                                  "sha256": state.opened_entry_sha}),
    }
    projected = JobState(**{**state.__dict__, "attempts": state.attempts + [{**body, "at": finished_at}]})
    body["exhaustion_after"] = exhaustion(projected).verdict
    return ledger.append("ATTEMPT", job_id, body, at=finished_at)


def record_human_outcome(ledger: FreeFirstLedger, job_id: str, *, step_id: str, outcome: str, actor: str,
                         evidence_found: Sequence[Any] = (), evidence_not_found: Optional[Mapping[str, Any]] = None,
                         note: str = "") -> Dict[str, Any]:
    """A person ran a MANUAL (H-12) free route and says what they found."""
    state = ledger.job_state(job_id)
    step = next((s for s in state.steps if s["step_id"] == step_id), None)
    if step is None or step["mode"] != MANUAL:
        raise AttemptRefused(f"{step_id} is not a MANUAL step on {job_id}")
    if outcome not in HUMAN_OUTCOMES:
        raise AttemptRefused(f"human outcome {outcome!r} not in {sorted(HUMAN_OUTCOMES)}")
    if not actor.strip():
        raise AttemptRefused("a human outcome names the human")
    found = _evidence_rows(evidence_found)
    if outcome == "HIT" and not found:
        raise AttemptRefused("a HIT must carry the evidence it found")
    if outcome != "HIT" and not evidence_not_found:
        raise AttemptRefused("record what was searched (evidence_not_found)")
    now = utc_now_iso()
    return ledger.append("HUMAN_OUTCOME", job_id, {
        "step_id": step_id, "source_id": step["source_id"], "outcome": outcome, "actor": actor,
        "evidence_found": found, "evidence_not_found": dict(evidence_not_found or {}), "note": note,
        "finished_at": now, "jurisdiction": state.opened["jurisdiction"], "target": state.opened["target"],
    }, at=now)


def request_spend_authorization(ledger: FreeFirstLedger, job_id: str, *, providers: Sequence[str],
                                max_total_usd: float, max_per_call_usd: Optional[float], requested_by: str,
                                waives: Optional[Mapping[str, str]] = None, reason: str = "",
                                authority: Optional[SpendAuthority] = None) -> Dict[str, Any]:
    """Ask the configured spend authority; record its (confirmed) yes. A no raises."""
    state = ledger.job_state(job_id)
    waives = dict(waives or {})
    for sid, why in waives.items():
        if sid not in {s["step_id"] for s in state.steps}:
            raise AttemptRefused(f"cannot waive {sid}: not on the ladder")
        if not str(why).strip():
            raise AttemptRefused(f"waiving {sid} needs a reason")
    authority = authority or configured_authority()
    request = SpendRequest(job_id=job_id, fact=state.fact, target_key=state.target_key, county_fips=state.county_fips,
                           providers=list(providers), max_total_usd=float(max_total_usd),
                           max_per_call_usd=max_per_call_usd, waives=waives, requested_by=requested_by,
                           exhaustion_verdict=exhaustion(state).verdict, ledger_tip=ledger.verify()["tail_sha"],
                           reason=reason)
    grant = authority.authorize(request)
    authority.confirm(grant.to_dict(), now=datetime.now(timezone.utc))
    return ledger.append("SPEND_AUTHORIZED", job_id, grant.to_dict())


def close_job(ledger: FreeFirstLedger, job_id: str, *, resolution: str, actor: str, note: str = "") -> Dict[str, Any]:
    state = ledger.job_state(job_id)
    return ledger.append("JOB_CLOSED", job_id, {"resolution": resolution, "actor": actor, "note": note,
                                                "exhaustion": exhaustion(state).verdict})


__all__ = ["AttemptRefused", "AuthorityRefused", "close_job", "open_job", "record_attempt",
           "record_human_outcome", "request_spend_authorization"]

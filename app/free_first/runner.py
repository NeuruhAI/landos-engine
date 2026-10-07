"""Run a job's free ladder: AUTOMATED steps in order, MANUAL steps handed to a human, paid never.

Retry-safe by construction: the runner replays the job from the ledger, skips every
step already RESOLVED or EXHAUSTED, re-runs only OPEN / STALE / RETRYABLE / UNWIRED
steps, and stops at the first free HIT at the evidence floor. It has no code path to
a paid provider -- a paid call is a separate act that guard.paid_call clears only on
the replayed exhaustion verdict plus a confirmed spend authorization.

Executors are plain callables keyed by step_id or source_id. A step with no executor
in this runtime is recorded UNWIRED (non-terminal): the free path is still open, so
paid stays blocked. That is the honest answer, not a skip.
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .jobs import record_attempt
from .knowledge import AUTOMATED, MANUAL
from .ledger import FreeFirstLedger, JobState
from .policy import exhaustion, step_status
from .provenance import make_evidence, utc_now_iso


@dataclass
class AttemptResult:
    outcome: str
    evidence_found: Sequence[Any] = ()
    evidence_not_found: Optional[Mapping[str, Any]] = None
    error: Optional[Mapping[str, Any]] = None


Executor = Callable[[Mapping[str, Any], JobState], AttemptResult]


def worker_identity(worker_id: str) -> Dict[str, Any]:
    return {"worker_id": worker_id, "pid": os.getpid(), "host": socket.gethostname()}


@dataclass
class LadderRun:
    job_id: str
    attempted: List[Dict[str, Any]] = field(default_factory=list)
    skipped: List[Dict[str, Any]] = field(default_factory=list)
    human_steps: List[Dict[str, Any]] = field(default_factory=list)
    exhaustion: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"job_id": self.job_id, "attempted": self.attempted, "skipped": self.skipped,
                "human_steps": self.human_steps, "exhaustion": self.exhaustion}


def run_free_ladder(ledger: FreeFirstLedger, job_id: str, executors: Mapping[str, Executor], *,
                    worker_id: str, min_confidence: str = "MEDIUM", max_attempts: Optional[int] = None,
                    parent_receipt: Optional[Mapping[str, Any]] = None) -> LadderRun:
    run = LadderRun(job_id)
    worker = worker_identity(worker_id)
    tried = 0
    for step in list(ledger.job_state(job_id).steps):
        state = ledger.job_state(job_id)
        now = datetime.now(timezone.utc)
        status = step_status(state, step, now, min_confidence=min_confidence)
        if any(s.status == "RESOLVED" for s in exhaustion(state, now, min_confidence=min_confidence).steps):
            break
        if step["mode"] == MANUAL:
            if status.status == "HUMAN_PENDING":
                run.human_steps.append({"step_id": step["step_id"], "source_id": step["source_id"],
                                        "halt": step.get("halt") or "H-12", "human_step": step.get("human_step"),
                                        "procedure": step.get("procedure"), "provenance_url": step.get("provenance_url")})
            continue
        if status.status in ("RESOLVED", "EXHAUSTED"):
            run.skipped.append({"step_id": step["step_id"], "status": status.status, "detail": status.detail})
            continue
        if max_attempts is not None and tried >= max_attempts:
            break
        executor = executors.get(step["step_id"]) or executors.get(step["source_id"])
        started = utc_now_iso()
        if executor is None:
            result = AttemptResult("UNWIRED", error={"class": "UNWIRED", "permanent": False,
                                                     "message": f"no executor for {step['source_id']} in {worker_id}"})
        else:
            try:
                result = executor(step, state)
            except Exception as exc:  # noqa: BLE001 -- an executor failure is an ERROR attempt, never silent
                result = AttemptResult("ERROR", error={"class": type(exc).__name__, "message": str(exc)[:300],
                                                       "permanent": False})
        entry = record_attempt(ledger, job_id, step_id=step["step_id"], outcome=result.outcome, started_at=started,
                               finished_at=utc_now_iso(), worker=worker, evidence_found=result.evidence_found,
                               evidence_not_found=result.evidence_not_found, error=result.error,
                               parent_receipt=parent_receipt)
        tried += 1
        run.attempted.append({"step_id": step["step_id"], "outcome": result.outcome, "entry_sha": entry["entry_sha"],
                              "exhaustion_after": entry["body"]["exhaustion_after"]})
    run.exhaustion = exhaustion(ledger.job_state(job_id), min_confidence=min_confidence).to_dict()
    return run


# ---------------------------------------------------------------------------
# executors over the engine's own free parcel tiers (county REST / FetchGIS / Colligo)
# ---------------------------------------------------------------------------

_NOT_APPLICABLE_MARKERS = ("no county rest endpoint", "unverified", "not configured", "no endpoint",
                           "no fetchgis", "no colligo")


def parcel_tier_executor(tenant_id: str, *, registry: Optional[Mapping[str, Any]] = None,
                         local_cache_conn: Any = None) -> Executor:
    """A live, free, per-parcel query against the county's own published GIS.

    target: {apn} (preferred) or {owner_name}. local_cache reads the engine's own
    landos.owner_leads through LocalCacheProvider on ``local_cache_conn``; without a
    connection it is UNWIRED (the cache may already hold the answer, so a runtime that
    cannot read it must not pay for it). A cache row is dated by nothing but its row
    bookkeeping, so its evidence carries no as-of: freshness UNKNOWN, never faked."""
    from app.providers.base import ProviderUnavailable
    # The free tiers only: app.providers.chain would import local_cache -> app.config -> the
    # engine database, which a free-tier executor never needs.
    from app.providers.colligo import ColligoProvider
    from app.providers.county_rest import CountyRESTProvider
    from app.providers.fetchgis import FetchGISProvider
    reg = registry or {"county_rest": CountyRESTProvider, "fetchgis": FetchGISProvider, "colligo": ColligoProvider}

    def _run(step: Mapping[str, Any], state: JobState) -> AttemptResult:
        tier = step["source_id"]
        target, fips = state.opened["target"], state.county_fips
        if tier == "local_cache":
            if local_cache_conn is None:
                return AttemptResult("UNWIRED", error={"class": "UNWIRED", "permanent": False,
                                                       "message": "local_cache needs the engine database connection"})
            from app.providers.local_cache import LocalCacheProvider
            provider = LocalCacheProvider(tenant_id, conn=local_cache_conn)
        else:
            provider = reg[tier](tenant_id)
        apn, owner = target.get("apn"), target.get("owner_name")
        op, arg = ("lookup_by_apn", apn) if apn else ("search_by_owner", owner)
        if not arg:
            return AttemptResult("NOT_APPLICABLE", evidence_not_found={"reason": "target has neither apn nor owner_name"})
        query = {"op": op, "county_fips": fips, "value": arg, "endpoint": step.get("provenance_url")}
        retrieved_at = utc_now_iso()
        try:
            rows = getattr(provider, op)(fips, arg)
        except ProviderUnavailable as exc:
            msg = str(exc)
            if any(m in msg.lower() for m in _NOT_APPLICABLE_MARKERS):
                return AttemptResult("NOT_APPLICABLE", evidence_not_found={**query, "reason": msg[:300]})
            return AttemptResult("ERROR", error={"class": "ProviderUnavailable", "message": msg[:300],
                                                 "permanent": False})
        if not rows:
            return AttemptResult("NO_HIT", evidence_not_found={**query, "result_count": 0,
                                                               "searched_at": retrieved_at})
        exact = [r for r in rows if apn and str(r.get("parcel_apn") or "") == str(apn)]
        best = (exact or rows)[0]
        confidence = "HIGH" if exact else ("MEDIUM" if len(rows) == 1 else "LOW")
        if state.fact == "parcel.owner_identity":
            value = {"owner_name": best.get("owner_name"), "owner_name2": best.get("owner_name2"),
                     "parcel_apn": best.get("parcel_apn")}
            ok = bool(best.get("owner_name"))
        else:
            value = {k: best.get(k) for k in ("owner_mailing_address", "owner_mailing_city",
                                              "owner_mailing_state", "owner_mailing_zip")}
            ok = bool(value["owner_mailing_address"])
        if not ok:
            return AttemptResult("NO_HIT", evidence_not_found={**query, "result_count": len(rows),
                                                               "reason": "parcel found; the layer carries no value for this fact"})
        raw = [{k: v for k, v in r.items() if k != "raw_meta"} for r in rows]
        ev = make_evidence(fact=state.fact, source_id=f"{tier}:{fips}", value=value, retrieved_at=retrieved_at,
                           confidence=confidence, locator=f"{best.get('provenance')}?{op}={arg}", raw_payload=raw,
                           live_query=tier != "local_cache", source_record_id=best.get("parcel_apn"))
        return AttemptResult("HIT", evidence_found=[ev])

    return _run

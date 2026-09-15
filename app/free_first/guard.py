"""The paid-call guard: the one door every paid vendor call in this engine walks through.

Every function that sends a billable request (the call sites catalogued in
knowledge.PAID_PROVIDERS, enforced by tests/test_free_first_call_sites.py) wraps it:

    with paid_call("tracerfy", items=[("contact.phone", f"lead:{lead_id}")], est_cost_usd=0.02):
        ...urlopen...

Clearance is decided from the free-first LEDGER, not from the caller: the job the
process is bound to (a context binding, or LANDOS_FREE_FIRST_LEDGER +
LANDOS_FREE_FIRST_JOB for a worker subprocess) is replayed, its exhaustion is
recomputed, its spend authorization is re-confirmed with the configured authority,
and the decision -- allow or block, with every reason -- is appended to the ledger
BEFORE any byte leaves the process. A call with no bound job is blocked
(NO_FREE_FIRST_JOB) and, when a ledger is configured, recorded as well.

PaidCallBlocked is a RuntimeError so every existing degrade path treats a blocked
tier the way it treats an unavailable one (the parcel chain moves on; scripts stop
loudly), and never as a vendor answer.
"""
from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from .authority import AuthorityRefused, configured_authority
from .ledger import FreeFirstLedger, UnknownJob
from .policy import decide_paid

UNBOUND_JOB = "ffj_unbound"
ENV_LEDGER, ENV_JOB, ENV_JOBS = "LANDOS_FREE_FIRST_LEDGER", "LANDOS_FREE_FIRST_JOB", "LANDOS_FREE_FIRST_JOBS"

Item = Tuple[str, str]          # (fact, target_key)


class PaidCallBlocked(RuntimeError):
    def __init__(self, provider: str, reasons: Sequence[str], *, job_ids: Sequence[str] = (),
                 decision_shas: Sequence[str] = ()):
        super().__init__(f"FREE-FIRST blocked paid provider {provider}: {'; '.join(reasons)}")
        self.provider = provider
        self.reasons = list(reasons)
        self.job_ids = list(job_ids)
        self.decision_shas = list(decision_shas)


@dataclass(frozen=True)
class Binding:
    ledger_path: str
    jobs: Mapping[str, str]       # target_key -> job_id
    source: str                   # "context" | "env"


_BINDING: contextvars.ContextVar[Optional[Binding]] = contextvars.ContextVar("landos_free_first_binding", default=None)


@contextmanager
def bound_jobs(ledger: FreeFirstLedger, job_ids: Sequence[str]) -> Iterator[Binding]:
    """Bind this context to one or more free-first jobs (one per target)."""
    jobs = {}
    for jid in job_ids:
        state = ledger.job_state(jid)
        jobs[state.target_key] = jid
    binding = Binding(str(ledger.path), jobs, "context")
    token = _BINDING.set(binding)
    try:
        yield binding
    finally:
        _BINDING.reset(token)


def bound_job(ledger: FreeFirstLedger, job_id: str):
    return bound_jobs(ledger, [job_id])


def binding_env(ledger: FreeFirstLedger, job_ids: Sequence[str]) -> Dict[str, str]:
    """Env for a worker subprocess that must carry the binding across a process boundary."""
    return {ENV_LEDGER: str(ledger.path), ENV_JOBS: ",".join(job_ids)}


def current_binding() -> Optional[Binding]:
    binding = _BINDING.get()
    if binding is not None:
        return binding
    path = os.environ.get(ENV_LEDGER)
    ids = [j for j in (os.environ.get(ENV_JOBS) or os.environ.get(ENV_JOB) or "").split(",") if j.strip()]
    if not path or not ids:
        return None
    ledger = FreeFirstLedger(path)
    jobs = {}
    for jid in ids:
        try:
            jobs[ledger.job_state(jid.strip()).target_key] = jid.strip()
        except UnknownJob:
            continue
    return Binding(path, jobs, "env")


@dataclass
class Clearance:
    ledger: FreeFirstLedger
    provider: str
    job_ids: List[str]
    authorization_ids: List[Optional[str]]
    decision_shas: List[str]
    est_cost_usd: Optional[float]
    charges_usd: List[Optional[float]] = field(default_factory=list)   # per job, from its decision
    settled: bool = field(default=False)

    def settle(self, *, cost_usd: Optional[float], outcome: str, evidence_found: Sequence[Mapping[str, Any]] = (),
               note: str = "") -> None:
        """Record what the vendor call cost and produced, split across the batch's jobs."""
        if self.settled:
            return
        n = max(1, len(self.job_ids))
        charges = self.charges_usd or [None] * n
        for jid, auth_id, dsha, charge in zip(self.job_ids, self.authorization_ids, self.decision_shas, charges):
            # a known vendor cost is split evenly; an unknown one charges what the decision budgeted
            share = round(float(cost_usd) / n, 6) if cost_usd is not None else charge
            self.ledger.append("PAID_SETTLED", jid, {
                "provider": self.provider, "authorization_id": auth_id, "decision_sha": dsha,
                "cost_usd": share, "cost_known": cost_usd is not None,
                "outcome": outcome, "evidence_found": [dict(e) for e in evidence_found], "note": note,
                "batch_size": n})
        self.settled = True


def _record_unbound(provider: str, items: Sequence[Item], caller: str, reasons: List[str]) -> Optional[str]:
    path = os.environ.get(ENV_LEDGER)
    if not path:
        return None
    entry = FreeFirstLedger(path).append("PAID_DECISION", UNBOUND_JOB, {
        "allow": False, "provider": provider, "reasons": reasons, "caller": caller,
        "items": [list(i) for i in items], "authority": None, "binding": None})
    return entry["entry_sha"]


def clear_paid_call(provider: str, *, items: Sequence[Item], est_cost_usd: Optional[float],
                    caller: str) -> Clearance:
    """Allow the call, or raise PaidCallBlocked. Either way the decision is on the ledger first."""
    items = [(str(f), str(t)) for f, t in items]
    if not items:
        raise PaidCallBlocked(provider, ["NO_TARGETS_DECLARED"])
    binding = current_binding()
    if binding is None:
        reasons = ["NO_FREE_FIRST_JOB"]
        sha = _record_unbound(provider, items, caller, reasons)
        raise PaidCallBlocked(provider, reasons, decision_shas=[sha] if sha else [])
    ledger = FreeFirstLedger(binding.ledger_path)
    authority = configured_authority()
    now = datetime.now(timezone.utc)
    per_item = None if est_cost_usd is None else round(float(est_cost_usd) / len(items), 6)
    decisions: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    blocked: List[str] = []
    for fact, target_key in items:
        job_id = binding.jobs.get(target_key)
        if job_id is None:
            reasons = [f"TARGET_NOT_BOUND:{target_key}"]
            sha = _record_unbound(provider, [(fact, target_key)], caller, reasons)
            blocked.extend(reasons)
            decisions.append(("", {"allow": False, "reasons": reasons}, {"entry_sha": sha or ""}))
            continue
        state = ledger.job_state(job_id)
        extra: List[str] = []
        if state.fact != fact:
            extra.append(f"FACT_MISMATCH:{fact}!={state.fact}")
        auth, auth_error = None, None
        candidates = [a for a in state.authorizations if provider in a.get("providers", [])]
        if candidates:
            try:
                authority.confirm(candidates[-1], now=now)
                auth = candidates[-1]
            except AuthorityRefused as exc:
                auth_error = exc.reason
        else:
            auth_error = f"NONE_RECORDED:{authority.name}"
        decision = decide_paid(state, provider, est_cost_usd=per_item, authorization=auth,
                               authorization_error=auth_error, now=now)
        body = decision.to_dict()
        body["reasons"] = extra + body["reasons"]
        body["allow"] = not body["reasons"]
        body.update({"caller": caller, "authority": authority.name, "binding": binding.source,
                     "items_in_call": len(items), "target_key": target_key})
        entry = ledger.append("PAID_DECISION", job_id, body)
        if not body["allow"]:
            blocked.extend(f"{target_key}:{r}" for r in body["reasons"])
        decisions.append((job_id, body, entry))
    if blocked:
        raise PaidCallBlocked(provider, blocked, job_ids=[d[0] for d in decisions if d[0]],
                              decision_shas=[d[2].get("entry_sha", "") for d in decisions])
    return Clearance(ledger, provider, [d[0] for d in decisions], [d[1].get("authorization_id") for d in decisions],
                     [d[2]["entry_sha"] for d in decisions], est_cost_usd,
                     charges_usd=[d[1].get("effective_cost_usd") for d in decisions])


@contextmanager
def paid_call(provider: str, *, items: Sequence[Item], est_cost_usd: Optional[float],
              caller: str) -> Iterator[Clearance]:
    """Clear, run the vendor call, settle. An exception after clearance settles at the
    estimate (a vendor may have charged for a request whose answer we never read)."""
    clearance = clear_paid_call(provider, items=items, est_cost_usd=est_cost_usd, caller=caller)
    try:
        yield clearance
    except BaseException as exc:
        clearance.settle(cost_usd=None, outcome="ERROR_AFTER_CLEARANCE", note=type(exc).__name__)
        raise
    clearance.settle(cost_usd=est_cost_usd, outcome="COMPLETED")

"""FREE-FIRST policy: pure functions over a replayed job.

Exhaustion is the canonical FR-2 rule (docs/FREE_ROUTES_REGISTER.md): the machine
has exhausted the free path only when every applicable non-halted AUTOMATED step
has a terminal receipt for the target. MANUAL (H-12) steps are free paths too: they
are exhausted only by a recorded human outcome, or waived -- by name, with a
reason -- inside a spend authorization.

A paid provider is allowed only when ALL of these hold, and every failing one is
named in the decision (never just the first):

  * the provider is a catalogued paid provider that can establish this fact
  * the fact is not already resolved by a free source
  * no applicable free step is open (or each open step is explicitly waived)
  * a spend authorization exists, is confirmed by its authority, is unexpired,
    names this provider, and has budget left for the estimated cost
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from .knowledge import AUTOMATED, MANUAL, PAID_PROVIDERS
from .ledger import JobState

#: A source that answered: found it, did not have it, does not cover the target,
#: or is closed to us (walled / terms). Each ends the step until it goes stale.
TERMINAL_OUTCOMES = frozenset({"HIT", "NO_HIT", "NOT_APPLICABLE", "BLOCKED"})
#: A source that did not answer. ERROR counts toward exhaustion only once its retry
#: budget is spent (or the error is permanent); UNWIRED (no executor in this runtime)
#: and HALTED (a human step was emitted) never do.
NON_TERMINAL_OUTCOMES = frozenset({"ERROR", "UNWIRED", "HALTED"})
OUTCOMES = TERMINAL_OUTCOMES | NON_TERMINAL_OUTCOMES
HUMAN_OUTCOMES = frozenset({"HIT", "NO_HIT", "NOT_APPLICABLE", "BLOCKED"})
MAX_ERROR_ATTEMPTS = 3
CONFIDENCE_RANK = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}

STEP_STATES = ("RESOLVED", "EXHAUSTED", "OPEN", "STALE", "RETRYABLE", "UNWIRED", "HUMAN_PENDING")
OPEN_STATES = frozenset({"OPEN", "STALE", "RETRYABLE", "UNWIRED"})
VERDICTS = ("RESOLVED_FREE", "FREE_PATH_OPEN", "HUMAN_STEP_PENDING", "EXHAUSTED")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _fresh(entry: Mapping[str, Any], ttl_days: int, now: datetime) -> bool:
    return now - parse_ts(entry.get("finished_at") or entry["at"]) <= timedelta(days=ttl_days)


def _hit_at_floor(entry: Mapping[str, Any], min_confidence: str) -> bool:
    found = entry.get("evidence_found") or []
    return entry["outcome"] == "HIT" and any(
        CONFIDENCE_RANK.get(ev.get("confidence", "UNKNOWN"), 0) >= CONFIDENCE_RANK[min_confidence] for ev in found)


@dataclass
class StepStatus:
    step_id: str
    mode: str
    scope: str
    status: str
    detail: str = ""
    attempts: int = 0
    last_outcome: Optional[str] = None


def step_status(state: JobState, step: Mapping[str, Any], now: datetime, *, min_confidence: str) -> StepStatus:
    ttl = int(step.get("ttl_days") or 30)
    sid, mode = step["step_id"], step["mode"]
    attempts = state.attempts_for(sid)
    base = dict(step_id=sid, mode=mode, scope=step["scope"], attempts=len(attempts),
                last_outcome=attempts[-1]["outcome"] if attempts else None)
    if mode == MANUAL:
        human = [h for h in state.human_for(sid) if _fresh(h, ttl, now)]
        if any(_hit_at_floor(h, min_confidence) for h in human):
            return StepStatus(**base, status="RESOLVED", detail="human found it")
        if human:
            return StepStatus(**base, status="EXHAUSTED", detail=f"human recorded {human[-1]['outcome']}")
        return StepStatus(**base, status="HUMAN_PENDING", detail=step.get("human_step") or "human step")
    if not attempts:
        return StepStatus(**base, status="OPEN", detail="never attempted")
    fresh = [a for a in attempts if _fresh(a, ttl, now)]
    if any(_hit_at_floor(a, min_confidence) for a in fresh):
        return StepStatus(**base, status="RESOLVED", detail="free source found it")
    terminal = [a for a in fresh if a["outcome"] in TERMINAL_OUTCOMES]
    if terminal:
        return StepStatus(**base, status="EXHAUSTED", detail=f"{terminal[-1]['outcome']} at {terminal[-1]['finished_at']}")
    errors = [a for a in fresh if a["outcome"] == "ERROR"]
    if any((a.get("error") or {}).get("permanent") for a in errors) or len(errors) >= MAX_ERROR_ATTEMPTS:
        return StepStatus(**base, status="EXHAUSTED", detail=f"error budget spent ({len(errors)} errors)")
    if errors:
        return StepStatus(**base, status="RETRYABLE", detail=f"{len(errors)}/{MAX_ERROR_ATTEMPTS} errors")
    if any(a["outcome"] == "UNWIRED" for a in fresh):
        return StepStatus(**base, status="UNWIRED", detail="no executor in the runtime that tried it")
    return StepStatus(**base, status="STALE", detail=f"every terminal attempt is older than {ttl}d")


@dataclass
class Exhaustion:
    verdict: str
    machine_exhausted: bool
    steps: List[StepStatus]
    resolved_by: List[str] = field(default_factory=list)
    open_automated: List[str] = field(default_factory=list)
    pending_human: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"verdict": self.verdict, "machine_exhausted": self.machine_exhausted,
                "resolved_by": self.resolved_by, "open_automated": self.open_automated,
                "pending_human": self.pending_human, "steps": [asdict(s) for s in self.steps]}


def exhaustion(state: JobState, now: Optional[datetime] = None, *, min_confidence: str = "MEDIUM") -> Exhaustion:
    now = now or datetime.now(timezone.utc)
    statuses = [step_status(state, s, now, min_confidence=min_confidence) for s in state.steps]
    resolved = [s.step_id for s in statuses if s.status == "RESOLVED"]
    open_auto = [s.step_id for s in statuses if s.mode == AUTOMATED and s.status in OPEN_STATES]
    pending = [s.step_id for s in statuses if s.status == "HUMAN_PENDING"]
    if resolved:
        verdict = "RESOLVED_FREE"
    elif open_auto:
        verdict = "FREE_PATH_OPEN"
    elif pending:
        verdict = "HUMAN_STEP_PENDING"
    else:
        verdict = "EXHAUSTED"
    return Exhaustion(verdict, not open_auto, statuses, resolved, open_auto, pending)


@dataclass
class PaidDecision:
    allow: bool
    provider: str
    reasons: List[str]
    exhaustion: Dict[str, Any]
    authorization_id: Optional[str] = None
    waived: Dict[str, str] = field(default_factory=dict)
    est_cost_usd: Optional[float] = None
    #: what the budget is charged for this call: the estimate, or the per-call cap when unpriced
    effective_cost_usd: Optional[float] = None
    spent_usd: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def decide_paid(state: JobState, provider: str, *, est_cost_usd: Optional[float],
                authorization: Optional[Mapping[str, Any]], authorization_error: Optional[str] = None,
                now: Optional[datetime] = None, min_confidence: str = "MEDIUM") -> PaidDecision:
    """``authorization`` is a SPEND_AUTHORIZED body that its authority has already
    CONFIRMED for this call; ``authorization_error`` says why none could be confirmed."""
    now = now or datetime.now(timezone.utc)
    reasons: List[str] = []
    catalog = PAID_PROVIDERS.get(provider)
    if catalog is None:
        reasons.append(f"UNKNOWN_PAID_PROVIDER:{provider}")
    elif state.fact not in catalog.facts:
        reasons.append(f"PROVIDER_DOES_NOT_ESTABLISH_FACT:{state.fact}")
    if state.closed is not None:
        reasons.append(f"JOB_CLOSED:{state.closed.get('resolution')}")
    ex = exhaustion(state, now, min_confidence=min_confidence)
    if ex.verdict == "RESOLVED_FREE":
        reasons.append("FACT_RESOLVED_FREE:" + ",".join(ex.resolved_by))
    waives: Dict[str, str] = dict((authorization or {}).get("waives") or {})
    still_open = [s for s in ex.open_automated if s not in waives]
    still_human = [s for s in ex.pending_human if s not in waives]
    if still_open:
        reasons.append("FREE_PATH_OPEN:" + ",".join(still_open))
    if still_human:
        reasons.append("HUMAN_STEP_PENDING:" + ",".join(still_human))
    spent = 0.0
    cost: Optional[float] = est_cost_usd
    if authorization is None:
        reasons.append("SPEND_UNAUTHORIZED" + (f":{authorization_error}" if authorization_error else ""))
    else:
        auth_id = authorization["authorization_id"]
        spent = state.spent_usd(auth_id)
        if provider not in authorization.get("providers", []):
            reasons.append(f"PROVIDER_NOT_AUTHORIZED:{provider}")
        if parse_ts(authorization["expires_at"]) <= now:
            reasons.append("AUTHORIZATION_EXPIRED")
        per_call = authorization.get("max_per_call_usd")
        if cost is None:
            if per_call is None:
                reasons.append("UNPRICED_WITHOUT_PER_CALL_CAP")
            cost = per_call
        if cost is not None and per_call is not None and cost > float(per_call):
            reasons.append(f"OVER_PER_CALL_CAP:{cost}>{per_call}")
        if cost is not None and spent + cost > float(authorization["max_total_usd"]):
            reasons.append(f"OVER_BUDGET:{spent}+{cost}>{authorization['max_total_usd']}")
    used = {k: v for k, v in waives.items() if k in ex.open_automated or k in ex.pending_human}
    return PaidDecision(allow=not reasons, provider=provider, reasons=reasons, exhaustion=ex.to_dict(),
                        authorization_id=(authorization or {}).get("authorization_id"), waived=used,
                        est_cost_usd=est_cost_usd, effective_cost_usd=cost, spent_usd=spent)

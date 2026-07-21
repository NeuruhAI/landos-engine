"""
capacity_plan.py — P-27: the 30,000-record skip-trace capacity planner. PURE PLAN, ZERO POWER.

This module can only ARITHMETIC. It imports no network layer, opens no socket, holds
no API key, and its output carries approval_gate.approved=False as a constant — there
is no parameter that flips it. Execution lives exclusively behind the operator
keystroke it names (scripts/skip_trace_tracerfy.py --batch --confirm), which has its
own independent --confirm gate. Structural mint-safety, same doctrine as P-23.

WHAT IT PLANS (Tracerfy economics, verified in scripts/skip_trace_tracerfy.py):
  * trace spend  — trace_type 'normal' = 1 credit/record, 'advanced' = 2; $0.02/credit;
                   MISSES ARE FREE, so the billed floor is hits_min × credits × $0.02
                   and the ceiling is every planned record billed (all hit).
  * expected hits— hit-rate RANGE, stated as an assumption (default 0.60–0.85; the
                   vendor bills only hits, so the rate drives spend + phone volume,
                   never eligibility). Every assumption is echoed into the receipt.
  * phone volume — phones-per-hit range (default 1.0–3.0 across mobile_1..5 /
                   landline_1..3 slots) → the DNC-scrub sizing, which bills PER PHONE
                   ($0.02, separate spend) and is planned here, never executed.
  * batches      — deterministic slicing of the planned pool into fixed-size batches
                   (default 2,000/queue) so the operator sees exactly how many queue
                   submissions the run would take.

Everything is deterministic: no clock, no randomness, canonical receipt hash.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .receipt import canonical_json, section_hash

PLANNER_VERSION = "1.0.0"

# Mirrors scripts/skip_trace_tracerfy.py — a drift test pins them equal.
TRACE_COST_PER_CREDIT = 0.02
TRACE_TYPE_CREDITS = {"normal": 1, "advanced": 2}

DEFAULT_TARGET = 30_000
DEFAULT_BATCH_SIZE = 2_000

DEFAULT_ASSUMPTIONS: Dict[str, Any] = {
    # conservative range; misses are free so the rate moves spend, not eligibility
    "hit_rate_range": (0.60, 0.85),
    # phones returned per hit (primary + mobile/landline slots), for DNC sizing
    "phones_per_hit_range": (1.0, 3.0),
}

APPROVAL_KEYSTROKE = "python -m scripts.skip_trace_tracerfy --batch --confirm"


def _money(x: float) -> float:
    return round(x, 2)


def plan_capacity(pool_available: int,
                  target: int = DEFAULT_TARGET,
                  batch_size: int = DEFAULT_BATCH_SIZE,
                  trace_type: str = "normal",
                  dnc: bool = True,
                  assumptions: Optional[Dict[str, Any]] = None,
                  per_county: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Plan (never execute) a skip-trace run of up to `target` records.

    pool_available — eligible not_traced individuals actually in the spine (the
                     caller counts them read-only; a fixture works identically)
    per_county     — optional {fips: count} breakdown of that pool, echoed sorted
    Returns the full plan dict incl. spend ranges, the approval gate (constant
    unapproved), and a canonical receipt hash.
    """
    if pool_available < 0:
        raise ValueError(f"pool_available is negative: {pool_available}")
    if target <= 0:
        raise ValueError(f"target must be positive: {target}")
    if batch_size <= 0:
        raise ValueError(f"batch_size must be positive: {batch_size}")
    if trace_type not in TRACE_TYPE_CREDITS:
        raise ValueError(f"unknown trace_type {trace_type!r}; "
                         f"valid: {sorted(TRACE_TYPE_CREDITS)}")
    a = dict(DEFAULT_ASSUMPTIONS)
    a.update(assumptions or {})
    hr_lo, hr_hi = a["hit_rate_range"]
    pph_lo, pph_hi = a["phones_per_hit_range"]
    if not (0 <= hr_lo <= hr_hi <= 1):
        raise ValueError(f"hit_rate_range must be 0<=lo<=hi<=1: {a['hit_rate_range']}")
    if not (0 <= pph_lo <= pph_hi):
        raise ValueError(f"phones_per_hit_range must be 0<=lo<=hi: {a['phones_per_hit_range']}")

    planned = min(pool_available, target)
    shortfall = max(0, target - pool_available)
    credits = TRACE_TYPE_CREDITS[trace_type]

    full, rem = divmod(planned, batch_size)
    batch_sizes = [batch_size] * full + ([rem] if rem else [])

    hits_min = int(planned * hr_lo)
    hits_max = int(planned * hr_hi)
    phones_min = int(hits_min * pph_lo)
    phones_max = int(hits_max * pph_hi)

    # misses free -> floor bills only the minimum hits; ceiling bills every record
    trace_min = _money(hits_min * credits * TRACE_COST_PER_CREDIT)
    trace_max = _money(planned * credits * TRACE_COST_PER_CREDIT)
    dnc_min = _money(phones_min * TRACE_COST_PER_CREDIT) if dnc else 0.0
    dnc_max = _money(phones_max * TRACE_COST_PER_CREDIT) if dnc else 0.0

    body = {
        "planner_version": PLANNER_VERSION,
        "mode": "DRY-RUN PLAN — NOTHING EXECUTED",
        "target": target,
        "pool_available": pool_available,
        "planned": planned,
        "shortfall": shortfall,
        "per_county": {k: (per_county or {})[k] for k in sorted(per_county or {})},
        "trace_type": trace_type,
        "credits_per_record": credits,
        "batches": {"batch_size": batch_size, "count": len(batch_sizes),
                    "sizes": batch_sizes},
        "expected_hits": {"min": hits_min, "max": hits_max},
        "phone_volume": {"min": phones_min, "max": phones_max},
        "spend_usd": {
            "trace": {"min": trace_min, "max": trace_max},
            "dnc_scrub": {"min": dnc_min, "max": dnc_max,
                          "note": ("separate spend, bills per phone"
                                   if dnc else "not planned (--no-dnc)")},
            "total": {"min": _money(trace_min + dnc_min),
                      "max": _money(trace_max + dnc_max)},
        },
        "assumptions": {"hit_rate_range": [hr_lo, hr_hi],
                        "phones_per_hit_range": [pph_lo, pph_hi],
                        "cost_per_credit_usd": TRACE_COST_PER_CREDIT,
                        "misses_free": True},
        "approval_gate": {
            "approved": False,   # CONSTANT — this module cannot approve anything
            "required_keystroke": APPROVAL_KEYSTROKE,
            "note": ("No trace, scrub, or spend happens from this plan. The named "
                     "keystroke is a separate tool with its own --confirm gate; "
                     "the operator pulls every trigger."),
        },
    }
    body["receipt_sha256"] = section_hash(body)
    return body


def render_plan(plan: Dict[str, Any]) -> str:
    s = plan["spend_usd"]
    L = [f"=== SKIP-TRACE CAPACITY PLAN (v{plan['planner_version']}) — "
         f"{plan['mode']} ===",
         f"target={plan['target']:,}  pool_available={plan['pool_available']:,}  "
         f"planned={plan['planned']:,}"
         + (f"  shortfall={plan['shortfall']:,} (pool smaller than target)"
            if plan["shortfall"] else ""),
         f"batches: {plan['batches']['count']} x <= {plan['batches']['batch_size']:,} "
         f"({plan['trace_type']}, {plan['credits_per_record']} credit/record)",
         f"expected hits : {plan['expected_hits']['min']:,} – {plan['expected_hits']['max']:,} "
         f"(assumed hit rate {plan['assumptions']['hit_rate_range'][0]:.0%}–"
         f"{plan['assumptions']['hit_rate_range'][1]:.0%})",
         f"phone volume  : {plan['phone_volume']['min']:,} – {plan['phone_volume']['max']:,} "
         f"(DNC scrub sizing; bills per phone)",
         f"spend — trace : ${s['trace']['min']:,.2f} – ${s['trace']['max']:,.2f} (misses free)",
         f"spend — DNC   : ${s['dnc_scrub']['min']:,.2f} – ${s['dnc_scrub']['max']:,.2f}",
         f"spend — TOTAL : ${s['total']['min']:,.2f} – ${s['total']['max']:,.2f}"]
    if plan["per_county"]:
        L.append("pool by county: " + "  ".join(f"[{k}]={v:,}"
                                                for k, v in plan["per_county"].items()))
    L.append(f"receipt sha256:{plan['receipt_sha256'][:24]}…")
    L.append("")
    L.append("🔴 OPERATOR APPROVAL GATE — this plan executes NOTHING. Paid action only via:")
    L.append(f"    {plan['approval_gate']['required_keystroke']}")
    return "\n".join(L)


def plan_canonical(plan: Dict[str, Any]) -> str:
    return canonical_json(plan)

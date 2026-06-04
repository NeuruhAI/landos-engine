"""
multiplier.py — the policy-tailwind multiplier with a PROVENANCE GATE.

Rules:
  * Only 'authoritative' overlays whose effective_date has not passed contribute.
  * 'sourced_unconfirmed' / 'stub' hits (and expired authoritative ones) are
    LOGGED in the output but applied as x1.0 — they never move the score until
    a human promotes them to 'authoritative'.
  * Applied weights add together, then clamp to [1.1, 1.4] when >=1 hit is
    applied; the multiplier is x1.0 when nothing is applied.

Pure: the caller passes the hits (from overlays.overlay_hits) and the as_of date.
"""
from datetime import date
from typing import Any, Dict, List, Optional

MULT_MIN = 1.1
MULT_MAX = 1.4


def _as_date(v: Any) -> Optional[date]:
    if v is None or isinstance(v, date):
        return v
    return date.fromisoformat(str(v)[:10])


def compute_multiplier(
    hits: List[Dict[str, Any]], as_of: Optional[date] = None
) -> Dict[str, Any]:
    as_of = as_of or date.today()
    applied: List[Dict[str, Any]] = []
    logged: List[Dict[str, Any]] = []
    applied_sum = 0.0

    for h in hits:
        status = h.get("provenance_status")
        eff = _as_date(h.get("effective_date"))
        expired = eff is not None and as_of > eff
        if status == "authoritative" and not expired:
            applied_sum += float(h.get("multiplier_weight") or 0.0)
            applied.append(h)
        else:
            if status == "authoritative" and expired:
                reason = "expired"
            else:
                reason = status or "unknown"
            logged.append({**h, "not_applied_reason": reason})

    multiplier = min(MULT_MAX, max(MULT_MIN, 1.0 + applied_sum)) if applied else 1.0
    return {
        "multiplier": round(multiplier, 4),
        "applied": applied,
        "logged": logged,
    }

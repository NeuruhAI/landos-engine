"""
comps.py — ARV estimation from manual comps + APG bid-ceiling math. PURE
(no DB/network) so it's trivially testable and the model logic stays Neuruh-side.

ARV: weighted-by-recency × proximity mean of a per-acre or per-lot unit value,
with a median cross-check. Basis is per_acre only when the subject acreage AND
every comp's acreage are known; otherwise per_lot (whole-sale-price).

Max bid: APG's documented tiered thresholds applied to the ARV, then constrained
by the "must net ≥ $10K" rule. Returns an explicit range + which constraint binds.
Real comps are DATA-GROUNDED (manual_entry); never fabricate a comp or a price.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any, Dict, List, Optional


def _to_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _recency_weight(sale_date: Any, as_of: date) -> float:
    if not sale_date:
        return 0.5
    if not isinstance(sale_date, date):
        try:
            sale_date = date.fromisoformat(str(sale_date)[:10])
        except ValueError:
            return 0.5
    age_yrs = max(0.0, (as_of - sale_date).days / 365.25)
    return math.exp(-age_yrs / 2.0)  # ~2-year decay


def _distance_weight(mi: Any) -> float:
    d = _to_float(mi)
    if d is None:
        return 0.6
    return 1.0 / (1.0 + max(0.0, d))


def _median(xs: List[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def compute_arv(subject_acreage: Any, comps: List[Dict[str, Any]], as_of: date) -> Dict[str, Any]:
    """Return ARV estimate + method/confidence/cited rows. arv=None if no usable comp."""
    subj_acre = _to_float(subject_acreage)
    usable = [c for c in (comps or []) if _to_float(c.get("sale_price")) and _to_float(c.get("sale_price")) > 0]
    if not usable:
        return {"arv": None, "n": 0, "method": "n/a", "basis": "n/a",
                "confidence": "none", "rows": [], "weighted_unit": None, "median_unit": None}

    per_acre = bool(subj_acre and subj_acre > 0 and all((_to_float(c.get("acreage")) or 0) > 0 for c in usable))
    basis = "per_acre" if per_acre else "per_lot"

    rows = []
    for c in usable:
        price = _to_float(c.get("sale_price"))
        unit = price / _to_float(c.get("acreage")) if per_acre else price
        w = _recency_weight(c.get("sale_date"), as_of) * _distance_weight(c.get("distance_mi"))
        rows.append({"comp": c, "unit": round(unit, 2), "weight": round(w, 4)})

    wsum = sum(r["weight"] for r in rows) or 1.0
    weighted_unit = sum(r["unit"] * r["weight"] for r in rows) / wsum
    median_unit = _median([r["unit"] for r in rows])

    if per_acre:
        arv = weighted_unit * subj_acre
    else:
        arv = weighted_unit

    n = len(rows)
    confidence = "low (<3 comps)" if n < 3 else "normal"
    return {
        "arv": round(arv, 2),
        "arv_median": round(median_unit * subj_acre if per_acre else median_unit, 2),
        "n": n,
        "basis": basis,
        "method": f"weighted mean of {basis} unit value by recency×proximity (median cross-check)",
        "confidence": confidence,
        "weighted_unit": round(weighted_unit, 2),
        "median_unit": round(median_unit, 2),
        "rows": rows,
    }


def apg_tier(arv: float):
    """(label, initial_pct, max_pct) — APG's documented offer thresholds."""
    if arv < 50000:
        return ("Under $50K", 0.50, 0.55)
    if arv < 100000:
        return ("$50K–$100K", 0.50, 0.55)
    if arv < 250000:
        return ("$100K–$250K", 0.55, 0.60)
    return ("$250K+", 0.55, 0.65)


def compute_max_bid(arv: float, costs_pct: float = 0.10, prep: float = 0.0,
                    net_floor: float = 10000.0) -> Dict[str, Any]:
    """APG ceiling: acquire at initial–max % of ARV, constrained by net ≥ net_floor."""
    tier, lo, hi = apg_tier(arv)
    costs = costs_pct * arv
    net_cap = arv - costs - prep - net_floor          # bid that leaves exactly net_floor
    bid_low = lo * arv
    threshold_high = hi * arv
    bid_high = min(threshold_high, net_cap)
    binding = "net-$10K floor" if net_cap < threshold_high else "APG % ceiling"
    feasible = bid_high >= bid_low and bid_high > 0
    return {
        "tier": tier, "initial_pct": lo, "max_pct": hi,
        "costs": round(costs, 2), "prep": round(prep, 2),
        "bid_low": round(bid_low, 2), "bid_high": round(max(0.0, bid_high), 2),
        "binding": binding, "feasible": feasible,
        "net_at_high": round(arv - max(0.0, bid_high) - costs - prep, 2),
    }

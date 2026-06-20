"""
comp_match.py — COMP-MATCHER v1: per-subject FMV + arbitrage from SDF land comps.

PURE (no DB/network). Conservative — NULL over a bad number. For a small urban land
subject, find arm's-length vacant-land sales in a tight acreage band, take the median
$/acre, and derive a FMV + an arbitrage_component on the composite's 0-100 scale.

Arm's-length proxy (the SDF file has NO arm's-length flag, and ~7% are $1 nominal
quitclaims): vacant_land_flag set AND sale_price >= $1,000 AND price_per_acre >=
$1,000/ac AND acreage >= 0.01 (the billion/acre guard). Below those = nominal/garbage.

Acreage band: +/-50% of the subject; widen ONCE to +/-100% if <3 comps; still <3 ->
arbitrage_component = NULL (a 0.2-ac lot must not be comped against multi-acre land).

arbitrage_component MAPPING (documented): FMV = median(price_per_acre) * subject acreage;
basis = the subject's assessor land value (av_land — a PROXY for acquisition basis, not a
true cost); spread = FMV - basis; arbitrage_component = clamp(spread / FMV, 0, 1) * 100 =
the % by which market FMV exceeds the assessed land basis (the equity/under-assessment
upside). FMV <= basis -> 0 (grounded, no upside). No basis -> NULL (spread-only, unmappable).
"""
from __future__ import annotations

import statistics
from typing import Any, Dict, List, Optional

METHOD = "comp_match_v1"
ACRE_FLOOR = 0.01
MIN_SALE_PRICE = 1000.0
MIN_PPA = 1000.0
# Per-acre artifact guard (extends the billion/acre guard): $/acre is unstable at tiny
# acreage (a $20k 0.02-ac lot = $1M/acre). Drop comps above this ceiling so a sub-0.06ac
# subject whose band is dominated by such artifacts falls below MIN_COMPS -> NULL (the
# conservative outcome), rather than a per-acre-inflated FMV. Plausible residential land
# in this market is <=~$300k/acre; the ceiling only trims the extreme tail.
MAX_PPA = 1_000_000.0
MIN_COMPS = 3


def _f(x: Any) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def is_arms_length(c: Dict[str, Any]) -> bool:
    sp, ppa, ac = _f(c.get("sale_price")), _f(c.get("price_per_acre")), _f(c.get("acreage"))
    flag = str(c.get("vacant_land_flag", "")).strip().upper() in ("Y", "TRUE", "1")
    return bool(flag and sp and sp >= MIN_SALE_PRICE and ppa and MIN_PPA <= ppa <= MAX_PPA
                and ac and ac >= ACRE_FLOOR)


def _band(comps: List[Dict], subj_acre: float, pct: float) -> List[Dict]:
    lo, hi = subj_acre * (1 - pct), subj_acre * (1 + pct)
    return [c for c in comps if lo <= (_f(c.get("acreage")) or 0) <= hi]


def match_subject(subject: Dict[str, Any], pool: List[Dict[str, Any]],
                  recency_cut: str) -> Dict[str, Any]:
    """subject: {parcel_apn, acreage, av_land}. pool: arm's-length-or-not comp rows.
    recency_cut: 'YYYY-MM' (prefer conveyances >= this; fall back to full band)."""
    subj_acre, basis = _f(subject.get("acreage")), _f(subject.get("av_land"))
    out: Dict[str, Any] = {
        "method": METHOD, "fmv": None, "median_ppa": None, "comps_n": 0, "spread": None,
        "band_used": None, "date_range": None, "arbitrage_component": None,
        "basis": basis, "basis_source": "assessor_av_land", "status": None, "matched": [],
    }
    if not subj_acre or subj_acre < ACRE_FLOOR:
        out["status"] = "subject acreage below floor"
        return out

    arm = [c for c in pool if is_arms_length(c)]
    band = _band(arm, subj_acre, 0.5)
    band_used = "+/-50%"
    if len(band) < MIN_COMPS:
        band = _band(arm, subj_acre, 1.0)
        band_used = "+/-100%"
    out["band_used"] = band_used

    recent = [c for c in band if (c.get("conveyance_date") or "") >= recency_cut]
    use = recent if len(recent) >= MIN_COMPS else band
    out["comps_n"] = len(use)
    if len(use) < MIN_COMPS:
        out["status"] = f"insufficient comps ({len(use)}<{MIN_COMPS}) -> NULL"
        return out

    ppa = [p for p in (_f(c.get("price_per_acre")) for c in use) if p]
    med_ppa = statistics.median(ppa)
    fmv = med_ppa * subj_acre
    dates = sorted(c.get("conveyance_date") for c in use if c.get("conveyance_date"))
    spread = (fmv - basis) if basis is not None else None
    arb = (max(0.0, min(1.0, spread / fmv)) * 100.0
           if (basis is not None and fmv and fmv > 0) else None)

    out.update({
        "fmv": round(fmv, 2), "median_ppa": round(med_ppa, 2), "comps_n": len(use),
        "spread": (round(spread, 2) if spread is not None else None),
        "date_range": [dates[0], dates[-1]] if dates else None,
        "arbitrage_component": (round(arb, 2) if arb is not None else None),
        "status": "grounded" if arb is not None else "grounded_no_basis",
        "matched": [{"comp_apn": c.get("parcel_number"), "sale_date": c.get("conveyance_date"),
                     "sale_price": _f(c.get("sale_price")), "acreage": _f(c.get("acreage")),
                     "price_per_acre": _f(c.get("price_per_acre"))} for c in use],
    })
    return out

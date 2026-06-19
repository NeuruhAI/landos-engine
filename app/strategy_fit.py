"""
strategy_fit.py — exit-strategy fit axes, SEPARATE from the wholesale composite.

alt_use_fitness (developer / redevelopment fit): brownfield/Part-201 + Opportunity
Zone + electrical-substation (interconnection) proximity. ADDITIVE and fully
independent of owner/distress/arbitrage components, the composite score,
final_lead_score, and weights_version — it lives under its OWN version key
(alt_use_fitness_v1) so the weights retune without touching the composite.

PURE (no DB / no network); haversine only (no GDAL/shapely on this box).

MISSING is first-class: an input that is genuinely ABSENT (not observed) is omitted
from the sum and marked grounded=false — never imputed as 0-as-data. A present-but-
false boolean (e.g. observed NOT in an OZ) contributes 0 legitimately (grounded=true,
value=false). Score is a capped weighted sum of grounded inputs — NO renormalization.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

ALT_USE_VERSION = "alt_use_fitness_v1"
W_CONTAM = 40
W_OZ = 30
# substation proximity bands (nearest miles -> points). v1 starting weights.
# v2 (alt_use_fitness_v2 — NOT built): weight proximity BY VOLTAGE using MAX_VOLT from
# substations_michiana.geojson — transmission (>=69kV) is interconnection-ready, distribution
# is not. Treat MAX_VOLT -999999 as UNKNOWN -> grounded=false for the voltage sub-factor (never
# 0). v1 bands on distance only, which barely discriminates here (115/136 are <1mi).
_SUB_BANDS = [(1.0, 30, "<1mi"), (5.0, 22, "1-5mi"), (15.0, 12, "5-15mi"),
              (30.0, 5, "15-30mi")]
NOTE = ("Developer/redevelopment fit — brownfield + OZ + interconnection. "
        "Separate from the wholesale composite; not a CRM headline.")


def haversine_mi(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 3958.7613  # mean earth radius, miles
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def substation_band(mi: float) -> tuple[int, str]:
    for cut, pts, label in _SUB_BANDS:
        if mi < cut:
            return pts, label
    return 0, ">=30mi"


def _nearest(lat: float, lng: float, substations: List[Dict[str, Any]]):
    best, best_mi = None, None
    for s in substations:
        d = haversine_mi(lat, lng, s["lat"], s["lng"])
        if best_mi is None or d < best_mi:
            best, best_mi = s, d
    return best, best_mi


def alt_use_fitness(lead: Dict[str, Any], substations: List[Dict[str, Any]],
                    manifest_sha: Optional[str], as_of: str) -> Dict[str, Any]:
    """
    lead keys:
      distress_tokens (list), distress_present (bool — distress_stack observed?),
      in_opportunity_zone (bool|None), oz_present (bool — key observed?),
      situs_lat, situs_lng, parcel_state ('MI'|'IN'|None).
    substations: [{lat, lng, state}]. manifest_sha: CC3 feed sha. as_of: iso str.
    Returns the strategy_fit.alt_use_fitness annotation object.
    """
    inputs: Dict[str, Any] = {}
    score = 0

    # ---- contamination (brownfield / Part-201) — grounded if distress_stack observed
    toks = [str(t).strip().lower() for t in (lead.get("distress_tokens") or [])]
    contam_present = lead.get("distress_present", bool(toks))
    is_contam = any(t in ("environmental_part201", "brownfield") for t in toks)
    kind = ("part201" if "environmental_part201" in toks
            else "brownfield" if "brownfield" in toks else None)
    if contam_present:
        inputs["contamination"] = {"value": is_contam, "kind": kind, "grounded": True,
                                   "weight": W_CONTAM,
                                   "source": "engine.signal_overlap.distress_stack"}
        if is_contam:
            score += W_CONTAM
    else:
        inputs["contamination"] = {"value": None, "kind": None, "grounded": False,
                                   "weight": W_CONTAM,
                                   "source": "engine.signal_overlap.distress_stack"}

    # ---- opportunity zone — grounded if the key was observed
    oz_present = lead.get("oz_present", lead.get("in_opportunity_zone") is not None)
    if oz_present:
        oz_val = bool(lead.get("in_opportunity_zone"))
        inputs["opportunity_zone"] = {"value": oz_val, "grounded": True, "weight": W_OZ,
                                      "source": "engine.signal_overlap.in_opportunity_zone"}
        if oz_val:
            score += W_OZ
    else:
        inputs["opportunity_zone"] = {"value": None, "grounded": False, "weight": W_OZ,
                                      "source": "engine.signal_overlap.in_opportunity_zone"}

    # ---- substation proximity — haversine to nearest point; do NOT pretend coverage:
    # an MI parcel whose nearest point is across the state line (feed is NW-Indiana,
    # no MI substations) is grounded=false, not a fake distance.
    lat, lng, pstate = lead.get("situs_lat"), lead.get("situs_lng"), lead.get("parcel_state")
    sub: Dict[str, Any] = {"nearest_mi": None, "band": None, "grounded": False, "weight": 0,
                           "coverage_scope": "nw_indiana_only", "source": "hifld_substations",
                           "manifest_sha": manifest_sha}
    if lat is not None and lng is not None and substations:
        best, mi = _nearest(float(lat), float(lng), substations)
        pts, band = substation_band(mi)
        nstate = (best or {}).get("state")
        across_line = (pstate == "MI" and nstate != "MI")
        grounded = not across_line
        sub.update({"nearest_mi": round(mi, 2), "band": band, "nearest_state": nstate,
                    "grounded": grounded, "weight": (pts if grounded else 0)})
        if grounded:
            score += pts
    inputs["substation_proximity"] = sub

    return {
        "version": ALT_USE_VERSION,
        "score": min(100, score),
        "inputs": inputs,
        "computed_at": as_of,
        "note": NOTE,
    }

"""
scoring.py — the Deal Intelligence Score model. THE crown jewel.

This file is deliberately PURE: it takes inputs and weights, returns a score.
No database, no network. That makes it trivially testable and means the model
logic never leaks onto a client's infrastructure — it lives here, Neuruh-side.

The composite score (0-100) blends three components:
  1. DISTRESS   — how motivated the situation is (tax, probate, vacancy...)
  2. OWNER      — how motivated the person is (out-of-state, senior, no heirs...)
  3. ARBITRAGE  — how good the math is (discount to market, subdivision upside, carry)

Every weight is configurable. The active weights live in the database
(landos.scoring_weights) so they can be retrained from real deal outcomes
without touching this code. DEFAULT_WEIGHTS below is the v1 starting point
and the safety fallback if no active weights exist for a tenant.
"""
from typing import Any, Dict

# --- v1 default weights. Each sub-group's values are the share that signal
#     contributes WITHIN its component. The "composite" group is how the three
#     components blend into the final 0-100. ---
DEFAULT_WEIGHTS: Dict[str, Dict[str, float]] = {
    "composite": {"distress": 0.40, "owner": 0.25, "arbitrage": 0.35},
    "distress": {
        "tax_delinquent": 0.25,
        "probate": 0.25,
        "pre_foreclosure": 0.20,
        "code_violation": 0.10,
        "vacancy": 0.10,
        "long_hold": 0.10,
        "environmental": 0.15,  # MI Part 201 / brownfield — second-tier distress (added default v3)
    },
    "owner": {
        "out_of_state": 0.30,
        "age_senior": 0.30,
        "age_boomer": 0.20,
        "no_succession": 0.25,
        "is_entity": 0.15,
    },
    "arbitrage": {"discount": 0.50, "subdivision": 0.30, "carry": 0.20},
}

# Human-readable names for the explainability output ("the why").
LABELS = {
    "tax_delinquent": "Tax delinquent",
    "probate": "Probate",
    "pre_foreclosure": "Pre-foreclosure",
    "code_violation": "Code violation",
    "vacancy": "Vacancy",
    "long_hold": "Long-term hold",
    "environmental": "Environmental (Part 201 / brownfield)",
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def score_parcel(inputs: Dict[str, Any], weights: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute the composite score and the top 3 contributing factors.

    `inputs` shape:
      {
        "distress":  {tax_delinquent, probate, pre_foreclosure, code_violation, vacancy, long_hold},  # bools
        "owner":     {is_entity(bool), age_band("senior"|"boomer"|"other"), out_of_state(bool), no_succession(bool)},
        "arbitrage": {list_price_per_acre, market_price_per_acre, acreage, subdivisions_to_2x, carry_feasible_90d}
      }
    Returns: {score, distress_component, owner_component, arbitrage_component, top_factors}
    """
    cw = weights["composite"]
    # factors collects (label, points_added_to_final) for explainability.
    factors = []

    # ---------- DISTRESS component (0..100) ----------
    dw = weights["distress"]
    d = inputs.get("distress", {})
    distress_unit = 0.0
    for key, weight in dw.items():
        if d.get(key):
            distress_unit += weight
            # This signal's true contribution to the FINAL score:
            factors.append((LABELS.get(key, key), 100 * weight * cw["distress"]))
    distress_component = 100 * _clamp(distress_unit)

    # ---------- OWNER component (0..100) ----------
    ow = weights["owner"]
    o = inputs.get("owner", {})
    owner_unit = 0.0
    if o.get("out_of_state"):
        owner_unit += ow["out_of_state"]
        factors.append(("Out-of-state owner", 100 * ow["out_of_state"] * cw["owner"]))
    age = o.get("age_band", "other")
    if age == "senior":
        owner_unit += ow["age_senior"]
        factors.append(("Senior owner (65+)", 100 * ow["age_senior"] * cw["owner"]))
    elif age == "boomer":
        owner_unit += ow["age_boomer"]
        factors.append(("Boomer owner (55-64)", 100 * ow["age_boomer"] * cw["owner"]))
    if o.get("no_succession"):
        owner_unit += ow["no_succession"]
        factors.append(("No succession / heirs", 100 * ow["no_succession"] * cw["owner"]))
    if o.get("is_entity"):
        owner_unit += ow["is_entity"]
        factors.append(("Entity-owned", 100 * ow["is_entity"] * cw["owner"]))
    owner_component = 100 * _clamp(owner_unit)

    # ---------- ARBITRAGE component (0..100) ----------
    aw = weights["arbitrage"]
    a = inputs.get("arbitrage", {})
    mppa = a.get("market_price_per_acre", 0) or 0
    lppa = a.get("list_price_per_acre", 0) or 0
    discount = _clamp((mppa - lppa) / mppa) if (mppa > 0 and lppa > 0) else 0.0
    subs = a.get("subdivisions_to_2x", 10) or 10
    # Fewer subdivisions to double = better. 1 split -> 1.0, 10+ splits -> 0.
    subdiv_score = _clamp(1 - (max(1, subs) - 1) / 9.0)
    carry = 1.0 if a.get("carry_feasible_90d") else 0.0
    arb_unit = _clamp(discount * aw["discount"] + subdiv_score * aw["subdivision"] + carry * aw["carry"])
    arbitrage_component = 100 * arb_unit
    if discount > 0:
        factors.append((f"{round(discount * 100)}% below market", 100 * discount * aw["discount"] * cw["arbitrage"]))
    if subdiv_score > 0:
        factors.append(("Subdivision upside", 100 * subdiv_score * aw["subdivision"] * cw["arbitrage"]))
    if carry > 0:
        factors.append(("90-day carry feasible", 100 * carry * aw["carry"] * cw["arbitrage"]))

    # ---------- COMPOSITE ----------
    final = (
        distress_component * cw["distress"]
        + owner_component * cw["owner"]
        + arbitrage_component * cw["arbitrage"]
    )
    final = round(_clamp(final, 0, 100), 2)

    # Top 3 factors by their real contribution = the explainability payload.
    factors.sort(key=lambda t: t[1], reverse=True)
    top = [{"factor": f, "points": round(p, 2)} for f, p in factors[:3] if p > 0]

    return {
        "score": final,
        "distress_component": round(distress_component, 2),
        "owner_component": round(owner_component, 2),
        "arbitrage_component": round(arbitrage_component, 2),
        "top_factors": top,
    }


# ===========================================================================
# benton_harbor_edge — acquisition "edge" model (profile: benton_harbor_edge)
# ===========================================================================
# A location/upside-first model, distinct from the distress/owner/arbitrage
# composite above. The base score blends four 0..1 sub-scores; distress is
# deliberately de-emphasized (tax_delinquent carries a NEAR-ZERO weight) so the
# edge reflects LOCATION + UPSIDE, not seller motivation. The policy-tailwind
# multiplier and deal-killer gates are applied OUTSIDE this pure function
# (see app/multiplier.py and app/gates.py) so this stays a pure base-score model.

EDGE_PROFILE = "benton_harbor_edge"

EDGE_DEFAULT_WEIGHTS: Dict[str, Dict[str, float]] = {
    "base": {
        "location_score": 0.30,
        "future_potential": 0.25,
        "dev_fit": 0.25,
        "prep_lift_inverse": 0.20,
    },
    # tax_delinquent FORCED to near-zero — present for auditability, ~0 effect.
    "distress": {"tax_delinquent": 0.0001},
}

EDGE_SUBSCORES = ("location_score", "future_potential", "dev_fit", "prep_lift_inverse")


def score_edge(inputs: Dict[str, Any], weights: Dict[str, Any]) -> Dict[str, Any]:
    """
    Compute the benton_harbor_edge BASE score (0-100) from four 0..1 sub-scores.

    `inputs` shape:
      {
        "base": {location_score, future_potential, dev_fit, prep_lift_inverse},  # each 0..1
        "distress": {"tax_delinquent": bool}
      }
    Returns: {base_score, sub_scores, factors, tax_delinquent_contribution}.
    Pure: no DB, no network, no overlay/gate logic.
    """
    bw = weights.get("base", EDGE_DEFAULT_WEIGHTS["base"])
    b = inputs.get("base", {})
    sub_scores: Dict[str, float] = {}
    factors = []
    unit = 0.0
    for key in EDGE_SUBSCORES:
        val = _clamp(float(b.get(key, 0.0) or 0.0))
        sub_scores[key] = round(val, 4)
        w = float(bw.get(key, 0.0))
        unit += val * w
        factors.append((key, round(100 * val * w, 2)))

    # near-zero tax_delinquent term (explicit + auditable, ~0 effect on score)
    tdw = float(weights.get("distress", {}).get("tax_delinquent", 0.0))
    td_on = bool(inputs.get("distress", {}).get("tax_delinquent"))
    if td_on:
        unit += tdw

    base_score = round(_clamp(100 * unit, 0, 100), 2)
    factors.sort(key=lambda t: t[1], reverse=True)
    return {
        "base_score": base_score,
        "sub_scores": sub_scores,
        "factors": [{"factor": f, "points": p} for f, p in factors if p > 0],
        "tax_delinquent_contribution": round(100 * tdw if td_on else 0.0, 4),
    }

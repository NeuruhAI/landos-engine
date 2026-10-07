"""
legacy_factors.py — P-28: the four LEGACY ranking factors. PURE (no DB, no clock).

The original APG ranking dimensions, kept as first-class, individually-explained
factors so legacy rankings stay reproducible next to the newer composite models:

    absentee     distance-graded reachability/disposition proxy — the same tier
                 semantics app/score_absentee classifies, normalized to 0..1
                 (TIER_BASE/100 — one source of truth, never a second table)
    out_of_state the binary legacy flag: mailing state != situs state
    opportunity  opportunity-zone membership (exit/buyer-demand premium)
    tenure       ownership age: years held since last arms-length transfer,
                 ramped linearly 0→1 across TENURE_FLOOR..TENURE_CEIL years —
                 the graded version of the old long_hold boolean

HONESTY RULES: a factor with no derivable evidence scores 0.0 with its reason in
the evidence trail — never a fabricated floor. The clock is INJECTED (`as_of`):
tenure is a function of a date you name, so the same lead always ranks the same
for a given day and tests never read the wall clock.

Weights are configurable with the same typo-proof validation idiom as the Logan
recipe; every ranking ships a deterministic, hash-chained explanation receipt.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from .freshness import _to_date   # module-private, reused read-only (recipe_core idiom)
from .receipt import canonical_json, section_hash
from .score_absentee import TIER_BASE

LEGACY_VERSION = "1.0.0"

LEGACY_FACTORS = ("absentee", "out_of_state", "opportunity", "tenure")

LEGACY_DEFAULT_WEIGHTS: Dict[str, float] = {
    "absentee": 0.35,
    "out_of_state": 0.20,
    "opportunity": 0.20,
    "tenure": 0.25,
}

# tenure ramp: <5 years held contributes nothing; 25+ years is the full legacy
# "long hold" conviction (an owner deep into tenure has equity + detachment).
TENURE_FLOOR_YEARS = 5.0
TENURE_CEIL_YEARS = 25.0


def validate_weights(weights: Optional[Dict[str, float]]) -> Dict[str, float]:
    """Validated weight dict (defaults when None); unknown factors and negative /
    non-numeric values refused loudly (same doctrine as recipe_logan)."""
    if weights is None:
        return dict(LEGACY_DEFAULT_WEIGHTS)
    unknown = sorted(set(weights) - set(LEGACY_FACTORS))
    if unknown:
        raise ValueError(f"unknown legacy factor(s): {unknown}; "
                         f"valid: {sorted(LEGACY_FACTORS)}")
    merged = dict(LEGACY_DEFAULT_WEIGHTS)
    for k, v in weights.items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"weight for {k!r} is not a number: {v!r}")
        if fv < 0:
            raise ValueError(f"weight for {k!r} is negative: {fv}")
        merged[k] = fv
    if sum(merged.values()) <= 0:
        raise ValueError("all legacy weights are zero — nothing to rank")
    return merged


def absentee_factor(absentee_tier: Optional[str]) -> Dict[str, Any]:
    """0..1 from the absentee tier — TIER_BASE/100, one source of truth."""
    tier = absentee_tier or "unknown"
    value = round(TIER_BASE.get(tier, TIER_BASE["unknown"]) / 100.0, 4)
    return {"value": value, "evidence": [f"absentee_tier={tier}"]}


def out_of_state_factor(absentee_tier: Optional[str]) -> Dict[str, Any]:
    hit = absentee_tier == "out_of_state"
    return {"value": 1.0 if hit else 0.0,
            "evidence": ["mailing state != situs state"] if hit else []}


def opportunity_factor(oz_hit: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Opportunity-zone membership. oz_hit is the overlay row (or None) — the same
    shape app/score_absentee.build_signal_overlap consumes."""
    if not oz_hit:
        return {"value": 0.0, "evidence": []}
    ev = ["in_opportunity_zone"]
    if oz_hit.get("name"):
        ev.append(f"oz_tract={oz_hit['name']}")
    return {"value": 1.0, "evidence": ev}


def tenure_factor(acquired_date: Any, as_of: date) -> Dict[str, Any]:
    """Ownership age, ramped linearly TENURE_FLOOR..TENURE_CEIL years -> 0..1.
    acquired_date = the last arms-length transfer / deed date (str or date).
    No derivable date -> honest 0.0 ('tenure unknown'), never a fabricated hold."""
    d = _to_date(acquired_date)
    if d is None:
        return {"value": 0.0, "evidence": ["tenure unknown (no transfer date)"]}
    if d > as_of:
        return {"value": 0.0, "evidence": [f"transfer date {d.isoformat()} is in "
                                           f"the future of as_of — not ranked"]}
    years = (as_of - d).days / 365.25
    unit = (years - TENURE_FLOOR_YEARS) / (TENURE_CEIL_YEARS - TENURE_FLOOR_YEARS)
    value = round(max(0.0, min(1.0, unit)), 4)
    return {"value": value,
            "evidence": [f"held {years:.1f}y since {d.isoformat()}"]}


def legacy_rank(lead: Dict[str, Any], as_of: date,
                oz_hit: Optional[Dict[str, Any]] = None,
                weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Rank one lead on the four legacy factors.

    lead — carries absentee_tier and acquired_date (extras ignored); as_of — the
    injected clock; oz_hit — opportunity-zone overlay row or None.
    Returns {score (0-100), factors:[...], weights_sha256, receipt} with factors
    sorted by points desc then name, and a hash-chained explanation receipt.
    """
    w = validate_weights(weights)
    total_w = sum(w.values())
    computed = {
        "absentee": absentee_factor(lead.get("absentee_tier")),
        "out_of_state": out_of_state_factor(lead.get("absentee_tier")),
        "opportunity": opportunity_factor(oz_hit),
        "tenure": tenure_factor(lead.get("acquired_date"), as_of),
    }
    rows = []
    unit = 0.0
    for f in LEGACY_FACTORS:
        fe = computed[f]
        points = 100.0 * w[f] * fe["value"] / total_w
        unit += w[f] * fe["value"]
        rows.append({"factor": f, "weight": w[f], "value": fe["value"],
                     "evidence": fe["evidence"], "points": round(points, 2)})
    rows.sort(key=lambda r: (-r["points"], r["factor"]))
    score = round(100.0 * unit / total_w, 2)

    weights_sha = section_hash({f: w[f] for f in LEGACY_FACTORS})
    sections = {
        "MODEL": {"model": "legacy_factors", "version": LEGACY_VERSION,
                  "as_of": as_of.isoformat(),
                  "weights": {f: w[f] for f in LEGACY_FACTORS},
                  "weights_sha256": weights_sha,
                  "tenure_ramp_years": [TENURE_FLOOR_YEARS, TENURE_CEIL_YEARS]},
        "FACTORS": {"rows": rows},
        "SCORE": {"score": score,
                  "fired": [r["factor"] for r in rows if r["points"] > 0]},
    }
    order = ("MODEL", "FACTORS", "SCORE")
    hashes = {n: section_hash(sections[n]) for n in order}
    chain = section_hash({"algo": "sha256", "order": list(order),
                          "section_hashes": hashes})
    return {"model": "legacy_factors", "version": LEGACY_VERSION, "score": score,
            "factors": rows, "weights_sha256": weights_sha,
            "receipt": {"sections": {n: {"data": sections[n], "sha256": hashes[n]}
                                     for n in order},
                        "section_hashes": hashes, "chain_sha256": chain}}


def receipt_canonical(result: Dict[str, Any]) -> str:
    return canonical_json(result)

"""
recipe_logan.py — P-25: the LOGAN high-priority recipe. PURE (no DB, no network).

The operator's high-conviction distress stack, as one deterministic, explainable
scorer. Six factor lanes (the charter's list, verbatim):

    tax_delinquency_lien   tax delinquency / tax-sale / recorded lien
    code_violation         municipal code enforcement
    vacant_boarded         vacancy / boarded-up
    physical_distress      windshield-visible physical distress (field pins)
    absentee_out_of_state  absentee ownership, graded by distance tier
    probate_title          probate / estate / tangled-title clouds

Evidence comes from data the spine already holds — discovery-signal source types
plus field-observation tags (signal lanes) and the absentee tier (lead lane). The
mapping is an explicit table (FACTOR_SIGNALS), never a substring guess.

MINT-SAFETY NOTE: 'obituary' is deliberately ABSENT from probate_title's signal
set. An obituary is a review-only lane (P-23, fuzzy identity) — it must not move
a priority score any more than it may mint. Only property-linked probate evidence
counts here.

CONFIGURABLE SCORING: weights are a plain dict over exactly these six factors.
validate_weights() refuses unknown factors and negative values — a typo'd config
fails loudly instead of silently scoring wrong. Score = 100 · Σ(wᵢvᵢ)/Σ(wᵢ).

DETERMINISTIC EXPLANATION RECEIPT: score_logan() returns per-factor evidence
(which signals actually fired, sorted), points, and a hash-chained receipt
(app/receipt idiom) with the weight config fingerprinted — the same lead, signals
and weights always produce byte-identical receipts; change any of them and the
chain hash moves.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set

from .projection.code_vacancy import (
    LOGAN_CODE_VIOLATION_EVIDENCE,
    LOGAN_VACANT_BOARDED_EVIDENCE,
)
from .receipt import canonical_json, section_hash

RECIPE_NAME = "logan_high_priority"
RECIPE_VERSION = "1.0.0"

FACTORS = ("tax_delinquency_lien", "code_violation", "vacant_boarded",
           "physical_distress", "absentee_out_of_state", "probate_title")

# The operator's default emphasis: active tax pipeline first, then the visible-
# distress pair, then probate/title, then reachability. Sum 1.0 (validated anyway).
LOGAN_DEFAULT_WEIGHTS: Dict[str, float] = {
    "tax_delinquency_lien": 0.25,
    "code_violation": 0.15,
    "vacant_boarded": 0.15,
    "physical_distress": 0.15,
    "absentee_out_of_state": 0.10,
    "probate_title": 0.20,
}

# Explicit evidence mapping: signal source_types / observation tags -> factor.
# 'obituary' is intentionally NOT here (review-only lane — see module docstring).
FACTOR_SIGNALS: Dict[str, Set[str]] = {
    "tax_delinquency_lien": {"tax_delinquent", "taxsale", "lien"},
    "code_violation": {"code_violation"} | set(LOGAN_CODE_VIOLATION_EVIDENCE),
    "vacant_boarded": {"vacancy", "vacant", "boarded"} | set(LOGAN_VACANT_BOARDED_EVIDENCE),
    "physical_distress": {"physical_distress", "condemned", "fire_damage",
                          "demolition_order", "field_pin"},
    "probate_title": {"probate", "probate_in", "heirs_of_tangled_title",
                      "estate_sale", "estate_sale_ext", "title_cloud"},
}

# absentee_out_of_state is lead-lane evidence, graded by distance tier (the same
# tiers app/score_absentee classifies) — not a signal-set lookup.
ABSENTEE_TIER_VALUE = {"out_of_state": 1.0, "out_of_county": 0.7, "out_of_town": 0.4}


def validate_weights(weights: Optional[Dict[str, float]]) -> Dict[str, float]:
    """Return a validated weight dict (defaults when None). Unknown factor names and
    negative/non-numeric values are REFUSED — a config typo must fail loudly."""
    if weights is None:
        return dict(LOGAN_DEFAULT_WEIGHTS)
    unknown = sorted(set(weights) - set(FACTORS))
    if unknown:
        raise ValueError(f"unknown Logan factor(s): {unknown}; valid: {sorted(FACTORS)}")
    merged = dict(LOGAN_DEFAULT_WEIGHTS)
    for k, v in weights.items():
        try:
            fv = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"weight for {k!r} is not a number: {v!r}")
        if fv < 0:
            raise ValueError(f"weight for {k!r} is negative: {fv}")
        merged[k] = fv
    if sum(merged.values()) <= 0:
        raise ValueError("all Logan weights are zero — nothing to score")
    return merged


def _factor_evidence(factor: str, types: Set[str],
                     absentee_tier: Optional[str]) -> Dict[str, Any]:
    """(value 0..1, the exact evidence that fired) for one factor. Honest: no
    evidence -> 0.0 with an empty list, never a floor."""
    if factor == "absentee_out_of_state":
        value = ABSENTEE_TIER_VALUE.get(absentee_tier or "", 0.0)
        ev = [f"absentee_tier={absentee_tier}"] if value > 0 else []
        return {"value": value, "evidence": ev}
    hits = sorted(types & FACTOR_SIGNALS[factor])
    return {"value": 1.0 if hits else 0.0, "evidence": hits}


def score_logan(lead: Dict[str, Any], signal_types: Iterable[str],
                weights: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """Score one lead through the Logan recipe.

    lead         — carries absentee_tier (and anything else; extras ignored)
    signal_types — the discovery-signal source_types / observation tags on the parcel
    weights      — optional override, validated (see validate_weights)

    Returns {recipe, version, score, factors:[...], weights_sha256, receipt}.
    factors are sorted by points desc then name — a stable, explainable ordering.
    """
    w = validate_weights(weights)
    types = {str(t) for t in (signal_types or []) if t}
    total_w = sum(w.values())

    rows: List[Dict[str, Any]] = []
    unit = 0.0
    for f in FACTORS:
        fe = _factor_evidence(f, types, lead.get("absentee_tier"))
        points = 100.0 * w[f] * fe["value"] / total_w
        unit += w[f] * fe["value"]
        rows.append({"factor": f, "weight": w[f], "value": fe["value"],
                     "evidence": fe["evidence"], "points": round(points, 2)})
    rows.sort(key=lambda r: (-r["points"], r["factor"]))
    score = round(100.0 * unit / total_w, 2)

    weights_sha = section_hash({f: w[f] for f in FACTORS})
    sections = {
        "RECIPE": {"recipe": RECIPE_NAME, "version": RECIPE_VERSION,
                   "weights": {f: w[f] for f in FACTORS},
                   "weights_sha256": weights_sha},
        "EVIDENCE": {"signal_types": sorted(types),
                     "absentee_tier": lead.get("absentee_tier"),
                     "factors": rows},
        "SCORE": {"score": score,
                  "fired": [r["factor"] for r in rows if r["points"] > 0]},
    }
    order = ("RECIPE", "EVIDENCE", "SCORE")
    hashes = {n: section_hash(sections[n]) for n in order}
    chain = section_hash({"algo": "sha256", "order": list(order),
                          "section_hashes": hashes})
    return {
        "recipe": RECIPE_NAME,
        "version": RECIPE_VERSION,
        "score": score,
        "factors": rows,
        "weights_sha256": weights_sha,
        "receipt": {"sections": {n: {"data": sections[n], "sha256": hashes[n]}
                                 for n in order},
                    "section_hashes": hashes, "chain_sha256": chain},
    }


def receipt_canonical(result: Dict[str, Any]) -> str:
    """Canonical JSON of a score result — what two runs diff byte-for-byte."""
    return canonical_json(result)

"""
composite_inputs.py — the BATCH input-fact assembler for the composite scorer.

PURE (no DB, no network), like scoring.py and comps.py — so it's trivially
testable and the "what counts as grounded" logic has ONE home, shared by the
audit (scripts/audit_composite_coverage.py) and the runner
(scripts/score_composite_batch.py).

It turns a parcel's RAW engine facts into the exact `inputs` dict that
`app.scoring.score_parcel` consumes — PLUS a coverage record that makes MISSING
a first-class outcome. The HARD RULE this enforces:

    Never fabricate or substitute inputs. An axis with no inputs (or whose only
    inputs the model has no slot for) yields a NULL / low-confidence component,
    NOT a 0. 0 is a *guess* ("we looked and there's no distress"); these parcels
    were never looked at on that axis.

Three per-axis states (`status`):
  • "grounded"            — real, modeled inputs exist → component is computed.
  • "grounded_unmodeled"  — a real signal exists but the composite has no slot
                            for it (e.g. environmental_part201) → component NULL,
                            the signal is recorded so we never claim "none".
  • "absent"              — no inputs at all → component NULL.

`score_parcel` itself reads any absent key as falsy → 0. So the COMPOSITE score
we keep is a conservative FLOOR: unknown axes contribute nothing. We do NOT
renormalize weights over the present axes (that would let one grounded axis
masquerade as a full score). Partiality is carried explicitly in `coverage` +
`confidence`, never hidden in the number.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


# Engine distress token  ->  composite distress key (app.scoring DEFAULT_WEIGHTS["distress"]).
# Only EXACT-or-family matches belong here. Anything not in this map is treated
# as grounded-but-unmodeled (recorded, but it cannot move the modeled component).
DISTRESS_TOKEN_MAP: Dict[str, str] = {
    "tax_delinquent": "tax_delinquent",   # exact
    "foreclosure": "pre_foreclosure",     # family map (engine emits 'foreclosure'); flagged in coverage
    "environmental_part201": "environmental",  # MI Part 201 contamination -> 'environmental' (family map)
    "brownfield": "environmental",             # brownfield -> 'environmental' (family map)
    "probate": "probate",                 # from discovery_signals (source_type probate_in -> 'probate')
    "probate_in": "probate",
    # future engine tokens that already have a model slot:
    "code_violation": "code_violation",
    "vacancy": "vacancy",
    "long_hold": "long_hold",
    "pre_foreclosure": "pre_foreclosure",
}

# Composite owner sub-signals that NO current engine field can observe.
# Recorded as unobserved so the owner component is never read as "confirmed-negative".
OWNER_UNOBSERVABLE = ("age_senior", "age_boomer")  # no age/DOB source on owner_leads


def _owner_weight_total(owner_weights: Dict[str, float]) -> float:
    return sum(float(v) for v in owner_weights.values()) or 1.0


def assemble(lead: Dict[str, Any], weights: Dict[str, Any]) -> Dict[str, Any]:
    """
    lead — normalized RAW facts for one parcel (the runner builds this from DB):
        absentee_tier   : str | None     # out_of_state|out_of_county|out_of_town|owner_occupied|unknown|...
        owner_type      : str | None     # individual|entity|govt
        skip_traced     : bool           # status in (traced_web, traced_paid)
        is_deceased     : bool | None    # from skip_trace_meta.deceased; None = unknown
        distress_tokens : list[str]      # union of signal_overlap.distress_stack + discovery_signal types
        acreage         : float | None
        market_ppa      : float | None   # market $/acre from comps (None when comps absent)
        list_ppa        : float | None   # list $/acre (None when no listing)
        comps_n         : int            # number of usable comps backing market_ppa

    weights — the ACTIVE weights dict (same one score_parcel will use), so the
              owner observability fraction is computed against the live weights.

    Returns:
        {
          "inputs":    {distress, owner, arbitrage},   # ONLY grounded-modeled facts -> score_parcel
          "coverage":  {owner, distress, arbitrage},   # per-axis status + observed/missing detail
          "null_components": {distress, owner, arbitrage},  # True => store NULL (not the floor 0)
          "confidence": float,                          # axis-weighted grounded-modeled fraction (0..1)
        }
    """
    cw = weights.get("composite", {})
    ow = weights.get("owner", {})

    # ---------------- OWNER axis ----------------------------------------------
    absentee_tier = (lead.get("absentee_tier") or "").strip().lower()
    owner_type = (lead.get("owner_type") or "").strip().lower()
    tier_known = bool(absentee_tier) and absentee_tier not in ("unknown", "unknown_situs")
    type_known = owner_type in ("individual", "entity", "govt")

    owner_inputs: Dict[str, Any] = {}
    observed: List[str] = []
    unobserved: List[str] = list(OWNER_UNOBSERVABLE)

    # out_of_state — grounded whenever the absentee tier is known (true or false).
    if tier_known:
        owner_inputs["out_of_state"] = (absentee_tier == "out_of_state")
        observed.append("out_of_state")
    else:
        unobserved.append("out_of_state")

    # is_entity — grounded whenever owner_type is known. govt is NOT the LLC/corp
    # motivation the weight intends, so is_entity is True only for 'entity'.
    if type_known:
        owner_inputs["is_entity"] = (owner_type == "entity")
        observed.append("is_entity")
    else:
        unobserved.append("is_entity")

    # no_succession — only observable when we actually know living/deceased status.
    is_deceased = lead.get("is_deceased")
    if is_deceased is True:
        owner_inputs["no_succession"] = True
        observed.append("no_succession")
    elif lead.get("skip_traced") and is_deceased is False:
        owner_inputs["no_succession"] = False
        observed.append("no_succession")
    else:
        unobserved.append("no_succession")

    # age_band deliberately omitted — no source. (Stays "other"/0 inside score_parcel,
    # but we record it unobserved so the 0 is never read as "confirmed not-senior".)

    owner_obs_weight = sum(
        float(ow.get(k, 0.0)) for k in (
            ["out_of_state"] if "out_of_state" in observed else []
        ) + (["is_entity"] if "is_entity" in observed else [])
        + (["no_succession"] if "no_succession" in observed else [])
    )
    owner_fraction = owner_obs_weight / _owner_weight_total(ow)
    owner_status = "grounded" if observed else "absent"
    owner_coverage = {
        "status": owner_status,
        "observed_subsignals": observed,
        "unobserved_subsignals": unobserved,
        "observed_fraction": round(owner_fraction, 4),
        "facts": dict(owner_inputs),
    }

    # ---------------- DISTRESS axis -------------------------------------------
    tokens = [str(t).strip().lower() for t in (lead.get("distress_tokens") or []) if t]
    distress_inputs: Dict[str, bool] = {}
    modeled: List[str] = []
    unmodeled: List[str] = []
    dw = weights.get("distress", {})
    for t in tokens:
        key = DISTRESS_TOKEN_MAP.get(t)
        # "modeled" only if the ACTIVE weights actually score this key — a token
        # mapped to a key the current profile lacks stays unmodeled (NULL), never
        # silently grounded-but-0. (environmental: unmodeled under v2, modeled under v3.)
        if key and key in dw:
            distress_inputs[key] = True
            modeled.append(t)
        else:
            unmodeled.append(t)

    if modeled:
        distress_status = "grounded"
    elif unmodeled:
        distress_status = "grounded_unmodeled"   # real signal, no model slot -> NULL component
    else:
        distress_status = "absent"
    distress_coverage = {
        "status": distress_status,
        "present_tokens": tokens,
        "modeled_tokens": modeled,
        "unmodeled_tokens": unmodeled,
        "facts": dict(distress_inputs),
    }

    # ---------------- ARBITRAGE axis ------------------------------------------
    market_ppa = lead.get("market_ppa")
    list_ppa = lead.get("list_ppa")
    comps_n = int(lead.get("comps_n") or 0)
    arb_inputs: Dict[str, Any] = {}
    arb_present: List[str] = []
    arb_missing: List[str] = []
    if market_ppa and comps_n > 0:
        arb_inputs["market_price_per_acre"] = float(market_ppa)
        arb_present.append("market_price_per_acre")
    else:
        arb_missing.append("market_price_per_acre")
    if list_ppa:
        arb_inputs["list_price_per_acre"] = float(list_ppa)
        arb_present.append("list_price_per_acre")
    else:
        arb_missing.append("list_price_per_acre")
    if lead.get("acreage"):
        arb_inputs["acreage"] = float(lead["acreage"])
        arb_present.append("acreage")
    else:
        arb_missing.append("acreage")
    # The discount term needs BOTH market and list price; without them the axis
    # cannot produce a real arbitrage component (acreage alone is not enough).
    arb_scorable = "market_price_per_acre" in arb_present and "list_price_per_acre" in arb_present
    arb_status = "grounded" if arb_scorable else "absent"
    arbitrage_coverage = {
        "status": arb_status,
        "present_inputs": arb_present,
        "missing_inputs": arb_missing,
        "comps_n": comps_n,
        "facts": {k: v for k, v in arb_inputs.items()},
    }
    if not arb_scorable:
        arb_inputs = {k: v for k, v in arb_inputs.items() if k == "acreage"}  # acreage harmless, scores 0

    # ---------------- null-component map + confidence -------------------------
    null_components = {
        "owner": owner_status == "absent",
        "distress": distress_status in ("grounded_unmodeled", "absent"),
        "arbitrage": arb_status == "absent",
    }
    # axis-weighted grounded-modeled fraction:
    #   owner contributes its OBSERVED fraction; distress/arbitrage contribute
    #   1.0 only when modeled-grounded, else 0.
    conf = (
        float(cw.get("owner", 0.0)) * (owner_fraction if owner_status == "grounded" else 0.0)
        + float(cw.get("distress", 0.0)) * (1.0 if distress_status == "grounded" else 0.0)
        + float(cw.get("arbitrage", 0.0)) * (1.0 if arb_status == "grounded" else 0.0)
    )

    return {
        "inputs": {
            "distress": distress_inputs,
            "owner": owner_inputs,
            "arbitrage": arb_inputs,
        },
        "coverage": {
            "owner": owner_coverage,
            "distress": distress_coverage,
            "arbitrage": arbitrage_coverage,
        },
        "null_components": null_components,
        "confidence": round(conf, 4),
    }

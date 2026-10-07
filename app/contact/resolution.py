"""Explicit contact-resolution state for every CURRENT NC candidate.

PHONE PRESENT ≠ CALLABLE. A HIGH append is an intelligence state, not
authorization to SMS, email, or dial.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

from .owner_class import owner_route_class
from .owner_identity import parse_owner_name
from .product_export import unresolved_reason

PHONE_APPENDED_HIGH = "PHONE_APPENDED_HIGH"

RESOLUTION_STATES = (
    PHONE_APPENDED_HIGH,
    "IDENTITY_MATCHED_VOTER_PHONE_ABSENT",
    "TIER_C_HELD_NOT_APPENDED",
    "AMBIGUOUS_REJECTED",
    "COMMON_NAME_REJECTED",
    "NO_ADDRESS_CORROBORATED_NCSBE_MATCH",
    "DECEASED_OWNER_NEEDS_HEIR_IDENTITY",
    "HEIR_ON_ROLL_NO_ACCEPTED_PHONE",
    "ENTITY_NEEDS_LAWFUL_SOS_IMPORT",
    "NONPROFIT_NO_990_PHONE",
    "TRUST_EXCLUDED_FROM_VOTER_PERSON_MATCH",
    "TRUST_NEEDS_TRUSTEE_IDENTITY",
    "TRUSTEE_ON_ROLL_NO_ACCEPTED_PHONE",
    "GOVERNMENT_NOT_A_SELLER_CONTACT",
    "UNRESOLVED",
)

CALLABILITY_NOT_AUTHORIZED = "NOT_AUTHORIZED"

RESOLUTION_COLUMNS = (
    "candidate_id",
    "county",
    "fips",
    "apn",
    "owner_name",
    "owner_class",
    "qualification_state",
    "primary_recipe",
    "score",
    "global_rank",
    "contact_resolution_state",
    "phone_present",
    "why_matched",
    "phone_source",
    "phone_role",
    "matched_person",
    "observed_at",
    "confidence_category",
    "confidence_score",
    "identity_tuple_hash",
    "dnc_status",
    "callability_status",
)


def prioritize_estate(estate: List[dict]) -> List[dict]:
    """Run order only. Match outcomes stay per-candidate deterministic."""

    def key(row: dict) -> tuple:
        qual = str(row.get("qualification_state") or "")
        qualified = 0 if qual.startswith("QUALIFIED") else 1
        try:
            score = -float(row.get("score") or 0)
        except (TypeError, ValueError):
            score = 0.0
        try:
            rank = int(float(row.get("global_rank") or 10**9))
        except (TypeError, ValueError):
            rank = 10**9
        return (qualified, score, rank, str(row.get("candidate_id") or ""))

    return sorted(estate, key=key)


def _dnc(row: dict) -> str:
    dnc = row.get("dnc_status") or "UNKNOWN_NOT_EVALUATED"
    if dnc in {"", "UNKNOWN"}:
        return "UNKNOWN_NOT_EVALUATED"
    return dnc


def classify_resolution_state(
    *,
    owner_class: str,
    has_high_phone: bool,
    identity_matched: bool = False,
    held_tier_c: bool = False,
    ambiguous: bool = False,
    common_name: bool = False,
    prior_reason: str = "",
    row: Optional[dict] = None,
) -> str:
    if has_high_phone:
        return PHONE_APPENDED_HIGH
    if held_tier_c:
        return "TIER_C_HELD_NOT_APPENDED"
    if ambiguous:
        return "AMBIGUOUS_REJECTED"
    if common_name:
        return "COMMON_NAME_REJECTED"
    reason = (prior_reason or "").strip()
    if reason in RESOLUTION_STATES and reason != PHONE_APPENDED_HIGH:
        return reason
    if identity_matched:
        return "IDENTITY_MATCHED_VOTER_PHONE_ABSENT"
    computed = unresolved_reason(row or {}, owner_class, identity_matched)
    if computed in RESOLUTION_STATES:
        return computed
    return computed or "UNRESOLVED"


def build_resolution_ledger(
    estate: List[dict],
    owner_class: Dict[str, str],
    phones_by_cand: Dict[str, List[dict]],
    identity_tuple_hash,
    *,
    identity_cands: Optional[Set[str]] = None,
    held_tier_c: Optional[Set[str]] = None,
    ambiguous: Optional[Set[str]] = None,
    prior_reasons: Optional[Dict[str, str]] = None,
) -> List[dict]:
    identity_cands = identity_cands or set()
    held_tier_c = held_tier_c or set()
    ambiguous = ambiguous or set()
    prior_reasons = prior_reasons or {}
    out: List[dict] = []
    for row in estate:
        cid = row["candidate_id"]
        recs = [r for r in phones_by_cand.get(cid, []) if r.get("confidence_category") == "HIGH" or r.get("phone")]
        high = [r for r in recs if (r.get("confidence_category") or "HIGH") == "HIGH" and r.get("phone")]
        top = high[0] if high else {}
        oc = owner_class.get(cid) or owner_route_class(row.get("owner_name") or "")
        state = classify_resolution_state(
            owner_class=oc,
            has_high_phone=bool(high),
            identity_matched=cid in identity_cands,
            held_tier_c=cid in held_tier_c,
            ambiguous=cid in ambiguous,
            prior_reason=prior_reasons.get(cid, ""),
            row=row,
        )
        dnc = _dnc(row)
        out.append({
            "candidate_id": cid,
            "county": row.get("county") or "",
            "fips": row.get("fips") or "",
            "apn": row.get("apn") or "",
            "owner_name": row.get("owner_name") or "",
            "owner_class": oc,
            "qualification_state": row.get("qualification_state") or "",
            "primary_recipe": row.get("primary_recipe") or "",
            "score": row.get("score") or "",
            "global_rank": row.get("global_rank") or "",
            "contact_resolution_state": state,
            "phone_present": "YES" if high else "NO",
            "why_matched": top.get("why_matched") or top.get("match_tier") or "",
            "phone_source": top.get("phone_source") or "",
            "phone_role": top.get("phone_role") or "",
            "matched_person": top.get("matched_person") or "",
            "observed_at": top.get("observed_at") or top.get("source_observed_at") or "",
            "confidence_category": top.get("confidence_category") or "",
            "confidence_score": top.get("confidence_score") or "",
            "identity_tuple_hash": identity_tuple_hash(row),
            "dnc_status": dnc if dnc in {"DNC_TRUE_CRM", "DNC_FALSE_CRM"} else "UNKNOWN",
            "callability_status": CALLABILITY_NOT_AUTHORIZED,
        })
    return out


def owner_class_for_estate(estate: Iterable[dict]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for row in estate:
        parsed = parse_owner_name(row.get("owner_name") or "")
        out[row["candidate_id"]] = owner_route_class(row.get("owner_name") or "", parsed)
    return out

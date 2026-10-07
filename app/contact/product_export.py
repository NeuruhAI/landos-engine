"""Founder product lists. PHONE PRESENT ≠ CALLABLE. DNC stays UNKNOWN unless proven."""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

COUNTY_ORDER = ["Mecklenburg", "Wake", "Durham", "Johnston", "Union", "Cabarrus", "Gaston"]

PRODUCT_COLUMNS = [
    "candidate_id", "apn", "county", "fips", "owner_name", "owner_class",
    "property_address_raw", "mailing_address", "mailing_city", "mailing_state", "mailing_zip",
    "score", "global_rank", "primary_recipe", "qualification_state",
    "phone_1", "phone_1_role", "phone_1_source", "phone_1_confidence",
    "phone_2", "phone_2_role", "phone_2_source", "phone_2_confidence",
    "phone_3", "phone_3_role", "phone_3_source", "phone_3_confidence",
    "phone_present", "contact_resolution_state", "dnc_status", "callability_status",
]

MISSING_COLUMNS = [
    "candidate_id", "apn", "county", "fips", "owner_name", "owner_class",
    "property_address_raw", "mailing_address", "qualification_state",
    "primary_recipe", "global_rank", "score",
    "resolution_state", "unresolved_reason",
    "attempted_sources", "last_attempted_at", "next_safe_free_route",
]

# What a $0 operator can lawfully try next, per reason. "" means there is no
# remaining free route and the candidate is a paid-tier decision, not a bug.
NEXT_SAFE_FREE_ROUTE = {
    "DECEASED_OWNER_NEEDS_HEIR_IDENTITY":
        "ESTATE_IMPORT: clerk-of-court estate file -> living heir/representative, then NCSBE",
    "HEIR_ON_ROLL_NO_ACCEPTED_PHONE":
        "NCSBE: widen to co-owner/heir siblings at the same mailing address",
    "ENTITY_NEEDS_LAWFUL_SOS_IMPORT":
        "SOS_IMPORT: operator-supplied NC SOS listing -> registered agent/officer, then NCSBE",
    "NONPROFIT_NO_990_PHONE":
        "IRS_FORM_990_PUBLIC: pull the EIN's latest 990 XML for a filed contact",
    "TRUST_EXCLUDED_FROM_VOTER_PERSON_MATCH":
        "ESTATE_IMPORT/SOS_IMPORT: identify the trustee first; never voter-match a trust name",
    "GOVERNMENT_NOT_A_SELLER_CONTACT": "",
    "NO_ADDRESS_CORROBORATED_NCSBE_MATCH":
        "NCSBE: re-run after address normalization improves, or corroborate via county parcel mailing",
    "IDENTITY_MATCHED_VOTER_PHONE_ABSENT":
        "NONE_FREE: identity is known but the voter record carries no phone",
    "TIER_C_HELD_NOT_APPENDED":
        "PRECISION_COURT: TIER_C stays held until precision >= 0.98 on n >= 40",
    "AMBIGUOUS_REJECTED":
        "NCSBE: needs a second corroborating identity field before any append",
    "COMMON_NAME_REJECTED":
        "NCSBE: needs mailing-address corroboration to separate the common name",
    "UNRESOLVED": "TRIAGE: classify the owner string first",
}


def next_safe_free_route(reason: str) -> str:
    return NEXT_SAFE_FREE_ROUTE.get(reason, "TRIAGE: no mapped free route for this reason")


def _dnc(row: dict) -> str:
    dnc = row.get("dnc_status") or "UNKNOWN_NOT_EVALUATED"
    if dnc in {"", "UNKNOWN"}:
        return "UNKNOWN_NOT_EVALUATED"
    return dnc


def overlay_product_rows(
    estate: List[dict],
    phones_by_cand: Dict[str, List[dict]],
    owner_class: Dict[str, str],
    resolution_by_cand: Optional[Dict[str, str]] = None,
) -> List[dict]:
    out = []
    resolution_by_cand = resolution_by_cand or {}
    for row in estate:
        recs = [r for r in phones_by_cand.get(row["candidate_id"], []) if r.get("confidence_category") == "HIGH"]
        # stable: TIER_A first then existing order
        recs = recs[:3]
        slots = []
        for rec in recs:
            slots.append((
                rec.get("phone") or "",
                rec.get("phone_role") or rec.get("person_role") or "",
                rec.get("phone_source") or "",
                rec.get("confidence_category") or "",
            ))
        while len(slots) < 3:
            slots.append(("", "", "", ""))
        has = bool(slots[0][0])
        dnc = _dnc(row)
        oc = owner_class.get(row["candidate_id"], "")
        state = resolution_by_cand.get(row["candidate_id"]) or (
            "PHONE_APPENDED_HIGH" if has else unresolved_reason(row, oc, False)
        )
        out.append({
            "candidate_id": row["candidate_id"],
            "apn": row.get("apn") or "",
            "county": row.get("county") or "",
            "fips": row.get("fips") or "",
            "owner_name": row.get("owner_name") or "",
            "owner_class": oc,
            "property_address_raw": row.get("property_address_raw") or "",
            "mailing_address": row.get("mailing_address") or "",
            "mailing_city": row.get("mailing_city") or "",
            "mailing_state": row.get("mailing_state") or "",
            "mailing_zip": row.get("mailing_zip") or "",
            "score": row.get("score") or "",
            "global_rank": row.get("global_rank") or "",
            "primary_recipe": row.get("primary_recipe") or "",
            "qualification_state": row.get("qualification_state") or "",
            "phone_1": slots[0][0], "phone_1_role": slots[0][1], "phone_1_source": slots[0][2], "phone_1_confidence": slots[0][3],
            "phone_2": slots[1][0], "phone_2_role": slots[1][1], "phone_2_source": slots[1][2], "phone_2_confidence": slots[1][3],
            "phone_3": slots[2][0], "phone_3_role": slots[2][1], "phone_3_source": slots[2][2], "phone_3_confidence": slots[2][3],
            "phone_present": "YES" if has else "NO",
            "contact_resolution_state": state,
            "dnc_status": dnc if dnc in {"DNC_TRUE_CRM", "DNC_FALSE_CRM"} else "UNKNOWN",
            "callability_status": "NOT_AUTHORIZED",
        })
    return out


def unresolved_reason(row: dict, owner_class: str, has_identity: bool) -> str:
    if owner_class in {"DECEASED", "ESTATE"}:
        return "DECEASED_OWNER_NEEDS_HEIR_IDENTITY"
    if owner_class == "HEIRS":
        return "HEIR_ON_ROLL_NO_ACCEPTED_PHONE"
    if owner_class in {"LLC", "CORPORATION", "OTHER_ENTITY"}:
        return "ENTITY_NEEDS_LAWFUL_SOS_IMPORT"
    if owner_class == "NONPROFIT":
        return "NONPROFIT_NO_990_PHONE"
    if owner_class == "TRUST":
        # A trust whose trustee the roll names, and whom we matched on the voter
        # file, is a different state from a trust with no identifiable trustee.
        from .owner_identity import trustee_people_on_roll
        if has_identity:
            return "TRUSTEE_ON_ROLL_NO_ACCEPTED_PHONE"
        if trustee_people_on_roll(row.get("owner_name") or ""):
            return "TRUST_EXCLUDED_FROM_VOTER_PERSON_MATCH"
        return "TRUST_NEEDS_TRUSTEE_IDENTITY"
    if owner_class == "GOVERNMENT":
        return "GOVERNMENT_NOT_A_SELLER_CONTACT"
    if has_identity:
        return "IDENTITY_MATCHED_VOTER_PHONE_ABSENT"
    if owner_class in {"LIVING_PERSON", "MULTI_PERSON"}:
        return "NO_ADDRESS_CORROBORATED_NCSBE_MATCH"
    return "UNRESOLVED"


def write_product_suite(
    estate: List[dict],
    phones_by_cand: Dict[str, List[dict]],
    owner_class: Dict[str, str],
    identity_cands: set,
    out_dir: Path,
    write_csv,
    resolution_by_cand: Optional[Dict[str, str]] = None,
    attempted_by_cand: Optional[Dict[str, str]] = None,
    attempted_default: str = "",
    last_attempted_at: str = "",
) -> dict:
    product = overlay_product_rows(estate, phones_by_cand, owner_class, resolution_by_cand)
    files = {}
    files["master"] = write_csv(out_dir / "MASTER_CURRENT_WITH_FREE_CONTACTS.csv", product, PRODUCT_COLUMNS)
    by_county = defaultdict(list)
    for row in product:
        by_county[row["county"]].append(row)
    county_files = []
    for county in COUNTY_ORDER:
        county_files.append(write_csv(
            out_dir / f"{county.upper()}_CURRENT_WITH_CONTACTS.csv",
            by_county.get(county, []),
            PRODUCT_COLUMNS,
        ))
    files["counties"] = county_files
    qualified = [r for r in product if (r.get("qualification_state") or "").startswith("QUALIFIED")]
    files["qualified"] = write_csv(out_dir / "QUALIFIED_306_WITH_FREE_CONTACTS.csv", qualified, PRODUCT_COLUMNS)
    missing = []
    resolution_by_cand = resolution_by_cand or {}
    attempted_by_cand = attempted_by_cand or {}
    for row, prod in zip(estate, product):
        if prod["phone_present"] == "YES":
            continue
        oc = owner_class.get(row["candidate_id"], "")
        reason = unresolved_reason(row, oc, row["candidate_id"] in identity_cands)
        state = resolution_by_cand.get(row["candidate_id"]) or reason
        missing.append({
            "candidate_id": row["candidate_id"],
            "apn": row.get("apn") or "",
            "county": row.get("county") or "",
            "fips": row.get("fips") or "",
            "owner_name": row.get("owner_name") or "",
            "owner_class": oc,
            "property_address_raw": row.get("property_address_raw") or "",
            "mailing_address": row.get("mailing_address") or "",
            "qualification_state": row.get("qualification_state") or "",
            "primary_recipe": row.get("primary_recipe") or "",
            "global_rank": row.get("global_rank") or "",
            "score": row.get("score") or "",
            "resolution_state": state,
            "unresolved_reason": reason,
            "attempted_sources": attempted_by_cand.get(row["candidate_id"], "") or attempted_default,
            "last_attempted_at": last_attempted_at,
            "next_safe_free_route": next_safe_free_route(state) or next_safe_free_route(reason),
        })
    files["missing"] = write_csv(out_dir / "CURRENT_STILL_MISSING_PHONE.csv", missing, MISSING_COLUMNS)
    q_missing = [r for r in missing if (r.get("qualification_state") or "").startswith("QUALIFIED")]
    files["qualified_missing"] = write_csv(out_dir / "QUALIFIED_STILL_MISSING_PHONE.csv", q_missing, MISSING_COLUMNS)
    return files

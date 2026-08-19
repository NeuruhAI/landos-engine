"""T12 phone-append contract, ported from the sealed TypeScript evaluator."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .phone_normalize import normalize_us_phone

PHONE_APPEND_REJECT_REASONS = (
    "CANDIDATE_MISMATCH",
    "APN_MISMATCH",
    "COUNTY_FIPS_MISMATCH",
    "AMBIGUOUS_IDENTITY",
    "MALFORMED_NUMBER",
    "SYNTHETIC_555",
    "MISSING_PHONE",
    "NAME_ONLY_WHEN_STRONG_FIELDS_DISAGREE",
    "DECEASED_TARGET",
    "ENTITY_MISMATCH",
    "LOW_CONFIDENCE",
    "INVALID_PHONE",
)

REQUIRED_APPEND_COLUMNS = (
    "candidate_id",
    "apn",
    "phone",
    "county",
    "fips",
)

OPTIONAL_APPEND_COLUMNS = (
    "source_row_id",
    "owner_name",
    "phone_source",
    "provider_reference",
    "observed_at",
)

# Extra provenance columns allowed alongside the T12 contract.
PROVENANCE_COLUMNS = (
    "match_tier",
    "match_confidence",
    "source_record_identity",
    "source_observed_at",
    "source_hash",
    "phone_number_type",
    "numbering_plan_valid",
    "currently_assigned",
    "right_party",
    "callable_claim",
    "person_role",
    "phone_role",
    "relationship",
    "estate_case",
    "heir_person_id",
    "owner_class",
    "confidence_category",
    "confidence_score",
    "source_count",
    "source_list",
    "source_age_days",
    "fanout_candidates",
    "why_matched",
    "matched_person",
    "primary_recipe",
    "score",
    "global_rank",
    "qualification_state",
)

# WHO / WHY / SOURCE / OBSERVED_AT / CONFIDENCE / PHONE ROLE / CANDIDATE / RECIPE / SCORE/RANK
CONTACT_PROVENANCE_FIELDS = (
    "matched_person",
    "why_matched",
    "phone_source",
    "observed_at",
    "confidence_category",
    "phone_role",
    "candidate_id",
    "primary_recipe",
    "score",
    "global_rank",
)

COMBINED_COLUMNS = REQUIRED_APPEND_COLUMNS + OPTIONAL_APPEND_COLUMNS + PROVENANCE_COLUMNS


def _norm(value: Optional[str]) -> str:
    return str(value or "").strip().upper()


def apn_equal(a: str, b: str) -> bool:
    left = _norm(a).lstrip("0") or "0"
    right = _norm(b).lstrip("0") or "0"
    return left == right


@dataclass(frozen=True)
class PhoneAppendIdentity:
    candidate_id: str
    source_row_id: str
    apn: str
    fips: str
    county: str
    owner_name: str


def evaluate_phone_append(
    existing: PhoneAppendIdentity,
    input_row: dict,
    ambiguous: bool = False,
    deceased_target: bool = False,
    low_confidence: bool = False,
    entity_mismatch: bool = False,
) -> dict:
    if deceased_target:
        return {"ok": False, "reason": "DECEASED_TARGET"}
    if entity_mismatch:
        return {"ok": False, "reason": "ENTITY_MISMATCH"}
    if low_confidence:
        return {"ok": False, "reason": "LOW_CONFIDENCE"}
    if ambiguous:
        return {"ok": False, "reason": "AMBIGUOUS_IDENTITY"}
    phone = normalize_us_phone(input_row.get("phone"))
    if phone.classification == "MISSING":
        return {"ok": False, "reason": "MISSING_PHONE"}
    if phone.classification == "SYNTHETIC_555":
        return {"ok": False, "reason": "SYNTHETIC_555"}
    if phone.classification in {"MALFORMED", "INVALID"} or not phone.e164:
        return {"ok": False, "reason": "MALFORMED_NUMBER"}
    cand = str(input_row.get("candidate_id") or "").strip()
    if not cand or cand != existing.candidate_id:
        return {"ok": False, "reason": "CANDIDATE_MISMATCH"}
    apn_in = str(input_row.get("apn") or "").strip()
    if not apn_in or not apn_equal(apn_in, existing.apn):
        return {"ok": False, "reason": "APN_MISMATCH"}
    fips_in = "".join(ch for ch in str(input_row.get("fips") or "") if ch.isdigit())
    county_in = _norm(input_row.get("county"))
    fips_ok = (not fips_in) or fips_in == existing.fips
    county_ok = (not county_in) or county_in == _norm(existing.county)
    if not fips_ok or not county_ok:
        return {"ok": False, "reason": "COUNTY_FIPS_MISMATCH"}
    source_row_in = str(input_row.get("source_row_id") or "").strip()
    if source_row_in and source_row_in != existing.source_row_id:
        return {"ok": False, "reason": "CANDIDATE_MISMATCH"}
    strong_disagree = (not fips_ok) or (not county_ok) or (not apn_equal(apn_in, existing.apn))
    name_in = str(input_row.get("owner_name") or "").strip()
    if strong_disagree and name_in and _norm(name_in) == _norm(existing.owner_name):
        return {"ok": False, "reason": "NAME_ONLY_WHEN_STRONG_FIELDS_DISAGREE"}
    return {
        "ok": True,
        "candidate_id": existing.candidate_id,
        "e164": phone.e164,
        "phone_source": str(input_row.get("phone_source") or "").strip() or "append",
        "provider_reference": str(input_row.get("provider_reference") or "").strip(),
        "observed_at": str(input_row.get("observed_at") or "").strip(),
        "match_confidence": "STRONG_IDENTITY",
    }


def source_row_id_for(fips: str, apn: str) -> str:
    return f"NC:{fips}:{apn}"


def validate_append_row(row: dict, identity: PhoneAppendIdentity) -> dict:
    missing = [c for c in REQUIRED_APPEND_COLUMNS if not str(row.get(c) or "").strip()]
    if missing:
        return {"ok": False, "reason": "MISSING_REQUIRED", "fields": missing}
    return evaluate_phone_append(identity, row)

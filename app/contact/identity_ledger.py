"""Every matched living identity, not only the ones that happened to carry a phone.

The matcher proves an identity for 6,652 CURRENT candidates. Until now only the
937 whose voter record also carried a phone survived into any artifact: the
other ~5,700 collapsed into the single string
"IDENTITY_MATCHED_VOTER_PHONE_ABSENT" and the person's NAME was discarded.

That is the difference between "we could not reach this parcel" and "this parcel
belongs to a named living person we can write to". Wake alone holds thousands of
these -- its voter file has a 0.0% phone fill rate, so EVERY Wake identity the
engine proves lands in that bucket and every one of those names was being thrown
away.

This module is a pure export. It re-matches nothing, changes no threshold, and
adds no source. It writes down what the matcher already decided.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

IDENTITY_LEDGER_VERSION = "identity_ledger.v1"

IDENTITY_LEDGER_COLUMNS = [
    "candidate_id",
    "apn",
    "county",
    "fips",
    "owner_name",
    "matched_person",
    "person_role",
    "relationship",
    "match_tier",
    "match_confidence",
    "why_matched",
    "identity_source",
    "source_record_identity",
    "source_hash",
    "observed_at",
    "voter_status",
    "residence_city",
    "residence_zip",
    "phone_present",
    "phone_absent_reason",
    "callability_status",
]

# person_role on the roll -> the relationship the person has to the parcel.
RELATIONSHIP_BY_ROLE = {
    "OWNER": "RECORDED_OWNER",
    "COOWNER": "COOWNER",
    "LIFE_TENANT": "LIFE_TENANT",
    "NAMED_HEIR_ON_ROLL": "HEIR",
    "NAMED_TRUSTEE_ON_ROLL": "TRUSTEE",
    "DECEDENT": "DECEDENT",
}


def _u(value: Optional[str]) -> str:
    return (value or "").strip()


def identity_rows(
    identity_hits: Iterable[tuple],
    *,
    identity_source: str = "NCSBE_PUBLIC_VOTER_FILE",
) -> List[dict]:
    """Flatten (estate_row, PersonName, MatchDecision) triples into ledger rows.

    `phone_absent_reason` distinguishes the two ways a proven identity yields no
    number, which is exactly the distinction the next-source router needs:
    the county publishes no phone column at all, versus this particular voter
    record has the field empty.
    """
    out: List[dict] = []
    for row, person, decision in identity_hits:
        voter = getattr(decision, "voter", None)
        if voter is None:
            continue
        phone = getattr(decision, "phone", None)
        e164 = getattr(phone, "e164", "") if phone else ""
        classification = getattr(phone, "classification", "") if phone else ""
        has_phone = bool(e164) and classification == "VALID_US"
        if has_phone:
            reason = ""
        elif not _u(voter.phone_raw):
            reason = "VOTER_RECORD_CARRIES_NO_PHONE"
        else:
            reason = f"PHONE_PRESENT_BUT_NOT_VALID_US:{classification or 'UNKNOWN'}"

        role = _u(getattr(person, "role", "")) or "OWNER"
        out.append({
            "candidate_id": row.get("candidate_id") or "",
            "apn": row.get("apn") or "",
            "county": row.get("county") or "",
            "fips": row.get("fips") or "",
            "owner_name": row.get("owner_name") or "",
            "matched_person": getattr(person, "full_normalized", "") or "",
            "person_role": role,
            "relationship": RELATIONSHIP_BY_ROLE.get(role, "OTHER_VERIFIED_RELATION"),
            "match_tier": _u(getattr(decision, "tier", "")),
            "match_confidence": _u(getattr(decision, "confidence", "")),
            "why_matched": _u(getattr(decision, "reason", "")),
            "identity_source": identity_source,
            "source_record_identity": voter.identity_hash,
            "source_hash": _u(voter.source_hash),
            "observed_at": _u(voter.observed_at),
            "voter_status": _u(voter.status),
            "residence_city": _u(getattr(voter.res, "city", "")),
            "residence_zip": _u(getattr(voter.res, "zip5", "")),
            "phone_present": "YES" if has_phone else "NO",
            "phone_absent_reason": reason,
            # An identity is intelligence. It is never authorization.
            "callability_status": "NOT_AUTHORIZED",
        })
    return out

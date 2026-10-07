"""Temporal provenance for every fact a free-first attempt finds (or fails to find).

Three clocks, never collapsed into one:

  observed_at            when the fact was observed true in the source
  retrieved_at           when this runtime read the source
  source_effective_date  the date the source itself says the value is true
                         (publication date, list date, recording date); None when
                         the source does not say

Freshness is computed by app/freshness.field_freshness (the engine's published rule
table) from source_as_of, where source_as_of is the source's effective date when it
states one, or the observation date of a LIVE authoritative query (basis recorded).
A row-bookkeeping timestamp (updated_at, ingested_at, ...) is refused as either
clock: it says when a row changed, not when a fact was true.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from typing import Any, Mapping, Optional

from app.freshness import UNKNOWN_AS_OF, field_freshness

EVIDENCE_VERSION = "landos.free_first.evidence.v1"
CONFIDENCE = ("HIGH", "MEDIUM", "LOW", "UNKNOWN")
AS_OF_BASES = ("SOURCE_EFFECTIVE_DATE", "LIVE_QUERY_OBSERVATION", "UNKNOWN")

#: Bookkeeping timestamps that must never stand in for a fact's observation / effective date.
ROW_TIMESTAMP_KEYS = frozenset({
    "updated_at", "created_at", "inserted_at", "ingested_at", "loaded_at", "modified_at",
    "last_modified", "last_updated", "synced_at", "refreshed_at", "row_updated_at",
})

#: enrichment fact -> app/freshness.FRESHNESS_RULES field.
FACT_FRESHNESS_FIELD = {
    "parcel.owner_identity": "owner_name",
    "contact.mailing_address": "mailing_address",
    "contact.phone": "phone",
    "contact.email": "email",
    "contact.heirs": "heir_link",
    "contact.legal_authority": "heir_link",
    "contact.entity_principal": "owner_name",
}


class ProvenanceRefused(ValueError):
    """The caller tried to date a fact with a clock that cannot date it."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(value: Any) -> str:
    raw = value if isinstance(value, (bytes, bytearray)) else (
        value.encode("utf-8") if isinstance(value, str) else canonical_json(value).encode("utf-8"))
    return hashlib.sha256(raw).hexdigest()


def _iso_date(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise ProvenanceRefused(f"not a date: {value!r}") from exc


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    fact: str
    source_id: str
    value: Any
    value_sha256: str
    observed_at: Optional[str]
    retrieved_at: str
    source_effective_date: Optional[str]
    source_as_of: Optional[str]
    as_of_basis: str
    freshness_state: str
    staleness_days: Optional[int]
    confidence: str
    locator: str
    content_sha256: str
    source_record_id: Optional[str] = None
    version: str = EVIDENCE_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


def make_evidence(*, fact: str, source_id: str, value: Any, retrieved_at: str, confidence: str,
                  locator: str, raw_payload: Any, observed_at: Optional[str] = None,
                  source_effective_date: Any = None, live_query: bool = False,
                  source_record_id: Optional[str] = None, today: Optional[date] = None) -> Evidence:
    """One evidence record with its three clocks and the engine's freshness verdict."""
    if confidence not in CONFIDENCE:
        raise ProvenanceRefused(f"confidence must be one of {CONFIDENCE}")
    effective = _iso_date(source_effective_date)
    if effective is not None:
        as_of, basis = effective, "SOURCE_EFFECTIVE_DATE"
    elif live_query:
        observed_at = observed_at or retrieved_at
        as_of, basis = _iso_date(observed_at), "LIVE_QUERY_OBSERVATION"
    else:
        as_of, basis = None, "UNKNOWN"
    today = today or datetime.now(timezone.utc).date()
    state, days, known = field_freshness(FACT_FRESHNESS_FIELD.get(fact, fact), as_of, today)
    if not known:
        state = UNKNOWN_AS_OF          # reported as UNKNOWN; field_freshness treats it as STALE
    value_sha = sha256_hex({"v": value})
    content_sha = sha256_hex(raw_payload if raw_payload is not None else {"v": value})
    evidence_id = "ev_" + sha256_hex({"fact": fact, "source_id": source_id, "value": value_sha,
                                      "content": content_sha, "as_of": as_of})[:32]
    return Evidence(evidence_id, fact, source_id, value, value_sha, observed_at, retrieved_at, effective,
                    as_of, basis, state, days, confidence, locator, content_sha, source_record_id)


def dated_from_record(record: Mapping[str, Any], *, observed_key: Optional[str],
                      effective_key: Optional[str]) -> tuple:
    """(observed_at, source_effective_date) read from named keys of a source record.

    Refuses to read either clock from a bookkeeping timestamp."""
    for key in (observed_key, effective_key):
        if key and key.strip().lower() in ROW_TIMESTAMP_KEYS:
            raise ProvenanceRefused(
                f"{key!r} is a row-bookkeeping timestamp; it dates the row, not the fact")
    observed = record.get(observed_key) if observed_key else None
    effective = record.get(effective_key) if effective_key else None
    return (None if observed in (None, "") else str(observed)), _iso_date(effective)

"""The Resolution Factory: estate + contact output -> one resolution record each.

Inputs are things that already exist and are already trusted:
  * the CURRENT estate master CSVs (the property truth),
  * the T20D validated contact bundle (phone_append_combined.csv),
  * the resolution ledger (CANDIDATE_CONTACT_RESOLUTION.csv).

Output is one ResolutionRecord per CURRENT candidate, carrying the property, the
owner condition, every living party with its explicit relationship, every
contact point with its evidence, the operator queue, and the next best source.

This module RE-DERIVES nothing that the contact engine already decided. It does
not re-match, re-score, or re-classify. It assembles, and in assembling it
refuses anything the contract forbids -- a phone with no evidence, a trustee
labelled an owner, a contact role that does not belong to its relationship.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..contact.owner_class import owner_route_class
from ..contact.owner_identity import parse_owner_name
from .contract import (
    RESOLUTION_CONTRACT_VERSION,
    ContactPoint,
    ContractViolation,
    Evidence,
    Party,
    Relationship,
    ResolutionRecord,
)
from .router import QUEUE_CONTACT_STATUS, next_best_source

FACTORY_VERSION = "resolution_factory.v2"

# owner_class -> owner_condition. The condition is what the router reads.
OWNER_CONDITION_BY_CLASS = {
    "LIVING_PERSON": "LIVING",
    "MULTI_PERSON": "LIVING",
    "DECEASED": "DECEASED",
    "ESTATE": "ESTATE_OPEN",
    "HEIRS": "HEIRS_UNRESOLVED",
    "TRUST": "TRUST_HELD",
    "LLC": "ENTITY_ACTIVE",
    "CORPORATION": "ENTITY_ACTIVE",
    "NONPROFIT": "ENTITY_ACTIVE",
    "OTHER_ENTITY": "ENTITY_STATUS_UNKNOWN",
    "GOVERNMENT": "GOVERNMENT",
    "UNKNOWN": "UNKNOWN",
}

# person_role on an append row -> relationship role on the graph. This is the
# mapping that stops a trustee becoming an "owner".
RELATIONSHIP_BY_PERSON_ROLE = {
    "OWNER": "RECORDED_OWNER",
    "COOWNER": "COOWNER",
    "LIFE_TENANT": "LIFE_TENANT",
    "NAMED_HEIR_ON_ROLL": "HEIR",
    "NAMED_TRUSTEE_ON_ROLL": "TRUSTEE",
    "PERSONAL_REPRESENTATIVE": "PERSONAL_REPRESENTATIVE",
    "EXECUTOR": "EXECUTOR",
    "ADMINISTRATOR": "ADMINISTRATOR",
    "ENTITY": "ENTITY_MAIN_CONTACT",
    "ENTITY_OFFICER": "ENTITY_OFFICER",
    "ENTITY_MANAGER": "ENTITY_MANAGER",
    "ENTITY_PRINCIPAL": "ENTITY_PRINCIPAL",
    "REGISTERED_AGENT": "REGISTERED_AGENT",
    "DECEDENT": "DECEDENT",
}

# A phone role that a relationship may not carry is a modelling error, but the
# contact engine already assigns roles consistently. Where an append row's
# phone_role is absent we derive it, never guess it.
DEFAULT_CONTACT_ROLE = {
    "RECORDED_OWNER": "OWNER_PERSONAL",
    "COOWNER": "COOWNER_PERSONAL",
    "LIFE_TENANT": "OWNER_PERSONAL",
    "HEIR": "HEIR_PERSONAL",
    "TRUSTEE": "TRUSTEE_PERSONAL",
    "PERSONAL_REPRESENTATIVE": "REPRESENTATIVE_PERSONAL",
    "EXECUTOR": "REPRESENTATIVE_PERSONAL",
    "ADMINISTRATOR": "REPRESENTATIVE_PERSONAL",
    "ENTITY_MAIN_CONTACT": "ENTITY_MAIN",
    "ENTITY_OFFICER": "BUSINESS_OFFICE",
    "ENTITY_MANAGER": "BUSINESS_OFFICE",
    "ENTITY_PRINCIPAL": "BUSINESS_OFFICE",
    "REGISTERED_AGENT": "REGISTERED_AGENT_OFFICE",
    "DECEDENT": "OTHER",
}

TIER_C_STATE = "TIER_C_HELD_NOT_APPENDED"


def _s(row: dict, *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return str(v).strip()
    return ""


def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def load_csv(path: Path) -> List[dict]:
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def evidence_from_append(row: dict, field: str, raw: str, normalized: str) -> Evidence:
    """Every phone in the bundle becomes a traceable observation.

    `source_hash` is the NCSBE county ZIP digest the match ran against, so a
    phone can be tied back to the exact source file, and `source_record_identity`
    is the voter record it came from.
    """
    family = _s(row, "phone_source") or "NCSBE_PUBLIC_VOTER_FILE"
    age = _int(row.get("source_age_days"))
    return Evidence(
        source_family=family,
        source_name=family,
        source_record_id=_s(row, "source_record_identity"),
        source_locator="",  # NCSBE terms: the record id + hash carry traceability
        source_hash=_s(row, "source_hash"),
        field=field,
        raw_value=raw,
        normalized_value=normalized,
        match_method=_s(row, "why_matched", "match_tier") or "TIER_A",
        confidence=_s(row, "confidence_category") or "HIGH",
        observed_at=_s(row, "observed_at", "source_observed_at"),
        retrieved_at=_s(row, "source_observed_at", "observed_at"),
        freshness_days=age,
        notes=_s(row, "match_confidence"),
    )


def relationship_role_for(row: dict) -> str:
    """The relationship role, read from BOTH the person role and the owner class.

    `person_role` on an append row is coarse: the contact engine writes "OWNER"
    for anyone named on the roll. The finer truth is already present in the same
    row -- `phone_role` is COOWNER_PERSONAL and `owner_class` is MULTI_PERSON
    when the parcel has several named owners. Reading only `person_role` would
    file a co-owner as THE recorded owner, which is exactly the role collapse
    this lane exists to prevent. The contract rejects it, so this is the place
    the two signals are reconciled rather than papered over.
    """
    person_role = _s(row, "person_role") or "OWNER"
    role = RELATIONSHIP_BY_PERSON_ROLE.get(person_role, "OTHER_VERIFIED_RELATION")
    if role == "RECORDED_OWNER":
        owner_class = _s(row, "owner_class")
        phone_role = _s(row, "phone_role")
        if owner_class == "MULTI_PERSON" or phone_role == "COOWNER_PERSONAL":
            return "COOWNER"
    return role


def build_relationships(append_rows: Sequence[dict]) -> List[Relationship]:
    """Group a candidate's append rows into parties, then relationships.

    A candidate with two co-owners on one parcel becomes two parties with two
    COOWNER relationships, each carrying its own phone -- not one "owner" with
    two numbers.
    """
    by_person: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for row in append_rows:
        person = _s(row, "matched_person") or _s(row, "owner_name")
        by_person[(person, relationship_role_for(row))].append(row)

    out: List[Relationship] = []
    for (person, rel_role), rows in sorted(by_person.items()):
        first = rows[0]
        ident_ev = evidence_from_append(first, "identity", _s(first, "owner_name"), person)
        contact_points: List[ContactPoint] = []
        for row in rows:
            phone = _s(row, "phone")
            if not phone:
                continue
            role = _s(row, "phone_role") or DEFAULT_CONTACT_ROLE.get(rel_role, "OTHER")
            contact_points.append(ContactPoint(
                kind="PHONE",
                value=phone,
                role=role,
                confidence=_s(row, "confidence_category") or "HIGH",
                confidence_score=_int(row.get("confidence_score")),
                observed_at=_s(row, "observed_at", "source_observed_at"),
                evidence=[evidence_from_append(row, "phone", phone, phone)],
                status="ACTIVE_EVIDENCE",
            ))
        party = Party(
            kind="ORG" if rel_role in {"ENTITY_MAIN_CONTACT", "ENTITY_OFFICER",
                                       "ENTITY_MANAGER", "ENTITY_PRINCIPAL",
                                       "REGISTERED_AGENT"} else "PERSON",
            canonical_name=person,
            evidence=[ident_ev],
            contact_points=contact_points,
            is_living=None if rel_role == "DECEDENT" else True,
            external_ids={"source_record_identity": _s(first, "source_record_identity")},
        )
        out.append(Relationship(
            party=party,
            role=rel_role,
            evidence=[ident_ev],
            confidence=_s(first, "confidence_category") or "HIGH",
            notes=_s(first, "relationship"),
        ))
    return out


def evidence_from_identity(row: dict) -> Evidence:
    """A proven identity that carries no phone is still a traceable fact."""
    family = _s(row, "identity_source") or "NCSBE_PUBLIC_VOTER_FILE"
    return Evidence(
        source_family=family,
        source_name=family,
        source_record_id=_s(row, "source_record_identity"),
        source_locator="",
        source_hash=_s(row, "source_hash"),
        field="identity",
        raw_value=_s(row, "owner_name"),
        normalized_value=_s(row, "matched_person"),
        match_method=_s(row, "why_matched", "match_tier") or "TIER_A",
        confidence="HIGH",
        observed_at=_s(row, "observed_at"),
        retrieved_at=_s(row, "observed_at"),
        notes=_s(row, "phone_absent_reason"),
    )


def identity_relationships(identity_rows: Sequence[dict]) -> List[Relationship]:
    """Parties proven by the matcher whose voter record carries no phone.

    These are the ~5,500 people whose NAMES the pipeline used to discard. They
    have no contact point, so they carry no ContactPoint -- but they are a
    living party with an explicit relationship to the parcel, which is what the
    operator queue and the Qualified board actually need.
    """
    out: List[Relationship] = []
    seen: set = set()
    for row in identity_rows:
        person = _s(row, "matched_person")
        role = _s(row, "relationship") or "RECORDED_OWNER"
        if not person or (person, role) in seen:
            continue
        seen.add((person, role))
        ev = evidence_from_identity(row)
        out.append(Relationship(
            party=Party(
                kind="PERSON",
                canonical_name=person,
                evidence=[ev],
                contact_points=[],
                is_living=True,
                external_ids={"source_record_identity": _s(row, "source_record_identity")},
            ),
            role=role,
            evidence=[ev],
            confidence="HIGH",
            notes=_s(row, "phone_absent_reason"),
        ))
    return out


def build_records(
    estate: Sequence[dict],
    append_rows: Sequence[dict],
    ledger: Sequence[dict],
    estate_revision: str,
    *,
    tier_c_certified_ids: Optional[Iterable[str]] = None,
    identity_ledger: Optional[Sequence[dict]] = None,
    official_relationships: Optional[Dict[str, List[Relationship]]] = None,
) -> List[ResolutionRecord]:
    certified = set(tier_c_certified_ids or ())
    appends_by_cand: Dict[str, List[dict]] = defaultdict(list)
    for row in append_rows:
        appends_by_cand[_s(row, "candidate_id")].append(row)
    identity_by_cand: Dict[str, List[dict]] = defaultdict(list)
    for row in identity_ledger or ():
        # A phone-bearing identity already arrives through the append bundle
        # with its contact point attached; adding it again here would create a
        # second, contactless copy of the same person.
        if _s(row, "phone_present") == "YES":
            continue
        identity_by_cand[_s(row, "candidate_id")].append(row)
    ledger_by_cand = {_s(r, "candidate_id"): r for r in ledger}

    records: List[ResolutionRecord] = []
    for row in estate:
        cid = _s(row, "candidate_id")
        led = ledger_by_cand.get(cid, {})
        owner_name = _s(row, "owner_name")
        owner_class = _s(led, "owner_class") or owner_route_class(owner_name)
        condition = OWNER_CONDITION_BY_CLASS.get(owner_class, "UNKNOWN")
        state = _s(led, "contact_resolution_state") or "UNRESOLVED"

        rels = build_relationships(appends_by_cand.get(cid, []))
        known = {(r.party.canonical_name, r.role) for r in rels}
        for rel in identity_relationships(identity_by_cand.get(cid, [])):
            if (rel.party.canonical_name, rel.role) not in known:
                rels.append(rel)
                known.add((rel.party.canonical_name, rel.role))
        # T22's official county/registry evidence. Added last and only when the
        # same (person, role) is not already proven, so an official import can
        # extend the graph but never restate or override what is there.
        for rel in (official_relationships or {}).get(cid, []):
            if (rel.party.canonical_name, rel.role) not in known:
                rels.append(rel)
                known.add((rel.party.canonical_name, rel.role))
        has_living = any(r.is_contactable_party for r in rels)
        has_high = any(
            cp.confidence == "HIGH" and cp.kind == "PHONE"
            for r in rels for cp in r.party.contact_points
        )
        nxt = next_best_source(
            owner_class=owner_class,
            owner_condition=condition,
            contact_resolution_state=state,
            county=_s(row, "county"),
            has_living_identity=has_living,
            has_high_phone=has_high,
            tier_c_held=(state == TIER_C_STATE),
            tier_c_certified=cid in certified,
        )
        rec = ResolutionRecord(
            candidate_id=cid,
            estate_revision=estate_revision,
            apn=_s(row, "apn"),
            county=_s(row, "county"),
            fips=_s(row, "fips"),
            property_address_raw=_s(row, "property_address_raw"),
            property_address_normalized=_s(row, "property_address_normalized"),
            mailing_address_raw=_s(row, "mailing_address"),
            mailing_address_normalized=_s(row, "mailing_address"),
            owner_name_raw=owner_name,
            owner_name_normalized=" ".join(owner_name.upper().split()),
            owner_class=owner_class,
            owner_condition=condition,
            score=_num(row.get("score")),
            global_rank=_int(row.get("global_rank")),
            qualification_state=_s(row, "qualification_state"),
            primary_recipe=_s(row, "primary_recipe"),
            relationships=rels,
            contact_resolution_state=state,
            next_source=nxt.to_dict(),
            operator_queue=nxt.queue,
            blockers=[nxt.blocking_reason] if nxt.blocking_reason else [],
        )
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Flat projections
# ---------------------------------------------------------------------------

def to_next_source_rows(records: Sequence[ResolutionRecord]) -> List[dict]:
    out = []
    for rec in records:
        ns = rec.next_source or {}
        out.append({
            "candidate_id": rec.candidate_id,
            "county": rec.county,
            "apn": rec.apn,
            "owner_name": rec.owner_name_raw,
            "owner_class": rec.owner_class,
            "owner_condition": rec.owner_condition,
            "qualification_state": rec.qualification_state,
            "primary_recipe": rec.primary_recipe,
            "score": rec.score if rec.score is not None else "",
            "global_rank": rec.global_rank if rec.global_rank is not None else "",
            "contact_resolution_state": rec.contact_resolution_state,
            "has_living_identity": "YES" if rec.has_living_identity else "NO",
            "phone_present": "YES" if rec.high_phones else "NO",
            "operator_queue": rec.operator_queue,
            "contact_status": QUEUE_CONTACT_STATUS.get(rec.operator_queue, ""),
            "next_source": ns.get("next_source", ""),
            "why": ns.get("why", ""),
            "required_input": ns.get("required_input", ""),
            "cost_usd": ns.get("cost_usd", "0.00"),
            "manual_or_automatic": ns.get("manual_or_automatic", ""),
            "expected_information": ns.get("expected_information", ""),
            "blocking_reason": ns.get("blocking_reason", ""),
        })
    return out


def to_yield_records(records: Sequence[ResolutionRecord]) -> List[dict]:
    """Flatten to the shape the yield engine measures."""
    out = []
    for rec in records:
        ns = rec.next_source or {}
        out.append({
            "candidate_id": rec.candidate_id,
            "owner_class": rec.owner_class,
            "county": rec.county,
            "primary_recipe": rec.primary_recipe,
            "qualification_state": rec.qualification_state,
            "source_family": ns.get("next_source") or "NCSBE_PUBLIC_VOTER_FILE",
            "identity_resolved": bool(rec.relationships),
            "living_party_resolved": rec.has_living_identity,
            "phone_found": bool(rec.phones),
            "email_found": any(cp.kind == "EMAIL" for _, cp in rec.contact_points),
            "high_confidence": bool(rec.high_phones),
            "held": rec.contact_resolution_state == TIER_C_STATE,
        })
    return out


QUALIFIED_BOARD_COLUMNS = [
    "candidate_id", "county", "apn", "property_address_raw",
    "owner_name", "owner_class", "owner_condition",
    "primary_recipe", "score", "global_rank", "qualification_state",
    "living_party", "living_party_role", "living_party_count",
    "contact_state", "contact_resolution_score", "phone_present",
    "operator_queue", "contact_status",
    "next_source", "manual_or_automatic", "blocker", "callability_status",
]


def to_qualified_board(records: Sequence[ResolutionRecord]) -> List[dict]:
    """The Qualified board. Priority zero, and the one report that answers
    'why is this a mysterious no-phone bucket' for every single row."""
    out = []
    for rec in records:
        if not rec.qualification_state.startswith("QUALIFIED"):
            continue
        ns = rec.next_source or {}
        living = rec.living_parties
        first = living[0] if living else None
        out.append({
            "candidate_id": rec.candidate_id,
            "county": rec.county,
            "apn": rec.apn,
            "property_address_raw": rec.property_address_raw,
            "owner_name": rec.owner_name_raw,
            "owner_class": rec.owner_class,
            "owner_condition": rec.owner_condition,
            "primary_recipe": rec.primary_recipe,
            "score": rec.score if rec.score is not None else "",
            "global_rank": rec.global_rank if rec.global_rank is not None else "",
            "qualification_state": rec.qualification_state,
            "living_party": first.party.canonical_name if first else "",
            "living_party_role": first.role if first else "",
            "living_party_count": len(living),
            "contact_state": rec.contact_resolution_state,
            "contact_resolution_score": rec.contact_resolution_score,
            "phone_present": "YES" if rec.high_phones else "NO",
            "operator_queue": rec.operator_queue,
            "contact_status": QUEUE_CONTACT_STATUS.get(rec.operator_queue, ""),
            "next_source": ns.get("next_source", ""),
            "manual_or_automatic": ns.get("manual_or_automatic", ""),
            "blocker": ns.get("blocking_reason", ""),
            "callability_status": rec.callability_status,
        })
    out.sort(key=lambda r: (r["global_rank"] if isinstance(r["global_rank"], int) else 10**9))
    return out

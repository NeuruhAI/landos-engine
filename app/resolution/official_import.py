"""Turn T22's official public-record evidence into contract relationships.

T22 read county and state parcel rolls and the IRS exempt-organisation file and
produced import-ready rows binding a candidate to a named living person or an
organisation contact. This module is the only place that translates those rows,
so the rules about what a source is allowed to assert live in one file.

THE RULE THAT MATTERS MOST. A shared surname on a parcel roll is not proof of
heirship. 92 of the 104 estate rows arrive as HEIR_OR_SUCCESSOR_CONTACT with
match method SURNAME_CORROBORATED -- a person on the roll whose surname matches
the decedent's. That is a real, useful, official lead, and it is NOT an
adjudicated heir. It lands as OTHER_VERIFIED_RELATION with the corroboration
named in the notes. Only rows whose source STATES the role -- the 12 marked
OFFICIAL_ROLE_STATED, conf=EXACT -- become EXECUTOR or ADMINISTRATOR.

Nothing here infers a relationship from a mailing address, and nothing here
creates a contact point: an address is not a phone and a name is not a person
you may call.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

from .contract import Evidence, Party, Relationship

#: Only these two confidence tokens may be imported without a human.
ACCEPTED_CONFIDENCE = frozenset({"EXACT", "HIGH"})

#: T22 source name -> the family that names its class of authority.
SOURCE_FAMILY_BY_NAME: Dict[str, str] = {
    "WAKE_PARCELS": "COUNTY_PARCEL_ROLL",
    "DURHAM_PARCELS": "COUNTY_PARCEL_ROLL",
    "MECK_CAMA_OWNERSHIP": "COUNTY_PARCEL_ROLL",
    "MECK_TAXPARCELSAPP": "COUNTY_PARCEL_ROLL",
    "NC_ONEMAP_PARCELS": "COUNTY_PARCEL_ROLL",
    "IRS_EO_BMF_NC": "IRS_EXEMPT_ORG_BMF",
}

#: T22 relationship token -> (contract role, whether the source states the role)
ROLE_MAP: Dict[str, Tuple[str, bool]] = {
    "EXECUTOR": ("EXECUTOR", True),
    "ADMINISTRATOR": ("ADMINISTRATOR", True),
    "PERSONAL_REPRESENTATIVE": ("PERSONAL_REPRESENTATIVE", True),
    "TRUSTEE": ("TRUSTEE", True),
    "COOWNER": ("COOWNER", True),
    "REGISTERED_AGENT": ("REGISTERED_AGENT", True),
    # Surname corroboration. A successor CANDIDATE, not an heir.
    "HEIR_OR_SUCCESSOR_CONTACT": ("OTHER_VERIFIED_RELATION", False),
}


def _s(row: dict, key: str) -> str:
    return " ".join(str(row.get(key) or "").strip().split())


def parse_source_document(value: str) -> dict:
    """`T22|WAKE_PARCELS|<locator>|conf=EXACT|OFFICIAL_ROLE_STATED`."""
    parts = [p.strip() for p in (value or "").split("|")]
    out = {"lane": "", "source_name": "", "locator": "", "confidence": "", "match_method": ""}
    if parts:
        out["lane"] = parts[0]
    if len(parts) > 1:
        out["source_name"] = parts[1]
    if len(parts) > 2:
        out["locator"] = parts[2]
    for p in parts[3:]:
        if p.startswith("conf="):
            out["confidence"] = p[5:].strip().upper()
        elif p:
            out["match_method"] = p
    return out


def _evidence(meta: dict, *, field: str, raw: str, normalized: str,
              observed_at: str, notes: str) -> Evidence:
    family = SOURCE_FAMILY_BY_NAME.get(meta["source_name"], "OPERATOR_MANUAL_PUBLIC_RECORD")
    # EXACT is stronger than HIGH at the source; the contract's vocabulary tops
    # out at HIGH, and calling an EXACT match HIGH understates nothing that
    # matters downstream.
    confidence = "HIGH" if meta["confidence"] in ACCEPTED_CONFIDENCE else "REVIEW"
    return Evidence(
        source_family=family,
        source_name=meta["source_name"] or "T22_OFFICIAL_IMPORT",
        field=field,
        raw_value=raw,
        normalized_value=normalized,
        match_method=meta["match_method"] or "OFFICIAL_IMPORT",
        confidence=confidence,
        observed_at=observed_at,
        retrieved_at=observed_at,
        source_record_id=meta["locator"][:200],
        source_locator=meta["locator"][:500],
        source_hash="",
        notes=notes,
    )


def estate_relationships(rows: Sequence[dict]) -> Dict[str, List[Relationship]]:
    """candidate_id -> the living parties T22 proved from an official roll."""
    out: Dict[str, List[Relationship]] = {}
    for row in rows:
        meta = parse_source_document(_s(row, "source_document"))
        if meta["confidence"] not in ACCEPTED_CONFIDENCE:
            continue  # REVIEW is never imported without a human
        cid = _s(row, "property_candidate_id")
        token = _s(row, "relationship").upper()
        mapped = ROLE_MAP.get(token)
        if not cid or not mapped:
            continue
        role, role_is_stated = mapped
        person = (_s(row, "personal_representative_name")
                  or _s(row, "heir_name")
                  or _s(row, "applicant_name"))
        if not person:
            continue
        decedent = _s(row, "decedent_name")
        if role_is_stated:
            notes = f"{meta['match_method']} for the estate of {decedent}".strip()
        else:
            notes = (f"successor candidate: {meta['match_method']} against decedent "
                     f"{decedent}. Surname corroboration is not an adjudicated "
                     f"heirship -- probate record still required to name the heir.")
        ev = _evidence(meta, field="party_identity", raw=person, normalized=person.upper(),
                       observed_at=_s(row, "source_date"), notes=notes)
        rel = Relationship(
            party=Party(kind="PERSON", canonical_name=person, evidence=[ev],
                        contact_points=[], is_living=True,
                        external_ids={"t22_apn": _s(row, "apn")}),
            role=role,
            evidence=[ev],
            confidence="HIGH",
            notes=notes,
        )
        out.setdefault(cid, []).append(rel)
    return out


def entity_relationships(rows: Sequence[dict]) -> Dict[str, List[Relationship]]:
    """candidate_id -> the organisation contact T22 proved from a registry.

    Only rows that actually NAME a natural person produce a relationship. A row
    that carries a mailing office and nothing else is an address, not a party,
    and it stays out of the party graph -- it belongs to the entity record.
    """
    out: Dict[str, List[Relationship]] = {}
    for row in rows:
        meta = parse_source_document(_s(row, "source_document"))
        if meta["confidence"] not in ACCEPTED_CONFIDENCE:
            continue
        cid = _s(row, "property_candidate_id")
        official = _s(row, "official_name")
        if not cid or not official:
            continue
        title = _s(row, "official_title").upper()
        role = {
            "REGISTERED_AGENT": "REGISTERED_AGENT",
            "MANAGER": "ENTITY_MANAGER",
            "MEMBER": "ENTITY_PRINCIPAL",
            "OFFICER": "ENTITY_OFFICER",
            "PRESIDENT": "ENTITY_OFFICER",
        }.get(title, "ENTITY_MAIN_CONTACT")
        legal = _s(row, "entity_legal_name")
        notes = f"{title or 'ORGANIZATION_CONTACT'} named for {legal}".strip()
        ev = _evidence(meta, field="party_identity", raw=official,
                       normalized=official.upper(), observed_at=_s(row, "source_date"),
                       notes=notes)
        out.setdefault(cid, []).append(Relationship(
            party=Party(kind="PERSON", canonical_name=official, evidence=[ev],
                        contact_points=[], is_living=True,
                        external_ids={"entity_legal_name": legal}),
            role=role, evidence=[ev], confidence="HIGH", notes=notes,
        ))
    return out


def load_import_csv(path: Path) -> List[dict]:
    if not Path(path).exists():
        return []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))

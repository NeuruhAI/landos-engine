"""Turn official public-record evidence into contract relationships.

T22 read county and state parcel rolls and the IRS exempt-organisation file and
produced import-ready rows binding a candidate to a named living person or an
organisation contact. T24C read more rolls, the same way, and wrote its rows in
a different grammar. This module is the only place that translates either of
them, so the rules about what a source is allowed to assert live in one file.

THE RULE THAT MATTERS MOST. A shared surname on a parcel roll is not proof of
heirship. 92 of T22's 104 estate rows arrive as HEIR_OR_SUCCESSOR_CONTACT with
match method SURNAME_CORROBORATED -- a person on the roll whose surname matches
the decedent's. That is a real, useful, official lead, and it is NOT an
adjudicated heir. It lands as OTHER_VERIFIED_RELATION with the corroboration
named in the notes. Only rows whose source STATES the role -- the ones marked
OFFICIAL_ROLE_STATED, conf=EXACT -- become EXECUTOR or ADMINISTRATOR.

THE RULE THAT COST THE MOST TO LEARN. Confidence is recovered from evidence, it
is never asserted by the file being imported. T24C's import rows carry no
confidence token at all; 95 of its entity rows are the county tax roll's care-of
line, which says who receives the bill and not who is an officer. A bridge that
stamped an accepted token on every row to make the file parse would have written
all 95 into the party graph as proven people. So a row whose grammar carries no
confidence is admitted only when a separate evidence file supplies one, keyed on
the same candidate and the same person, and is otherwise dropped exactly as a
REVIEW row is. The gate cannot be satisfied by the import file alone.

Nothing here infers a relationship from a mailing address, and nothing here
creates a contact point: an address is not a phone and a name is not a person
you may call.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .contract import Evidence, Party, Relationship

#: Only these two confidence tokens may be imported without a human.
ACCEPTED_CONFIDENCE = frozenset({"EXACT", "HIGH"})

#: Source name -> the family that names its class of authority.
SOURCE_FAMILY_BY_NAME: Dict[str, str] = {
    "WAKE_PARCELS": "COUNTY_PARCEL_ROLL",
    "DURHAM_PARCELS": "COUNTY_PARCEL_ROLL",
    "MECK_CAMA_OWNERSHIP": "COUNTY_PARCEL_ROLL",
    "MECK_TAXPARCELSAPP": "COUNTY_PARCEL_ROLL",
    "NC_ONEMAP_PARCELS": "COUNTY_PARCEL_ROLL",
    "IRS_EO_BMF_NC": "IRS_EXEMPT_ORG_BMF",
    # T24C opened two Mecklenburg services T22 never reached. Same class of
    # authority as the rolls above: the county publishing its own ownership.
    "MECK_ALL_OWNERS": "COUNTY_PARCEL_ROLL",
    "MECK_ALL_PARCELDATA": "COUNTY_PARCEL_ROLL",
}

#: Relationship token -> (contract role, whether the source states the role)
ROLE_MAP: Dict[str, Tuple[str, bool]] = {
    "EXECUTOR": ("EXECUTOR", True),
    "ADMINISTRATOR": ("ADMINISTRATOR", True),
    "PERSONAL_REPRESENTATIVE": ("PERSONAL_REPRESENTATIVE", True),
    "TRUSTEE": ("TRUSTEE", True),
    "COOWNER": ("COOWNER", True),
    "REGISTERED_AGENT": ("REGISTERED_AGENT", True),
    # The county owner-of-record field names two parties, one of them marked
    # dead ("HEIRS", "ESTATE OF") and one not. The unmarked one is a co-owner
    # BECAUSE THE ROLL SAYS SO -- that is a title-level fact stated outright,
    # not an inference, so it keeps the stated role COOWNER. What is NOT stated
    # is any relationship to the decedent; that stays surname corroboration and
    # is never called heirship. See the notes this produces.
    "CO_OWNER_OF_RECORD": ("COOWNER", True),
    # Surname corroboration. A successor CANDIDATE, not an heir.
    "HEIR_OR_SUCCESSOR_CONTACT": ("OTHER_VERIFIED_RELATION", False),
}

#: Tokens whose stated role is ownership of record rather than an estate office.
_OWNERSHIP_OF_RECORD_TOKENS = frozenset({"CO_OWNER_OF_RECORD"})

#: `WAKE_PARCELS :: <locator> (corroborated by 2 independent official sources: A,B)`
_T24C_CORROBORATION = re.compile(
    r"\s*\(corroborated by (\d+) independent official sources?:\s*([^)]*)\)\s*$")


def _s(row: dict, key: str) -> str:
    return " ".join(str(row.get(key) or "").strip().split())


#: A parcel roll writes the care-of marker INSIDE the owner name field:
#: `% IDA MAE P BROWN`. It is a record annotation about how mail reaches the
#: person, not part of the person's name, and carrying it into canonical_name
#: creates a second party for a human the graph already holds -- T22 landed
#: `% IDA MAE P BROWN` and T24C, which cleans the marker, would have landed
#: `IDA MAE P BROWN` beside it. The raw value is preserved on the evidence, so
#: what the roll actually said is never lost.
#: Deliberately narrow. `%` is the only form the NC rolls actually use (2 rows
#: across both supplies) and `C/O` / `ATTN` are the only other unambiguous ones.
#: A bare leading `CO` is NOT stripped: `CO` is a real surname, and turning
#: `CO MINH` into `MINH` would invent a person rather than clean one.
_CARE_OF_PREFIX = re.compile(r"^\s*(?:%|C\s*/\s*O|ATTN)[:.]?\s+", re.IGNORECASE)


def canonical_person(value: str) -> str:
    """The person's name as the graph keys it: no care-of marker, no padding."""
    out = " ".join(str(value or "").strip().split())
    prev = None
    while out != prev:
        prev = out
        out = _CARE_OF_PREFIX.sub("", out, count=1).strip()
    return out


def normalize_person(value: str) -> str:
    """The join key between an import row and its evidence row."""
    return canonical_person(value).upper()


def parse_source_document(value: str) -> dict:
    """Read either grammar an official supply has used.

    T22   `T22|WAKE_PARCELS|<locator>|conf=EXACT|OFFICIAL_ROLE_STATED`
    T24C  `WAKE_PARCELS :: <locator> (corroborated by 2 independent official
          sources: WAKE_PARCELS,NC_ONEMAP_PARCELS)`

    The two are told apart by the pipe, which T24C never emits and T22 always
    does. T22's rows are already in production and must keep parsing byte for
    byte, so its branch is untouched.

    T24C's grammar carries no confidence and none is invented for it: the field
    comes back empty and the caller must supply one from evidence or drop the
    row.
    """
    raw = (value or "").strip()
    out = {"lane": "", "source_name": "", "locator": "", "confidence": "",
           "match_method": "", "corroborating_sources": ()}
    if not raw:
        return out
    if "|" in raw:
        parts = [p.strip() for p in raw.split("|")]
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
    if " :: " not in raw:
        return out
    name, _, rest = raw.partition(" :: ")
    out["lane"] = "T24C"
    out["source_name"] = name.strip()
    m = _T24C_CORROBORATION.search(rest)
    if m:
        rest = rest[: m.start()]
        out["corroborating_sources"] = tuple(
            s.strip() for s in m.group(2).split(",") if s.strip())
    out["locator"] = rest.strip()
    return out


@dataclass(frozen=True)
class RecoveredEvidence:
    """What the evidence file says about one (candidate, person) pair.

    This is the only lawful source of confidence for an import row that does not
    carry one. It is read from the supply's own normalized evidence, never from
    the import file, and never defaulted.
    """
    confidence: str
    match_method: str = ""
    review_state: str = ""
    source_ids: Tuple[str, ...] = field(default_factory=tuple)


#: (candidate_id, normalized person) -> RecoveredEvidence
RecoveredMap = Mapping[Tuple[str, str], RecoveredEvidence]

_CONFIDENCE_RANK = {"EXACT": 3, "HIGH": 2, "MEDIUM": 1, "REVIEW": 0, "REJECT": -1}


def _recover(path: Path, *, person_col: str, confidence_col: str) -> Dict[Tuple[str, str], RecoveredEvidence]:
    out: Dict[Tuple[str, str], RecoveredEvidence] = {}
    by_key: Dict[Tuple[str, str], List[dict]] = {}
    for row in load_import_csv(path):
        person = normalize_person(row.get(person_col, ""))
        if not person:
            continue
        by_key.setdefault((_s(row, "candidate_id"), person), []).append(row)
    for key, rows in by_key.items():
        best = max(rows, key=lambda r: _CONFIDENCE_RANK.get(
            _s(r, confidence_col).upper(), -2))
        out[key] = RecoveredEvidence(
            confidence=_s(best, confidence_col).upper(),
            match_method=_s(best, "match_method"),
            review_state=_s(best, "review_state"),
            source_ids=tuple(sorted({_s(r, "source_id") for r in rows if _s(r, "source_id")})),
        )
    return out


def recover_estate_confidence(path: Path) -> Dict[Tuple[str, str], RecoveredEvidence]:
    """Read `ESTATE_RESOLUTION_EVIDENCE.csv` into the confidence the gate reads."""
    return _recover(Path(path), person_col="living_party_name",
                    confidence_col="match_confidence")


def recover_entity_confidence(path: Path) -> Dict[Tuple[str, str], RecoveredEvidence]:
    """Read `ENTITY_RESOLUTION_EVIDENCE.csv` into the confidence the gate reads."""
    return _recover(Path(path), person_col="principal_name",
                    confidence_col="confidence")


def _resolve_confidence(meta: dict, cid: str, person: str,
                        recovered: Optional[RecoveredMap]) -> dict:
    """A row that states its own confidence keeps it. A row that does not gets
    one from evidence, or gets nothing and is dropped by the caller."""
    if meta["confidence"]:
        return meta
    if not recovered:
        return meta
    got = recovered.get((cid, normalize_person(person)))
    if not got:
        return meta
    meta = dict(meta)
    meta["confidence"] = (got.confidence or "").upper()
    meta["match_method"] = meta["match_method"] or got.match_method
    meta["review_state"] = got.review_state
    meta["source_ids"] = got.source_ids
    return meta


def _corroboration_note(meta: dict) -> str:
    sources = meta.get("corroborating_sources") or meta.get("source_ids") or ()
    if len(sources) < 2:
        return ""
    return (f" Corroborated by {len(sources)} independent official sources: "
            f"{', '.join(sources)}.")


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


def estate_relationships(rows: Sequence[dict],
                         recovered: Optional[RecoveredMap] = None,
                         ) -> Dict[str, List[Relationship]]:
    """candidate_id -> the living parties an official roll proved.

    `recovered` supplies confidence for supplies whose import grammar does not
    carry it. Withhold it and those rows are dropped, which is the point: the
    file alone can never open the gate.
    """
    out: Dict[str, List[Relationship]] = {}
    for row in rows:
        cid = _s(row, "property_candidate_id")
        token = _s(row, "relationship").upper()
        person = (_s(row, "personal_representative_name")
                  or _s(row, "heir_name")
                  or _s(row, "applicant_name"))
        meta = _resolve_confidence(parse_source_document(_s(row, "source_document")),
                                   cid, person, recovered)
        if meta["confidence"] not in ACCEPTED_CONFIDENCE:
            continue  # REVIEW, MEDIUM and unevidenced are never imported without a human
        mapped = ROLE_MAP.get(token)
        if not cid or not mapped or not person:
            continue
        role, role_is_stated = mapped
        decedent = _s(row, "decedent_name")
        if token in _OWNERSHIP_OF_RECORD_TOKENS:
            # The roll states the ownership. It does not state the kinship, and
            # the note must not let a reader borrow one from the other.
            notes = (f"named as an owner of record alongside a decedent-marked "
                     f"co-owner segment ({decedent}) by {meta['match_method']}. "
                     f"Co-ownership of record is stated by the source; any "
                     f"relationship to the decedent is surname corroboration "
                     f"only and is not an adjudicated heirship.")
        elif role_is_stated:
            notes = f"{meta['match_method']} for the estate of {decedent}".strip()
        else:
            notes = (f"successor candidate: {meta['match_method']} against decedent "
                     f"{decedent}. Surname corroboration is not an adjudicated "
                     f"heirship -- probate record still required to name the heir.")
        notes = (notes + _corroboration_note(meta)).strip()
        canonical = canonical_person(person)
        ev = _evidence(meta, field="party_identity", raw=person,
                       normalized=canonical.upper(),
                       observed_at=_s(row, "source_date"), notes=notes)
        rel = Relationship(
            party=Party(kind="PERSON", canonical_name=canonical, evidence=[ev],
                        contact_points=[], is_living=True,
                        external_ids={"t22_apn": _s(row, "apn")}),
            role=role,
            evidence=[ev],
            confidence="HIGH",
            notes=notes,
        )
        out.setdefault(cid, []).append(rel)
    return out


def entity_relationships(rows: Sequence[dict],
                         recovered: Optional[RecoveredMap] = None,
                         ) -> Dict[str, List[Relationship]]:
    """candidate_id -> the organisation contact a registry proved.

    Only rows that actually NAME a natural person produce a relationship. A row
    that carries a mailing office and nothing else is an address, not a party,
    and it stays out of the party graph -- it belongs to the entity record.

    The county tax roll's care-of line names whoever receives the bill. That is
    MEDIUM in the supply's own evidence and it stays out here, which is why the
    confidence has to arrive from that evidence and not from the import file.
    """
    out: Dict[str, List[Relationship]] = {}
    for row in rows:
        cid = _s(row, "property_candidate_id")
        official = _s(row, "official_name")
        meta = _resolve_confidence(parse_source_document(_s(row, "source_document")),
                                   cid, official, recovered)
        if meta["confidence"] not in ACCEPTED_CONFIDENCE:
            continue
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
        notes = (f"{title or 'ORGANIZATION_CONTACT'} named for {legal}"
                 + _corroboration_note(meta)).strip()
        canonical = canonical_person(official)
        ev = _evidence(meta, field="party_identity", raw=official,
                       normalized=canonical.upper(), observed_at=_s(row, "source_date"),
                       notes=notes)
        out.setdefault(cid, []).append(Relationship(
            party=Party(kind="PERSON", canonical_name=canonical, evidence=[ev],
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

"""Import adapter for lawfully obtained NC public estate-case identity data.

Does not scrape eCourts. Operators (or a later lawful bulk extract) drop a CSV
matching ESTATE_IMPORT_COLUMNS. Living applicant / PR / heir people are emitted
for the same NCSBE matcher. The decedent is never a phone target.
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from .owner_identity import PersonName, parse_owner_name

ESTATE_IMPORT_COLUMNS = (
    "property_candidate_id",
    "apn",
    "county",
    "fips",
    "decedent_name",
    "estate_case_number",
    "applicant_name",
    "co_applicant_name",
    "personal_representative_name",
    "heir_name",
    "relationship",
    "mailing_address",
    "mailing_city",
    "mailing_state",
    "mailing_zip",
    "attorney_name",
    "source_document",
    "source_date",
)

# Attorney phones are never accepted as owner/heir phones.
FORBIDDEN_RELATIONSHIPS = {"ATTORNEY", "COUNSEL", "LAWYER", "ESQ"}

ROLE_BY_FIELD = {
    "applicant_name": "APPLICANT",
    "co_applicant_name": "CO_APPLICANT",
    "personal_representative_name": "PERSONAL_REPRESENTATIVE",
    "heir_name": "HEIR",
}


@dataclass(frozen=True)
class EstateCasePerson:
    property_candidate_id: str
    apn: str
    county: str
    fips: str
    estate_case: str
    decedent_name: str
    person: PersonName
    relationship: str
    mailing_address: str
    mailing_city: str
    mailing_state: str
    mailing_zip: str
    source_document: str
    source_date: str
    heir_person_id: str


def _hid(*parts: str) -> str:
    payload = "|".join(parts)
    return "HEIR_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _people_from_field(name: str, relationship: str) -> List[PersonName]:
    parsed = parse_owner_name(name)
    out = []
    for p in parsed.living_people:
        out.append(
            PersonName(
                first=p.first,
                last=p.last,
                middle=p.middle,
                suffix=p.suffix,
                deceased=False,
                life_estate=False,
                role="NAMED_HEIR_ON_ROLL",
                parse_method=p.parse_method,
                raw_segment=p.raw_segment or name,
            )
        )
    return out


def parse_estate_import_rows(rows: Iterable[dict]) -> List[EstateCasePerson]:
    people: List[EstateCasePerson] = []
    for row in rows:
        rel_override = (row.get("relationship") or "").strip().upper()
        if rel_override in FORBIDDEN_RELATIONSHIPS:
            continue
        decedent = (row.get("decedent_name") or "").strip()
        case_no = (row.get("estate_case_number") or "").strip()
        cand = (row.get("property_candidate_id") or "").strip()
        for field_name, default_rel in ROLE_BY_FIELD.items():
            raw_name = (row.get(field_name) or "").strip()
            if not raw_name:
                continue
            relationship = rel_override or default_rel
            if relationship in FORBIDDEN_RELATIONSHIPS:
                continue
            for person in _people_from_field(raw_name, relationship):
                people.append(
                    EstateCasePerson(
                        property_candidate_id=cand,
                        apn=(row.get("apn") or "").strip(),
                        county=(row.get("county") or "").strip(),
                        fips=(row.get("fips") or "").strip(),
                        estate_case=case_no,
                        decedent_name=decedent,
                        person=person,
                        relationship=relationship,
                        mailing_address=(row.get("mailing_address") or "").strip(),
                        mailing_city=(row.get("mailing_city") or "").strip(),
                        mailing_state=(row.get("mailing_state") or "").strip(),
                        mailing_zip=(row.get("mailing_zip") or "").strip(),
                        source_document=(row.get("source_document") or "").strip(),
                        source_date=(row.get("source_date") or "").strip(),
                        heir_person_id=_hid(cand, case_no, person.full_normalized, relationship),
                    )
                )
    return people


def load_estate_import_csv(path: str | Path) -> List[EstateCasePerson]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return parse_estate_import_rows(rows)


def empty_import_template_row() -> dict:
    return {k: "" for k in ESTATE_IMPORT_COLUMNS}

"""Import adapter for lawfully obtained NC public estate-case identity data.

Does not scrape eCourts. Operators (or a later lawful bulk extract) drop a CSV
matching ESTATE_IMPORT_COLUMNS. Living applicant / PR / heir people are emitted
for the same NCSBE matcher. The decedent is never a phone target.

Exact file contract (enforced by `estate_import_validate`, consumed by
`load_estate_import_csv` / `scripts/run_nc_free_phone_enrichment.py`):

- Path: `{raw_dir}/estate/` where `--raw-dir` is the enrichment `_raw`
  directory. Canonical drop:
  `_raw/estate/{county_slug}_estate_{YYYY-MM-DD}.csv`
- Filename: `{county_slug}_estate_{YYYY-MM-DD}.csv` (lowercase county slug).
  The runner glob-loads every `*.csv` here except names ending `template.csv`.
  `estate_import_template.csv` is the empty header the runner may write; it is
  never a data file.
- Encoding: UTF-8, no BOM. Delimiter: comma. Header: first row, exact names
  in `ESTATE_IMPORT_COLUMNS`. `source_date`: ISO-8601 calendar date `YYYY-MM-DD`
  stored as text — this loader does not parse it.
- Living people come only from `applicant_name` / `co_applicant_name` /
  `personal_representative_name` / `heir_name`. `attorney_name` is provenance
  only. `decedent_name` is never a phone target.
- This module does not write LEGAL_AUTHORITY, mutate DNC, or set a phone
  callable. Family labels without a court case number are not heirship.
"""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List

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

ESTATE_IMPORT_ENCODING = "utf-8"
ESTATE_IMPORT_DELIMITER = ","
ESTATE_IMPORT_HEADER_IS_FIRST_ROW = True
ESTATE_IMPORT_DATE_FORMAT = "%Y-%m-%d"
ESTATE_IMPORT_DIRNAME = "estate"
ESTATE_IMPORT_PATH_MARKER = "_raw/estate"
ESTATE_IMPORT_TEMPLATE_NAME = "estate_import_template.csv"
ESTATE_IMPORT_FILENAME_RE = re.compile(
    r"^(?P<county>[a-z]+)_estate_(?P<date>\d{4}-\d{2}-\d{2})\.csv$"
)

# Seven CURRENT NC counties. FIPS matches the estate master files.
ESTATE_IMPORT_COUNTY_FIPS = {
    "Cabarrus": "37025",
    "Durham": "37063",
    "Gaston": "37071",
    "Johnston": "37101",
    "Mecklenburg": "37119",
    "Union": "37179",
    "Wake": "37183",
}

# Attorney phones are never accepted as owner/heir phones.
FORBIDDEN_RELATIONSHIPS = {"ATTORNEY", "COUNSEL", "LAWYER", "ESQ"}

# This importer is identity-only. LEGAL_AUTHORITY is a graph
# relationship_class on a different contract (import_packages/estate) and
# must never be written here. EXECUTOR / ADMINISTRATOR as a court-stated
# *role* are allowed only when estate_case_number is present.
LEGAL_AUTHORITY_RELATIONSHIP = "LEGAL_AUTHORITY"
COURT_ROLE_REQUIRES_CASE = {
    "HEIR", "APPLICANT", "CO_APPLICANT", "PERSONAL_REPRESENTATIVE",
    "EXECUTOR", "ADMINISTRATOR",
}

FAMILY_RELATIONSHIPS = {
    "DAUGHTER", "SON", "CHILD", "SPOUSE", "WIDOW", "WIDOWER", "HUSBAND", "WIFE",
    "SIBLING", "BROTHER", "SISTER", "PARENT", "MOTHER", "FATHER",
    "GRANDCHILD", "NIECE", "NEPHEW", "FAMILY",
}

ROLE_BY_FIELD = {
    "applicant_name": "APPLICANT",
    "co_applicant_name": "CO_APPLICANT",
    "personal_representative_name": "PERSONAL_REPRESENTATIVE",
    "heir_name": "HEIR",
}

LIVING_PERSON_FIELDS = tuple(ROLE_BY_FIELD.keys())


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

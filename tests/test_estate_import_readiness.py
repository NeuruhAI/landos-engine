"""B1 — estate import readiness. Synthetic names only. No court contact. No DNC."""
from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from app.contact.estate_import import (
    ESTATE_IMPORT_COLUMNS,
    ESTATE_IMPORT_COUNTY_FIPS,
    FORBIDDEN_RELATIONSHIPS,
    LIVING_PERSON_FIELDS,
    load_estate_import_csv,
    parse_estate_import_rows,
)
from app.contact.estate_import_validate import (
    render_validator_report,
    validate_estate_import_file,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "estate" / "wake_estate_2026-08-28.csv"
GUIDE = ROOT / "docs" / "ESTATE_IMPORT_OPERATOR_GUIDE.md"

SYNTHETIC_SURNAMES = {
    "ALDERNEY", "MAPLEWOOD", "FAIRHAVEN", "RIVERBEND",
    "STONECROFT", "NORTHFORK", "CEDARHILL", "WILLOWBEND", "SAMPLE",
}
SYNTHETIC_GIVENS = {
    "QUINCY", "TESSA", "NELL", "OSCAR", "IRIS", "BESS", "PRUE", "HOMER",
}


def _write_csv(path: Path, rows: list[dict], columns=ESTATE_IMPORT_COLUMNS) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(columns))
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in columns})


def _base_row(**overrides) -> dict:
    row = {k: "" for k in ESTATE_IMPORT_COLUMNS}
    row.update({
        "property_candidate_id": "synthetic-wake-estate-009",
        "apn": "SYNTH-WAKE-0009",
        "county": "Wake",
        "fips": "37183",
        "decedent_name": "ALDERNEY, QUINCY P",
        "estate_case_number": "26E000009",
        "applicant_name": "MAPLEWOOD, TESSA J",
        "relationship": "APPLICANT",
        "mailing_address": "100 EXAMPLE LN",
        "mailing_city": "CHARLOTTE",
        "mailing_state": "NC",
        "mailing_zip": "28202",
        "source_document": "NC_ECOURTS_REGISTER_OF_ACTIONS",
        "source_date": "2026-08-28",
    })
    row.update(overrides)
    return row


def test_fixture_is_synthetic_only():
    text = FIXTURE.read_text(encoding="utf-8")
    assert "POWELL" not in text
    assert "GOODSON" not in text
    people = load_estate_import_csv(FIXTURE)
    assert people, "fixture must parse into heir identity records"
    for ep in people:
        assert ep.person.last in SYNTHETIC_SURNAMES
        assert ep.person.first in SYNTHETIC_GIVENS
        assert ep.decedent_name.split(",")[0].strip() in SYNTHETIC_SURNAMES


def test_synthetic_fixture_parses_heir_identity_records():
    people = load_estate_import_csv(FIXTURE)
    assert len(people) == 6
    assert {ep.property_candidate_id for ep in people} == {
        "synthetic-wake-estate-001",
        "synthetic-wake-estate-002",
    }
    assert all(ep.county == "Wake" for ep in people)
    assert all(ep.fips == ESTATE_IMPORT_COUNTY_FIPS["Wake"] for ep in people)
    assert all(ep.estate_case for ep in people)
    assert all(ep.heir_person_id.startswith("HEIR_") for ep in people)
    assert len({ep.heir_person_id for ep in people}) == 6
    assert all(ep.person.role == "NAMED_HEIR_ON_ROLL" for ep in people)
    assert all(not ep.person.deceased for ep in people)
    roles = {ep.relationship for ep in people}
    assert "APPLICANT" in roles
    assert "PERSONAL_REPRESENTATIVE" in roles
    assert "HEIR" in roles
    assert "CO_APPLICANT" in roles
    assert "LEGAL_AUTHORITY" not in roles
    assert "ATTORNEY" not in roles
    mail_cities = {ep.mailing_city for ep in people}
    assert "CHARLOTTE" in mail_cities
    assert "DURHAM" in mail_cities
    # Decedent strings never become living people.
    living = {ep.person.last for ep in people}
    assert "ALDERNEY" not in living
    assert "WILLOWBEND" not in living


def test_one_row_two_living_fields_emits_two_people():
    people = [ep for ep in load_estate_import_csv(FIXTURE)
              if ep.property_candidate_id == "synthetic-wake-estate-002"
              and ep.mailing_address.startswith("500")]
    names = {(ep.person.first, ep.person.last) for ep in people}
    assert names == {("BESS", "NORTHFORK"), ("PRUE", "CEDARHILL")}


def test_validator_fixture_passes_without_importing():
    report = validate_estate_import_file(FIXTURE)
    assert report["imported"] is False
    assert report["overall"] == "PASS"
    assert report["rows"] == 5
    assert report["preview_people"] == 6
    assert report["checks"]["PATH"]["result"] == "SKIP"
    assert report["checks"]["PARSE_PREVIEW"]["detail"]["imported"] is False
    for col in ESTATE_IMPORT_COLUMNS:
        spec = report["columns"][col]
        assert spec["header"] == "PASS", col
        if col not in {"co_applicant_name", "personal_representative_name",
                       "heir_name", "applicant_name", "attorney_name", "relationship"}:
            assert spec["format"] == "PASS", col
    table = render_validator_report(report)
    assert "imported: NO" in table
    assert "OVERALL PASS" in table
    for col in ESTATE_IMPORT_COLUMNS:
        assert col in table


def test_validator_placement_fails_for_fixture_path():
    report = validate_estate_import_file(FIXTURE, expect_placement=True)
    assert report["imported"] is False
    assert report["checks"]["PATH"]["result"] == "FAIL"
    assert report["overall"] == "FAIL"


def test_validator_reports_column_failures(tmp_path):
    path = tmp_path / "wake_estate_2026-08-28.csv"
    rows = [_base_row()]
    # Drop a required column.
    cols = [c for c in ESTATE_IMPORT_COLUMNS if c != "estate_case_number"]
    _write_csv(path, rows, columns=cols)
    report = validate_estate_import_file(path)
    assert report["imported"] is False
    assert report["overall"] == "FAIL"
    assert report["columns"]["estate_case_number"]["header"] == "FAIL"
    assert report["checks"]["HEADER"]["result"] == "FAIL"


def test_validator_rejects_family_without_court_record(tmp_path):
    path = tmp_path / "wake_estate_2026-08-28.csv"
    _write_csv(path, [_base_row(
        estate_case_number="",
        relationship="DAUGHTER",
        heir_name="MAPLEWOOD, TESSA J",
        applicant_name="",
    )])
    report = validate_estate_import_file(path)
    assert report["overall"] == "FAIL"
    assert report["checks"]["COURT_RECORD_PRESENT"]["result"] == "FAIL"
    assert report["checks"]["FAMILY_IS_NOT_AUTHORITY"]["result"] == "FAIL"


def test_validator_rejects_legal_authority_claim(tmp_path):
    path = tmp_path / "wake_estate_2026-08-28.csv"
    _write_csv(path, [_base_row(relationship="LEGAL_AUTHORITY")])
    report = validate_estate_import_file(path)
    assert report["overall"] == "FAIL"
    assert report["checks"]["LEGAL_AUTHORITY_NOT_ASSERTED"]["result"] == "FAIL"


def test_validator_rejects_attorney_relationship(tmp_path):
    path = tmp_path / "wake_estate_2026-08-28.csv"
    _write_csv(path, [_base_row(
        applicant_name="",
        heir_name="SAMPLE, COUNSEL",
        relationship="ATTORNEY",
    )])
    report = validate_estate_import_file(path)
    assert report["overall"] == "FAIL"
    assert report["checks"]["FORBIDDEN_RELATIONSHIP"]["result"] == "FAIL"
    assert "ATTORNEY" in FORBIDDEN_RELATIONSHIPS
    assert parse_estate_import_rows([_base_row(
        applicant_name="",
        heir_name="SAMPLE, COUNSEL",
        relationship="ATTORNEY",
    )]) == []


def test_validator_rejects_decedent_as_living_target(tmp_path):
    path = tmp_path / "wake_estate_2026-08-28.csv"
    _write_csv(path, [_base_row(
        applicant_name="",
        heir_name="ALDERNEY, QUINCY P",
        relationship="HEIR",
    )])
    report = validate_estate_import_file(path)
    assert report["checks"]["DECEDENT_NOT_A_TARGET"]["result"] == "FAIL"
    assert report["overall"] == "FAIL"


def test_validator_rejects_bad_date_and_fips(tmp_path):
    path = tmp_path / "wake_estate_2026-08-28.csv"
    _write_csv(path, [_base_row(source_date="08/28/2026", fips="37183-WAKE")])
    report = validate_estate_import_file(path)
    assert report["columns"]["source_date"]["format"] == "FAIL"
    assert report["columns"]["fips"]["format"] == "FAIL"
    assert report["checks"]["DATE_FORMAT"]["result"] == "FAIL"
    assert report["overall"] == "FAIL"


def test_validator_rejects_wrong_wake_fips(tmp_path):
    path = tmp_path / "wake_estate_2026-08-28.csv"
    _write_csv(path, [_base_row(fips="37119")])
    report = validate_estate_import_file(path)
    assert report["checks"]["COUNTY_FIPS"]["result"] == "FAIL"


def test_validator_rejects_bom_and_tab(tmp_path):
    bom = tmp_path / "wake_estate_2026-08-28.csv"
    payload = FIXTURE.read_bytes()
    bom.write_bytes(b"\xef\xbb\xbf" + payload)
    bom_report = validate_estate_import_file(bom)
    assert bom_report["checks"]["ENCODING"]["result"] == "FAIL"

    tab = tmp_path / "wake_estate_2026-08-28.csv"
    rows = list(csv.DictReader(FIXTURE.open(encoding="utf-8")))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(ESTATE_IMPORT_COLUMNS), delimiter="\t")
    w.writeheader()
    w.writerows(rows)
    tab.write_text(buf.getvalue(), encoding="utf-8")
    tab_report = validate_estate_import_file(tab)
    assert tab_report["checks"]["DELIMITER"]["result"] == "FAIL"


def test_validator_rejects_phone_column_and_bad_filename(tmp_path):
    bad_name = tmp_path / "estate_dump.csv"
    _write_csv(bad_name, [_base_row()])
    named = validate_estate_import_file(bad_name)
    assert named["checks"]["FILENAME"]["result"] == "FAIL"

    extra = tmp_path / "wake_estate_2026-08-28.csv"
    cols = list(ESTATE_IMPORT_COLUMNS) + ["phone_1"]
    row = _base_row()
    row["phone_1"] = "7045550100"
    _write_csv(extra, [row], columns=cols)
    phones = validate_estate_import_file(extra)
    assert phones["checks"]["NO_PHONE_OR_DNC_COLUMNS"]["result"] == "FAIL"


def test_validator_rejects_missing_heir_mailing(tmp_path):
    path = tmp_path / "wake_estate_2026-08-28.csv"
    _write_csv(path, [_base_row(mailing_address="", mailing_city="", mailing_zip="")])
    report = validate_estate_import_file(path)
    assert report["checks"]["HEIR_MAILING_PRESENT"]["result"] == "FAIL"


def test_validator_cli_dry_run_does_not_import():
    proc = subprocess.run(
        [sys.executable, "scripts/validate_estate_import.py", "--file", str(FIXTURE)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env={k: v for k, v in os.environ.items() if k != "DATABASE_URL"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "imported: NO" in proc.stdout
    assert "OVERALL PASS" in proc.stdout
    assert "DATABASE_URL" not in proc.stderr
    assert "nccourts" not in proc.stdout.lower()


def test_operator_guide_documents_contract_and_downstream_without_execution():
    text = GUIDE.read_text(encoding="utf-8")
    assert "_raw/estate/" in text
    assert "wake_estate_YYYY-MM-DD.csv" in text or "wake_estate_{YYYY-MM-DD}.csv" in text
    assert "UTF-8" in text
    assert "YYYY-MM-DD" in text
    assert "property_candidate_id" in text
    assert "one operator click per county" in text
    assert "nccourts.gov" in text
    assert "LEGAL_AUTHORITY" in text
    assert "0.0000%" in text or "0 phone" in text
    assert "Mecklenburg" in text and "Durham" in text
    assert "match_person_to_voters" in text
    assert "project_contacts_to_deedsonar" in text
    assert "NOT_AUTHORIZED" in text
    assert "Do not run the free-phone enrichment" in text
    assert "import_packages/estate/estate_import_template.csv" in text
    for col in ESTATE_IMPORT_COLUMNS:
        assert col in text
    for field in LIVING_PERSON_FIELDS:
        assert field in text

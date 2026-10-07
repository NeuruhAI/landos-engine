"""AOC-E-201 / AOC-E-505 PDF intake — receipt per PDF, never a phone, idempotent."""
from __future__ import annotations

import csv
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from app.contact.estate_import import ESTATE_IMPORT_COLUMNS, load_estate_import_csv
from app.contact.estate_import_validate import validate_estate_import_file
from app.contact.estate_pdf_intake import RECEIPT_SUFFIX, detect_form, intake_estate_pdfs, parse_form, pdf_text

FIXTURE = Path(__file__).parent / "fixtures" / "estate" / "pdf" / "synthetic_aoc_e_201.pdf"
pdftotext_present = shutil.which("pdftotext") is not None


def _raw(tmp_path: Path) -> Path:
    raw = tmp_path / "_raw"
    (raw / "estate" / "pdf" / "wake").mkdir(parents=True)
    shutil.copy(FIXTURE, raw / "estate" / "pdf" / "wake" / "synthetic_aoc_e_201.pdf")
    return raw


def test_fixture_is_synthetic_and_names_the_form():
    text = FIXTURE.read_bytes().decode("latin-1")
    assert "AOC-E-201" in text
    for real in ("CUTHBERTSON", "CHAMBERS", "ASPDEN"):
        assert real not in text


@pytest.mark.skipif(not pdftotext_present, reason="poppler pdftotext not installed")
def test_parse_extracts_decedent_case_applicant_and_heirs_and_refuses_phones():
    text, parser = pdf_text(FIXTURE)
    assert parser == "pdftotext-layout"
    assert detect_form(text) == "AOC-E-201"
    parsed = parse_form(text, "AOC-E-201")
    assert parsed["decedent_name"] == "ALDERNEY, QUINCY P"
    assert parsed["estate_case_number"] == "26E000777-910"
    assert parsed["applicant_name"].startswith("MAPLEWOOD, TESSA J")
    heirs = parsed["heirs"]
    assert [h["heir_name"] for h in heirs] == ["RIVERBEND, OSCAR K", "STONECROFT, IRIS L"]
    assert heirs[0]["relationship"] == "SON" and heirs[1]["relationship"] == "DAUGHTER"
    assert heirs[0]["mailing_address"] == "300 FIXTURE AVE" and heirs[0]["mailing_city"] == "CHARLOTTE"
    assert heirs[0]["mailing_state"] == "NC" and heirs[0]["mailing_zip"] == "28205"
    assert parsed["attorney_names_ignored"] >= 1
    assert parsed["phones_seen_and_refused"] >= 1
    assert "555" not in json.dumps(heirs)


@pytest.mark.skipif(not pdftotext_present, reason="poppler pdftotext not installed")
def test_intake_writes_importer_rows_with_a_receipt_and_is_idempotent(tmp_path: Path):
    raw = _raw(tmp_path)
    lookup = {"ALDERNEYQUINCYP": ("synthetic-wake-estate-001", "SYNTH-WAKE-0001")}
    receipts = intake_estate_pdfs(raw, today=date(2026, 9, 2), candidate_lookup=lookup)
    assert len(receipts) == 1
    r = receipts[0]
    assert r.state == "ROWS_WRITTEN" and r.form == "AOC-E-201" and r.case_number == "26E000777-910"
    assert r.people_written == 3  # applicant + 2 heirs
    assert r.phones_seen_and_refused >= 1 and r.attorney_names_ignored >= 1
    receipt_path = raw / "estate" / "pdf" / "wake" / ("synthetic_aoc_e_201.pdf" + RECEIPT_SUFFIX)
    assert receipt_path.exists()
    stored = json.loads(receipt_path.read_text())
    assert stored["sha256"] == r.sha256 and stored["people_written"] == 3
    csv_path = raw / "estate" / "wake_estate_2026-09-02.csv"
    with csv_path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        assert list(rows[0].keys()) == list(ESTATE_IMPORT_COLUMNS)
    assert len(rows) == 3
    assert all(row["property_candidate_id"] == "synthetic-wake-estate-001" for row in rows)
    assert all(row["decedent_name"] == "ALDERNEY, QUINCY P" for row in rows)
    assert {row["relationship"] for row in rows} == {"APPLICANT", "SON", "DAUGHTER"}
    joined = json.dumps(rows)
    assert "555" not in joined and "COUNSEL" not in joined
    # the importer contract accepts the file and emits living people only
    report = validate_estate_import_file(csv_path, expect_placement=True)
    assert report["overall"] == "PASS", report["checks"]
    people = load_estate_import_csv(csv_path)
    assert len(people) == 3 and all(p.person.deceased is False for p in people)
    # a second run writes nothing and re-reads nothing
    again = intake_estate_pdfs(raw, today=date(2026, 9, 2), candidate_lookup=lookup)
    assert again == []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        assert len(list(csv.DictReader(fh))) == 3


@pytest.mark.skipif(not pdftotext_present, reason="poppler pdftotext not installed")
def test_unbound_decedent_still_gets_rows_and_an_honest_receipt(tmp_path: Path):
    raw = _raw(tmp_path)
    receipts = intake_estate_pdfs(raw, today=date(2026, 9, 2))
    assert receipts[0].state == "ROWS_WRITTEN_CANDIDATE_UNBOUND"
    assert "property_candidate_id" in receipts[0].note


def test_a_pdf_without_a_text_layer_writes_a_receipt_and_no_rows(tmp_path: Path):
    raw = tmp_path / "_raw"
    (raw / "estate" / "pdf" / "wake").mkdir(parents=True)
    (raw / "estate" / "pdf" / "wake" / "scan.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
    receipts = intake_estate_pdfs(raw, today=date(2026, 9, 2))
    assert len(receipts) == 1
    assert receipts[0].state in ("PARSE_TEXT_EMPTY", "PARSER_UNAVAILABLE(pdftotext)", "PARSER_ERROR(pdftotext rc=1)")
    assert receipts[0].people_written == 0
    assert not (raw / "estate" / "wake_estate_2026-09-02.csv").exists()


def test_template_named_in_upper_case_is_skipped_by_the_runner_glob():
    src = (Path(__file__).parents[1] / "scripts" / "run_nc_free_phone_enrichment.py").read_text()
    assert 'csv_path.name.lower().endswith("template.csv")' in src
    assert "intake_estate_pdfs(raw_dir)" in src

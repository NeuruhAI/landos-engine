"""Controlled synthetic estate PDF fixture court.

PDF → estate CSV → import contract → LandOS candidate → recipe → contact state.

Skip-trace OFF. No Tracerfy. No real PII. Attorney phones never accepted.
Does not write landos-ingest, deedsonar, recipe-engine, or neuruh-property-scan.
Does not unfreeze fulfillment pin 1779abf2. Does not expose scoring weights.
"""
from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from app.contact.estate_pdf_fixture_path import (
    ESTATE_IMPORT_NO_PHONE_STATE,
    MISSION_ID,
    SYNTHETIC_APN,
    SYNTHETIC_CANDIDATE_ID,
    assert_no_score_weights,
    run_estate_pdf_fixture_path,
    synthetic_landos_candidate,
)
from app.contact.resolution import CALLABILITY_NOT_AUTHORIZED
from app.resolution.router import Q_ESTATE_ID_FOUND
from tests.test_opportunity_intelligence import recipe

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "tests" / "fixtures" / "estate" / "pdf" / "synthetic_aoc_e_201.pdf"
WORKLIST = ROOT / "tests" / "fixtures" / "estate" / "synthetic_wake_worklist.csv"
FIXTURE_MODULE = ROOT / "app" / "contact" / "estate_pdf_fixture_path.py"
OI_MODULE = ROOT / "app" / "opportunity_intelligence.py"
FULFILLMENT_PIN = "1779abf265814f5595e1153a531c4a0ee09a0faa"
pdftotext_present = shutil.which("pdftotext") is not None

SYNTHETIC_SURNAMES = {
    "ALDERNEY", "MAPLEWOOD", "FAIRHAVEN", "RIVERBEND",
    "STONECROFT", "NORTHFORK", "CEDARHILL", "WILLOWBEND", "SAMPLE",
}
FORBIDDEN_REAL = ("CUTHBERTSON", "CHAMBERS", "ASPDEN", "POWELL", "GOODSON")


def _raw(tmp_path: Path) -> Path:
    raw = tmp_path / "_raw"
    (raw / "estate" / "pdf" / "wake").mkdir(parents=True)
    shutil.copy(PDF, raw / "estate" / "pdf" / "wake" / "synthetic_aoc_e_201.pdf")
    return raw


def test_fixture_module_never_imports_tracerfy_or_skip_trace():
    src = FIXTURE_MODULE.read_text(encoding="utf-8")
    assert "import tracerfy" not in src
    assert "skip_trace_tracerfy" not in src
    assert "TRACERFY_API_KEY" not in src
    assert '"skip_trace": "OFF"' in src


def test_opportunity_intelligence_module_is_untouched():
    dirty = subprocess.run(
        ["git", "diff", "HEAD", "--", "app/opportunity_intelligence.py"],
        cwd=ROOT, text=True, capture_output=True, check=True,
    )
    assert dirty.stdout == ""
    cached = subprocess.run(
        ["git", "show", f"{FULFILLMENT_PIN}:app/opportunity_intelligence.py"],
        cwd=ROOT, text=True, capture_output=True, check=True,
    )
    assert cached.stdout == OI_MODULE.read_text(encoding="utf-8")


def test_synthetic_candidate_is_wake_and_nameless_of_real_people():
    cand = synthetic_landos_candidate()
    assert cand["candidate_id"] == SYNTHETIC_CANDIDATE_ID
    assert cand["apn"] == SYNTHETIC_APN
    assert cand["county"] == "Wake" and cand["fips"] == "37183"
    blob = json.dumps(cand)
    for name in FORBIDDEN_REAL:
        assert name not in blob
    assert "ALDERNEY" in cand["owner_name"]


def test_worklist_binds_synthetic_decedent_only():
    rows = list(csv.DictReader(WORKLIST.open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["candidate_id"] == SYNTHETIC_CANDIDATE_ID
    text = WORKLIST.read_text(encoding="utf-8")
    for name in FORBIDDEN_REAL:
        assert name not in text


@pytest.mark.skipif(not pdftotext_present, reason="poppler pdftotext not installed")
def test_pdf_through_recipe_to_contact_state(tmp_path: Path):
    raw = _raw(tmp_path)
    before = set(sys.modules)
    out = run_estate_pdf_fixture_path(
        raw, recipe=recipe(), today=date(2026, 9, 2),
    )
    after = set(sys.modules) - before
    assert not any("tracerfy" in name.lower() or "skip_trace" in name for name in after)

    assert out["mission_id"] == MISSION_ID
    assert out["status"] == "LIVE_PROVEN"
    assert out["skip_trace"] == "OFF"
    assert out["tracerfy"] == "NOT_IMPORTED"
    assert out["legal_authority_written"] is False

    pdf = out["pdf_intake"]
    assert pdf["state"] == "ROWS_WRITTEN"
    assert pdf["form"] == "AOC-E-201"
    assert pdf["case_number"] == "26E000777-910"
    assert pdf["decedent_name"] == "ALDERNEY, QUINCY P"
    assert pdf["people_written"] == 3
    assert pdf["attorney_names_ignored"] >= 1
    assert pdf["phones_seen_and_refused"] >= 1

    contract = out["import_contract"]
    assert contract["overall"] == "PASS"
    assert contract["imported"] is False
    assert contract["bound_people"] == 3
    assert contract["phone_digits_in_csv"] is False
    csv_path = Path(contract["csv_path"])
    csv_text = csv_path.read_text(encoding="utf-8")
    assert "555" not in csv_text
    assert "COUNSEL" not in csv_text
    for name in FORBIDDEN_REAL:
        assert name not in csv_text
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert {row["property_candidate_id"] for row in rows} == {SYNTHETIC_CANDIDATE_ID}
    living_surnames = set()
    for row in rows:
        for field in ("applicant_name", "heir_name", "personal_representative_name"):
            if row.get(field):
                living_surnames.add(row[field].split(",")[0].strip())
    assert living_surnames <= SYNTHETIC_SURNAMES
    assert "ALDERNEY" not in living_surnames

    cand = out["landos_candidate"]
    assert cand["candidate_id"] == SYNTHETIC_CANDIDATE_ID
    assert cand["database_written"] is False
    assert cand["owner_class"] == "DECEASED"

    contact = out["contact_state"]
    assert contact["contact_resolution_state"] == ESTATE_IMPORT_NO_PHONE_STATE
    assert contact["phone_present"] == "NO"
    assert contact["callability_status"] == CALLABILITY_NOT_AUTHORIZED
    assert contact["has_living_identity"] is True
    assert contact["attorney_phone_accepted"] is False
    assert contact["dnc_status"] in {"UNKNOWN", "UNKNOWN_NOT_EVALUATED"}

    opp = out["opportunity"]
    assert opp["subject_id"] == SYNTHETIC_CANDIDATE_ID
    assert opp["next_best_action"]["execution_authority"] == "NONE"
    assert opp["next_best_action"]["action"] == "ESTABLISH_LEGAL_AUTHORITY"
    assert opp["next_best_source"]["origin"].startswith("next_best_source.")
    assert opp["next_best_source"]["operator_queue"] == Q_ESTATE_ID_FOUND
    assert opp["readiness"]["system_may_contact"] is False
    assert opp["readiness"]["system_may_make_offer"] is False
    assert opp["safety"]["seller_contact_performed"] is False
    assert opp["safety"]["offer_sent_or_made"] is False
    assert opp["safety"]["execution_authority_granted"] is False

    assert_no_score_weights(out)
    dumped = json.dumps(out)
    for needle in ("points_if_present", "points_if_absent", "fusion_weight"):
        assert needle not in dumped
    for name in FORBIDDEN_REAL:
        assert name not in dumped

    invariants = out["invariants"]
    assert invariants["skip_trace"] == "OFF"
    assert invariants["tracerfy_called"] is False
    assert invariants["attorney_phones_accepted"] is False
    assert invariants["legal_authority_asserted"] is False
    assert invariants["score_internals_projected"] is False
    assert invariants["real_pii"] is False
    assert invariants["fulfillment_pin_unfrozen"] is False
    assert invariants["property_scan_written"] is False


def test_next_child_points_at_property_scan_sellable_bind():
    text = (ROOT / "NEXT_CHILD_MISSION_ID").read_text(encoding="utf-8").strip()
    assert text == "NEURUH-PROPERTY-SCAN-SELLABLE-BIND-20260902"

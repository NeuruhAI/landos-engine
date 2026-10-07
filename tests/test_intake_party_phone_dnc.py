"""A-PRIME party-phone DNC intake. Synthetic fixtures only. No vendor. No live DB."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app.capacity_plan import TRACE_COST_PER_CREDIT, plan_capacity
from scripts.intake_party_phone_dnc import (
    WRITE_CCR_SQL,
    WRITE_PHONE_SQL,
    assert_no_phone1_write,
    is_synthetic_555,
    july_phone_entry,
    lead_level_status,
    plan_from_staged_csv,
    project_owner_leads_meta,
    scrub_receipt,
)
from scripts.intake_party_phone_dnc import main as intake_main


JULY_META = {
    "phones": [{
        "type": "mobile",
        "number": "+17085550101",
        "source": "truepeoplesearch",
        "confidence": 0.9,
        "dnc_status": "clear",
        "verified_2x": True,
        "dnc_scrubbed_at": "2026-07-15T03:00:42.109707Z",
        "dnc_verified_via": "tracerfy",
    }],
    "dnc_status": "clear",
    "scrub_receipt": {
        "source": "ccT3",
        "dnc_flags": {},
        "csv_sha256": "bcb698a9deadbeef",
        "scrubbed_at": "2026-07-15T03:00:42.109707Z",
        "dnc_phone_hits": 0,
        "clean_phone_hits": 1,
    },
    "import_provenance": {
        "tos": "Tracerfy internal-use-only; never exported/resold",
        "scrub": "ccT3 national/state DNC scrub, PASS-only",
        "source": "tracerfy_20260713",
        "csv_sha256": "bcb698a9deadbeef",
        "status_set": "traced_paid",
    },
}


def _csv(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "staged.csv"
    fields = ["target_key", "full_name", "phone", "phone_type", "confidence", "source",
              "pulled_at", "scrub_status", "scrub_flags", "scrubbed_at"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})
    return path


CAND_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
CAND_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"


def test_july_shape_projection_of_254_contract():
    projected = project_owner_leads_meta(JULY_META)
    assert projected["lead_dnc_status"] == "clear"
    assert projected["phones"][0]["dnc_status"] == "clear"
    assert projected["phones"][0]["dnc_scrubbed_at"]
    assert projected["scrub_receipt"]["csv_sha256"] == "bcb698a9deadbeef"
    assert set(projected["phones"][0]) >= {
        "type", "number", "source", "confidence", "dnc_status",
        "verified_2x", "dnc_scrubbed_at", "dnc_verified_via",
    }


def test_three_conditions_and_mutation(tmp_path: Path):
    path = _csv(tmp_path, [{
        "target_key": CAND_A,
        "phone": "5171112222",
        "phone_type": "mobile",
        "scrub_status": "clean",
        "scrubbed_at": "2026-08-30T12:00:00Z",
    }])
    plan = plan_from_staged_csv(path)
    assert plan["clean_phones"] == 1
    assert plan["writes_acquisition_leads_phone"] is False
    assert plan["mints_owner_leads"] is False
    write = plan["planned"][0]
    assert write["lead_dnc_status"] == "clear"
    assert write["phones"][0]["dnc_status"] == "clear"
    assert write["phones"][0]["dnc_scrubbed_at"]
    assert write["scrub_receipt"]["csv_sha256"] == plan["csv_sha256"]

    # missing timestamp cannot be clear
    bad = _csv(tmp_path, [{
        "target_key": CAND_B,
        "phone": "5173334444",
        "scrub_status": "clean",
        "scrubbed_at": "",
    }])
    with pytest.raises(RuntimeError, match="without dnc_scrubbed_at"):
        plan_from_staged_csv(bad)

    # mutation: flip to do_not_call → lead-level blocked, would leave callable_leads
    blocked = _csv(tmp_path, [{
        "target_key": CAND_A,
        "phone": "5171112222",
        "scrub_status": "dnc",
        "scrubbed_at": "2026-08-30T12:00:01Z",
    }])
    flipped = plan_from_staged_csv(blocked)
    assert flipped["planned"][0]["lead_dnc_status"] == "do_not_call"
    assert flipped["planned"][0]["phones"][0]["dnc_status"] == "do_not_call"
    assert flipped["planned"][0]["lead_dnc_status"] != "clear"


def test_555_never_clear(tmp_path: Path):
    path = _csv(tmp_path, [{
        "target_key": CAND_A,
        "phone": "+17085550101",
        "scrub_status": "clean",
        "scrubbed_at": "2026-07-15T03:00:42.109707Z",
    }])
    assert is_synthetic_555("+17085550101")
    plan = plan_from_staged_csv(path)
    assert plan["planned"][0]["phones"][0]["dnc_status"] == "unchecked"
    assert plan["planned"][0]["lead_dnc_status"] == "unchecked"


def test_pass_only_and_idempotent_hash(tmp_path: Path):
    rows = [{
        "target_key": CAND_A,
        "phone": "5171112222",
        "scrub_status": "clean",
        "scrubbed_at": "2026-08-30T12:00:00Z",
    }]
    path = _csv(tmp_path, rows)
    a = plan_from_staged_csv(path)
    b = plan_from_staged_csv(path)
    assert a["csv_sha256"] == b["csv_sha256"]
    assert a["clean_phones"] == 1


def test_never_writes_phone_1():
    assert_no_phone1_write(WRITE_PHONE_SQL)
    assert_no_phone1_write(WRITE_CCR_SQL)
    with pytest.raises(RuntimeError):
        assert_no_phone1_write("update public.acquisition_leads set phone_1 = 'x'")


def test_apply_refused_without_founder(tmp_path: Path):
    path = _csv(tmp_path, [{
        "target_key": CAND_A,
        "phone": "5171112222",
        "scrub_status": "clean",
        "scrubbed_at": "2026-08-30T12:00:00Z",
    }])
    rc = intake_main(["--csv", str(path), "--apply"])
    assert rc == 78


def test_price_922_scrub_and_4998_trace_no_purchase():
    scrub = 922 * TRACE_COST_PER_CREDIT
    assert round(scrub, 2) == 18.44
    plan = plan_capacity(pool_available=4998, target=4998, dnc=True)
    assert plan["approval_gate"]["approved"] is False
    assert plan["spend_usd"]["trace"]["max"] == 99.96
    assert plan["spend_usd"]["trace"]["min"] == 59.96  # 4998 * 0.60 * 0.02
    # DNC on resulting phones is separate from the 922 already-held numbers
    assert plan["spend_usd"]["dnc_scrub"]["min"] >= 0
    assert plan["mode"].startswith("DRY-RUN")

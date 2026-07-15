"""Exact-48 DNC-only scrub rails: immutable cohort, cost, and receipts."""

import sys
from collections import Counter
from decimal import Decimal

import pytest

import scripts.scrub_crm_phone_unknowns as scrub
from scripts.scrub_crm_phone_unknowns import (
    AUTHORIZED_CAP_USD,
    BELINDA_ID,
    COHORT_SQL,
    EXPECTED_COST_USD,
    KNOWN_DNC_ID,
    SMOKE_ID,
    assert_paid_preflight,
    classify_vendor_result,
    cost_for,
    nanp_key,
    validate_cohort,
    write_receipts,
)


def cohort_rows():
    rows = []
    for i in range(45):
        rows.append({
            "target_key": f"00000000-0000-4000-8000-{i:012d}",
            "owner_lead_id": f"10000000-0000-4000-8000-{i:012d}",
            "phone": f"126955{i:05d}",
            "phone_type": "mobile",
            "dnc": None,
            "seller_contact_source": "phone_1_backfill",
        })
    rows.extend([
        {
            "target_key": SMOKE_ID, "owner_lead_id": None,
            "phone": "12696600001", "phone_type": "mobile", "dnc": None,
            "seller_contact_source": "(null)",
        },
        {
            "target_key": BELINDA_ID, "owner_lead_id": None,
            "phone": "12696600002", "phone_type": "mobile", "dnc": True,
            "seller_contact_source": "homeowner_direct",
        },
        {
            "target_key": KNOWN_DNC_ID, "owner_lead_id": None,
            "phone": "2696600003", "phone_type": "landline", "dnc": True,
            "seller_contact_source": "(null)",
        },
    ])
    return rows


def ready_stats():
    stats = validate_cohort(cohort_rows())
    stats.update({
        "schema_ready": True,
        "callable_members": 0,
        "cost_usd": cost_for(stats["unique_phones"]),
    })
    return stats


def test_exact_cohort_and_cost_are_locked():
    stats = ready_stats()
    assert stats["rows"] == 48
    assert stats["unique_phones"] == 48
    assert stats["linked"] == 45
    assert stats["unlinked"] == 3
    assert stats["provenance"] == Counter({
        "phone_1_backfill": 45, "homeowner_direct": 1, "(null)": 2,
    })
    assert stats["dnc_states"] == Counter({"null": 46, "true": 2})
    assert len(stats["phones"]) == len(set(stats["phones"])) == 48
    assert all(len(phone) == 10 for phone in stats["phones"])
    assert stats["cost_usd"] == EXPECTED_COST_USD == Decimal("0.96")
    assert_paid_preflight(stats, AUTHORIZED_CAP_USD)


def test_cohort_drift_and_legacy_false_fail_closed():
    with pytest.raises(RuntimeError, match="cohort drift"):
        validate_cohort(cohort_rows()[:-1])
    stats = ready_stats()
    stats["dnc_states"] = Counter({"false": 1, "null": 46, "true": 1})
    with pytest.raises(RuntimeError, match="legacy dnc=FALSE"):
        assert_paid_preflight(stats, AUTHORIZED_CAP_USD)


def test_national_prefix_alias_collision_fails_before_paid_preflight():
    rows = cohort_rows()
    rows[1]["phone"] = nanp_key(rows[0]["phone"])
    with pytest.raises(RuntimeError, match="distinct canonical"):
        validate_cohort(rows)


def test_cap_and_callable_membership_are_hard_stops():
    stats = ready_stats()
    with pytest.raises(RuntimeError, match="exceeds authorized"):
        assert_paid_preflight(stats, Decimal("2.01"))
    stats["callable_members"] = 1
    with pytest.raises(RuntimeError, match="still appear callable"):
        assert_paid_preflight(stats, AUTHORIZED_CAP_USD)


def test_vendor_truth_is_fail_closed():
    assert classify_vendor_result({
        "is_clean": True, "national_dnc": False, "state_dnc": False,
        "dma": False, "litigator": False,
    }) == ("clean", "")
    assert classify_vendor_result({
        "is_clean": True, "national_dnc": True, "state_dnc": False,
        "dma": False, "litigator": False,
    }) == ("dnc", "national_dnc")
    assert classify_vendor_result({}) == ("dnc", "vendor_not_clean")


def test_receipt_is_complete_private_and_never_overwritten(tmp_path):
    rows = cohort_rows()
    vendor = {
        nanp_key(r["phone"]): {
            "is_clean": i % 2 == 0,
            "national_dnc": i % 2 == 1,
            "state_dnc": False, "dma": False, "litigator": False,
        }
        for i, r in enumerate(rows)
    }
    out = tmp_path / "exact48.csv"
    receipt = write_receipts(rows, vendor, out, "queue-1", EXPECTED_COST_USD)
    assert receipt["clean"] == 24
    assert receipt["dnc"] == 24
    assert receipt["submitted_rows"] == 48
    assert out.stat().st_mode & 0o777 == 0o600
    assert receipt["manifest"].stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        write_receipts(rows, vendor, out, "queue-2", EXPECTED_COST_USD)


def test_receipt_mismatch_names_queue_and_exact_sets_without_writing(tmp_path):
    rows = cohort_rows()
    vendor = {
        nanp_key(r["phone"]): {
            "is_clean": True,
            "national_dnc": False,
            "state_dnc": False,
            "dma": False,
            "litigator": False,
        }
        for r in rows
    }
    missing = nanp_key(rows[0]["phone"])
    del vendor[missing]
    extra = "9995550101"
    vendor[extra] = {
        "is_clean": True,
        "national_dnc": False,
        "state_dnc": False,
        "dma": False,
        "litigator": False,
    }
    out = tmp_path / "must-not-exist.csv"
    with pytest.raises(RuntimeError) as caught:
        write_receipts(rows, vendor, out, "2353", EXPECTED_COST_USD)
    message = str(caught.value)
    assert "queue_id=2353" in message
    assert f"missing=['{missing}']" in message
    assert f"extra=['{extra}']" in message
    assert not out.exists()
    assert not out.with_suffix(out.suffix + ".manifest.json").exists()
    assert not out.with_name(out.name + ".tmp").exists()


def test_resume_queue_is_get_only_and_needs_no_apply(monkeypatch, tmp_path):
    rows = cohort_rows()
    stats = ready_stats()
    vendor_rows = [
        {
            "phone": nanp_key(row["phone"]),
            "is_clean": "true",
            "national_dnc": "false",
            "state_dnc": "false",
            "dma": "false",
            "litigator": "false",
        }
        for row in rows
    ]
    calls = []

    def fake_poll(queue_id, key, cap=180, expected_phones=None):
        calls.append((queue_id, tuple(expected_phones or ())))
        return vendor_rows

    def forbidden_submit(*args, **kwargs):
        pytest.fail("resume reached the paid submit path")

    output = tmp_path / "queue2353.csv"
    monkeypatch.setattr(scrub, "read_plan", lambda: (rows, stats))
    monkeypatch.setattr(scrub, "poll_dnc", fake_poll)
    monkeypatch.setattr(scrub, "submit_dnc", forbidden_submit)
    monkeypatch.setenv("TRACERFY_API_KEY", "test-key")
    monkeypatch.setattr(
        sys,
        "argv",
        ["scrub_crm_phone_unknowns", "--resume-dnc-queue", "2353",
         "--output", str(output)],
    )
    scrub.main()
    assert calls == [("2353", tuple(stats["phones"]))]
    assert output.exists()
    assert output.with_suffix(output.suffix + ".manifest.json").exists()


@pytest.mark.parametrize(
    "extra_args",
    (["--apply"], ["--confirm-spend", "0.96"]),
)
def test_resume_rejects_paid_flags_before_any_read(monkeypatch, extra_args):
    monkeypatch.setattr(
        scrub,
        "read_plan",
        lambda: pytest.fail("invalid resume flags reached the database read"),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["scrub_crm_phone_unknowns", "--resume-dnc-queue", "2353", *extra_args],
    )
    with pytest.raises(SystemExit, match="STOP"):
        scrub.main()


def test_cohort_query_is_read_only_and_selects_non_spine_provenance():
    sql = " ".join(COHORT_SQL.lower().split())
    assert sql.startswith("select")
    assert "coalesce(c.source, '') <> 'spine_sync'" in sql
    assert not any(word in sql for word in (" update ", " insert ", " delete "))

"""Exact-48 DNC-only scrub rails: immutable cohort, cost, and receipts."""

from collections import Counter
from decimal import Decimal

import pytest

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
    validate_cohort,
    write_receipts,
)


def cohort_rows():
    rows = []
    for i in range(45):
        rows.append({
            "target_key": f"00000000-0000-4000-8000-{i:012d}",
            "owner_lead_id": f"10000000-0000-4000-8000-{i:012d}",
            "phone": f"26955{i:05d}",
            "phone_type": "mobile",
            "dnc": None,
            "seller_contact_source": "phone_1_backfill",
        })
    rows.extend([
        {
            "target_key": SMOKE_ID, "owner_lead_id": None,
            "phone": "2696600001", "phone_type": "mobile", "dnc": None,
            "seller_contact_source": "(null)",
        },
        {
            "target_key": BELINDA_ID, "owner_lead_id": None,
            "phone": "2696600002", "phone_type": "mobile", "dnc": None,
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
    assert stats["dnc_states"] == Counter({"null": 47, "true": 1})
    assert stats["cost_usd"] == EXPECTED_COST_USD == Decimal("0.96")
    assert_paid_preflight(stats, AUTHORIZED_CAP_USD)


def test_cohort_drift_and_legacy_false_fail_closed():
    with pytest.raises(RuntimeError, match="cohort drift"):
        validate_cohort(cohort_rows()[:-1])
    stats = ready_stats()
    stats["dnc_states"] = Counter({"false": 1, "null": 46, "true": 1})
    with pytest.raises(RuntimeError, match="legacy dnc=FALSE"):
        assert_paid_preflight(stats, AUTHORIZED_CAP_USD)


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
        "".join(ch for ch in r["phone"] if ch.isdigit()): {
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


def test_cohort_query_is_read_only_and_selects_non_spine_provenance():
    sql = " ".join(COHORT_SQL.lower().split())
    assert sql.startswith("select")
    assert "coalesce(c.source, '') <> 'spine_sync'" in sql
    assert not any(word in sql for word in (" update ", " insert ", " delete "))

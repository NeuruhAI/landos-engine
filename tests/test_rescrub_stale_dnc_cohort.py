"""FREE-SCRUB-41 re-scrub rails: pinned 39-lead cohort, $0.76 under a $1.00 cap, guarded spend."""

from decimal import Decimal

import pytest

import scripts.rescrub_stale_dnc_cohort as rescrub
import scripts.skip_trace_tracerfy as tracerfy
from app.free_first.guard import PaidCallBlocked
from scripts.rescrub_stale_dnc_cohort import (
    AUTHORIZED_CAP_USD,
    COHORT_IDS,
    COHORT_SQL,
    EXPECTED_COST_USD,
    assert_paid_preflight,
    validate_cohort,
    write_receipts,
)
from scripts.scrub_crm_phone_unknowns import nanp_key

CLEAN = {"is_clean": True, "national_dnc": False, "state_dnc": False, "dma": False, "litigator": False}


def cohort_rows():
    rows = []
    for i, lead_id in enumerate(sorted(COHORT_IDS)):
        rows.append({
            "target_key": lead_id,
            "owner_lead_id": f"10000000-0000-4000-8000-{i:012d}",
            # two leads share one number: 39 rows, 38 billable phones
            "phone": f"126966{min(i, 37):05d}",
            "phone_type": "mobile",
            "dnc": False,
            "stage": "archived",
            "is_fixture": False,
            "opted_out": False,
        })
    return rows


def ready_stats():
    stats = validate_cohort(cohort_rows())
    stats["callable_members"] = 0
    return stats


def test_cohort_is_39_rows_38_phones_76_cents():
    stats = ready_stats()
    assert stats["rows"] == 39
    assert stats["unique_phones"] == 38 == len(set(stats["phones"]))
    assert stats["cost_usd"] == EXPECTED_COST_USD == Decimal("0.76")
    assert_paid_preflight(stats, AUTHORIZED_CAP_USD)


def test_excluded_fixtures_are_not_in_the_pinned_cohort():
    assert not any(i.startswith(rescrub.EXCLUDED_ID_PREFIXES) for i in COHORT_IDS)
    assert len(COHORT_IDS) == 39


def test_cohort_drift_fails_closed():
    with pytest.raises(RuntimeError, match="cohort drift"):
        validate_cohort(cohort_rows()[:-1])
    rows = cohort_rows()
    rows[0]["target_key"] = "087a66d2-0000-4000-8000-000000000000"
    with pytest.raises(RuntimeError, match="cohort drift"):
        validate_cohort(rows)


@pytest.mark.parametrize("field,value,problem", [
    ("dnc", None, "dnc_not_false"),
    ("dnc", True, "dnc_not_false"),
    ("stage", "candidate", "not_archived"),
    ("owner_lead_id", None, "no_spine_row"),
    ("is_fixture", True, "test_or_synthetic"),
    ("opted_out", True, "opted_out"),
    ("phone", "2695551234", "nanp_555"),
])
def test_row_state_drift_fails_closed(field, value, problem):
    rows = cohort_rows()
    rows[5][field] = value
    with pytest.raises(RuntimeError, match=problem):
        validate_cohort(rows)


def test_cap_cost_and_callable_membership_are_hard_stops():
    stats = ready_stats()
    with pytest.raises(RuntimeError, match="exceeds authorized"):
        assert_paid_preflight(stats, Decimal("1.01"))
    with pytest.raises(RuntimeError, match="exceeds cap"):
        assert_paid_preflight(stats, Decimal("0.50"))
    stats["callable_members"] = 1
    with pytest.raises(RuntimeError, match="already appear callable"):
        assert_paid_preflight(stats, AUTHORIZED_CAP_USD)


def test_receipt_stamps_scrub_time_on_every_row_and_is_private(tmp_path):
    rows = cohort_rows()
    vendor = {nanp_key(r["phone"]): dict(CLEAN) for r in rows}
    vendor[nanp_key(rows[0]["phone"])]["national_dnc"] = True
    out = tmp_path / "rescrub39.csv"
    receipt = write_receipts(rows, vendor, out, "q-1", EXPECTED_COST_USD)
    assert receipt["submitted_rows"] == 39 and receipt["unique_phones"] == 38
    assert receipt["dnc_rows"] == 1 and receipt["clean_rows"] == 38
    lines = out.read_text().splitlines()
    assert len(lines) == 40
    stamp = receipt["generated_at_utc"]
    assert all(line.endswith("," + stamp) for line in lines[1:])
    assert out.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        write_receipts(rows, vendor, out, "q-2", EXPECTED_COST_USD)


def test_vendor_mismatch_writes_nothing(tmp_path):
    rows = cohort_rows()
    vendor = {nanp_key(r["phone"]): dict(CLEAN) for r in rows}
    del vendor[nanp_key(rows[0]["phone"])]
    out = tmp_path / "never.csv"
    with pytest.raises(RuntimeError, match="missing=1"):
        write_receipts(rows, vendor, out, "q-3", EXPECTED_COST_USD)
    assert not out.exists()


def test_dry_run_makes_no_network_call(monkeypatch, capsys):
    monkeypatch.setattr(rescrub, "read_plan", lambda: (cohort_rows(), ready_stats()))
    monkeypatch.setattr(rescrub, "submit_dnc", lambda *a, **k: pytest.fail("dry-run submitted"))
    monkeypatch.setattr(rescrub, "poll_dnc", lambda *a, **k: pytest.fail("dry-run polled"))
    rescrub.main([])
    assert "DRY-RUN" in capsys.readouterr().out


def test_confirm_spend_must_match_exactly(monkeypatch):
    monkeypatch.setattr(rescrub, "read_plan", lambda: (cohort_rows(), ready_stats()))
    monkeypatch.setattr(rescrub, "submit_dnc", lambda *a, **k: pytest.fail("submitted"))
    with pytest.raises(SystemExit, match="confirm-spend must equal 0.76"):
        rescrub.main(["--apply", "--confirm-spend", "1.00"])


def test_apply_without_governance_is_blocked_by_the_free_first_guard(monkeypatch):
    """The real submit_dnc runs; with no Governance authority the guard refuses
    before any byte reaches Tracerfy."""
    for var in ("LANDOS_FREE_FIRST_AUTHORITY", "LANDOS_FREE_FIRST_LEDGER",
                "LANDOS_FREE_FIRST_JOB", "LANDOS_FREE_FIRST_JOBS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TRACERFY_API_KEY", "test-key")
    monkeypatch.setattr(rescrub, "read_plan", lambda: (cohort_rows(), ready_stats()))
    monkeypatch.setattr(tracerfy, "_request", lambda *a, **k: pytest.fail("vendor request left the process"))
    with pytest.raises(PaidCallBlocked):
        rescrub.main(["--apply", "--confirm-spend", "0.76", "--cap-usd", "1.00"])


@pytest.mark.parametrize("extra", (["--apply"], ["--confirm-spend", "0.76"]))
def test_resume_rejects_paid_flags_before_any_read(monkeypatch, extra):
    monkeypatch.setattr(rescrub, "read_plan", lambda: pytest.fail("read before flag check"))
    with pytest.raises(SystemExit, match="GET-only"):
        rescrub.main(["--resume-dnc-queue", "q-9", *extra])


def test_cohort_query_is_read_only_and_tenant_pinned():
    sql = " ".join(COHORT_SQL.lower().split())
    assert sql.startswith("select")
    assert "a.tenant_id = %s" in sql and "a.lead_id = any(%s::uuid[])" in sql
    assert not any(w in sql for w in (" update ", " insert ", " delete "))

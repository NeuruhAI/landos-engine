"""
test_intake_tracerfy_phones.py — the Tracerfy phone intake contract (JOB 5).
No DB, no network: the scrub-PASS filter, phone shaping, and SQL invariants that
keep the intake mirroring the spine writer (never a parallel CRM writer).
"""
import csv
import inspect
import io
import re
import sys

import pytest

import scripts.intake_tracerfy_phones as intake
from scripts.intake_tracerfy_phones import (BRIDGE_SQL, KEY_CENSUS_SQL, WRITE_SQL,
                                             SOURCE_STAMP,
                                             SYNC_THRESHOLD, build_phones,
                                             census_target_keys,
                                             confidence_for, is_scrub_pass,
                                             load_staged, norm_type,
                                             merge_clean_phone_evidence,
                                             select_key_convention,
                                             target_verdict)


# ---- scrub-PASS filter (fail-closed) -----------------------------------------------

def test_only_clean_is_scrub_pass():
    assert is_scrub_pass({"scrub_status": "clean"})
    assert is_scrub_pass({"scrub_status": " CLEAN "})
    for bad in ("dnc", "", "unknown", "pending", None):
        assert not is_scrub_pass({"scrub_status": bad})   # fail-closed


SYNTH_CSV = """target_key,full_name,phone,phone_type,confidence,source,pulled_at,scrub_status,scrub_flags,scrubbed_at
T1,ALICE,5171112222,Mobile,,tracerfy,2026-07-13T00:00:00Z,clean,,2026-07-13T01:00:00Z
T1,ALICE,5171112222,mobile,,tracerfy,2026-07-13T00:00:00Z,clean,,2026-07-13T01:00:00Z
T1,ALICE,5173334444,Landline,,tracerfy,2026-07-13T00:00:00Z,clean,,2026-07-13T01:00:00Z
T2,BOB,5175556666,Mobile,,tracerfy,2026-07-13T00:00:00Z,dnc,national_dnc,2026-07-13T01:00:00Z
T3,CARLA,5177778888,Landline,,tracerfy,2026-07-13T00:00:00Z,dnc,national_dnc;state_dnc,2026-07-13T01:00:00Z
"""


def test_load_staged_retains_target_verdict_but_not_dnc_digits(tmp_path):
    p = tmp_path / "phones.csv"
    p.write_text(SYNTH_CSV)
    by_target, stats = load_staged(str(p))
    # 5 rows in: all targets retained for the verdict receipt; only T1 can
    # produce phone digits.
    assert stats["csv_rows"] == 5
    assert stats["scrub_pass_rows"] == 3
    assert stats["scrub_dropped_rows"] == 2
    assert set(by_target) == {"T1", "T2", "T3"}
    assert target_verdict(by_target["T1"]) == "clear"
    assert target_verdict(by_target["T2"]) == "do_not_call"
    assert target_verdict(by_target["T3"]) == "do_not_call"
    assert build_phones(by_target["T2"]) == []  # DNC digits never enter phones[]
    assert "national_dnc" in stats["dropped_flags"]
    assert stats["clean_targets"] == 1
    assert stats["dnc_only_targets"] == 2


def test_build_phones_dedupes_number_and_sorts_mobile_first():
    rows = [
        {"phone": "517-333-4444", "phone_type": "Landline", "scrub_status": "clean"},
        {"phone": "(517) 111-2222", "phone_type": "Mobile", "scrub_status": "clean"},
        {"phone": "5171112222", "phone_type": "mobile", "scrub_status": "clean"},  # dup
    ]
    ph = build_phones(rows)
    assert [p["number"] for p in ph] == ["+15171112222", "+15173334444"]  # mobile first, deduped
    assert ph[0]["type"] == "mobile" and ph[0]["confidence"] >= SYNC_THRESHOLD
    assert all(p["source"] == SOURCE_STAMP for p in ph)
    assert all(p["verified_2x"] is False for p in ph)      # single source


def test_mixed_target_projects_only_clean_phone_and_stays_clear():
    rows = [
        {"phone": "517-111-2222", "phone_type": "Mobile", "scrub_status": "dnc"},
        {"phone": "517-333-4444", "phone_type": "Landline", "scrub_status": "clean"},
    ]
    assert target_verdict(rows) == "clear"
    assert [p["number"] for p in build_phones(rows)] == ["+15173334444"]


def test_existing_phone_set_must_exactly_match_clean_receipt():
    existing = [
        {"number": "+1 (517) 333-4444", "source": "prior"},
        {"number": "5171112222", "source": "prior"},
    ]
    same_receipt = [
        {"number": "+15171112222", "source": SOURCE_STAMP},
        {"number": "+15173334444", "source": SOURCE_STAMP},
    ]
    merged = merge_clean_phone_evidence(
        "T1", existing, same_receipt, "2026-07-15T00:00:00Z")
    assert all(phone["dnc_status"] == "clear" for phone in merged)


def test_existing_unreturned_phone_is_preserved_but_not_blessed_clear():
    existing = [
        {"number": "+15171112222"},
        {"number": "+15179990000"},
    ]
    receipt = [{"number": "+15171112222"}]
    merged = merge_clean_phone_evidence(
        "T1", existing, receipt, "2026-07-15T00:00:00Z")
    assert len(merged) == 2
    clean = [phone for phone in merged if phone.get("dnc_status") == "clear"]
    unknown = [phone for phone in merged if phone.get("dnc_status") != "clear"]
    assert [phone["number"] for phone in clean] == ["+15171112222"]
    assert [phone["number"] for phone in unknown] == ["+15179990000"]


def test_receipt_phone_missing_from_existing_meta_stops():
    existing = [{"number": "+15171112222"}]
    receipt = [{"number": "+15173334444", "dnc_status": "clear"}]
    with pytest.raises(RuntimeError, match="missing_from_existing"):
        merge_clean_phone_evidence(
            "T1", existing, receipt, "2026-07-15T00:00:00Z")


def test_confidence_clears_the_sync_threshold_and_mobile_outranks_landline():
    assert confidence_for("mobile") >= SYNC_THRESHOLD
    assert confidence_for("landline") >= SYNC_THRESHOLD
    assert confidence_for("mobile") > confidence_for("landline")
    assert confidence_for(None) >= SYNC_THRESHOLD          # default still callable


def test_norm_type_normalizes_case():
    assert norm_type("Mobile") == "mobile" and norm_type("LANDLINE") == "landline"


# ---- SQL invariants: mirror the spine writer, never the CRM ------------------------

def _flat(s):
    return re.sub(r"\s+", " ", s.lower())


def test_write_targets_owner_leads_only_and_guards_concurrent_meta():
    s = _flat(WRITE_SQL)
    assert "update landos.owner_leads" in s
    assert "public.acquisition_leads" not in s            # sync_to_crm is the sole CRM writer
    assert "skip_trace_status = 'traced_paid'" in s
    assert "skip_trace_meta is not distinct from %(prior_meta)s" in s
    assert "tenant_id = %(tenant)s" in s                  # tenant-qualified
    assert "delete" not in s


def test_bridge_maps_target_key_through_source_lead_id():
    s = _flat(BRIDGE_SQL)
    assert "a.lead_id::text" in s and "a.source_lead_id::text" in s
    assert "left join landos.owner_leads ol" in s
    assert "on ol.lead_id = a.source_lead_id" in s
    assert "a.tenant_id = %(tenant)s" in s
    assert "a.dnc" in s                                    # consent surface read


# ---- target-key namespace census ---------------------------------------------------

@pytest.mark.parametrize(
    ("winner", "census"),
    [
        ("crm_lead_id", {
            "target_keys": 2, "crm_lead_id": 2,
            "crm_source_lead_id": 0, "landos_owner_lead_id": 0,
        }),
        ("crm_source_lead_id", {
            "target_keys": 2, "crm_lead_id": 0,
            "crm_source_lead_id": 2, "landos_owner_lead_id": 0,
        }),
        ("landos_owner_lead_id", {
            "target_keys": 2, "crm_lead_id": 0,
            "crm_source_lead_id": 0, "landos_owner_lead_id": 2,
        }),
    ],
)
def test_key_census_fixture_picks_each_unambiguous_convention(winner, census):
    assert select_key_convention(census) == winner


@pytest.mark.parametrize(
    "census",
    [
        {
            "target_keys": 2, "crm_lead_id": 2,
            "crm_source_lead_id": 2, "landos_owner_lead_id": 0,
        },
        {
            "target_keys": 2, "crm_lead_id": 1,
            "crm_source_lead_id": 1, "landos_owner_lead_id": 1,
        },
        {
            "target_keys": 2, "crm_lead_id": 1,
            "crm_source_lead_id": 0, "landos_owner_lead_id": 0,
        },
    ],
)
def test_key_census_refuses_ambiguous_mixed_or_incomplete_input(census):
    with pytest.raises(RuntimeError, match="ambiguous, mixed, or incomplete"):
        select_key_convention(census)


def test_key_census_counts_all_three_candidates_before_bridge(capsys):
    class FakeCursor:
        def __init__(self):
            self.calls = []

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchone(self):
            return {
                "target_keys": 2,
                "crm_lead_id": 2,
                "crm_source_lead_id": 0,
                "landos_owner_lead_id": 0,
            }

    cur = FakeCursor()
    census, convention = census_target_keys(cur, ["crm-1", "crm-2"])
    assert convention == "crm_lead_id"
    assert census["target_keys"] == 2
    assert len(cur.calls) == 1
    assert cur.calls[0][0] == KEY_CENSUS_SQL
    assert cur.calls[0][1]["keys"] == ["crm-1", "crm-2"]
    receipt = capsys.readouterr().out
    assert "public.acquisition_leads.lead_id" in receipt
    assert "acquisition_leads.source_lead_id" in receipt
    assert "landos.owner_leads.lead_id" in receipt


def test_key_census_sql_and_bridge_preserve_canonical_hop():
    census_sql = _flat(KEY_CENSUS_SQL)
    bridge_sql = _flat(BRIDGE_SQL)
    assert "a.lead_id::text = k.target_key" in census_sql
    assert "a.source_lead_id::text = k.target_key" in census_sql
    assert "ol.lead_id::text = k.target_key" in census_sql
    assert "a.lead_id::text = any(%(keys)s)" in bridge_sql
    assert "on ol.lead_id = a.source_lead_id" in bridge_sql


# ---- CLI and transaction gates -----------------------------------------------------

@pytest.mark.parametrize(
    ("extra_args", "expected_dry_run"),
    [
        ([], True),
        (["--apply"], False),
    ],
)
def test_apply_is_the_only_cli_write_gate(
    monkeypatch, tmp_path, extra_args, expected_dry_run,
):
    staged = tmp_path / "phones.csv"
    staged.write_text(SYNTH_CSV)
    observed = {}

    def fake_run(csv_path, dry_run=True, limit=None):
        observed.update(csv_path=csv_path, dry_run=dry_run, limit=limit)

    monkeypatch.setattr(intake, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        ["intake_tracerfy_phones", "--csv", str(staged), *extra_args],
    )
    intake.main()
    assert observed == {
        "csv_path": str(staged),
        "dry_run": expected_dry_run,
        "limit": None,
    }


def test_default_dry_run_rolls_back_and_write_sql_is_apply_guarded():
    source = inspect.getsource(intake.run)
    write_guard = source.index("if not dry_run:")
    write_execute = source.index("cur.execute(WRITE_SQL")
    rollback = source.index("conn.rollback()")
    commit = source.index("conn.commit()")
    assert write_guard < write_execute
    assert rollback < commit

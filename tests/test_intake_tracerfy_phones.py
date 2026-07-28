"""
test_intake_tracerfy_phones.py — the Tracerfy phone intake contract (JOB 5).
No DB, no network: the scrub-PASS filter, phone shaping, and SQL invariants that
keep the intake mirroring the spine writer (never a parallel CRM writer).
"""
import csv
import io
import re

from scripts.intake_tracerfy_phones import (BRIDGE_SQL, WRITE_SQL, SOURCE_STAMP,
                                             SYNC_THRESHOLD, build_phones,
                                             confidence_for, is_scrub_pass,
                                             load_staged, norm_type,
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
    assert "a.dnc" in s                                    # consent surface read

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
from scripts.intake_tracerfy_phones import (BRIDGE_SQL, KEY_CENSUS_SQL,
                                             WRITE_SQL, CRM_LEAD_ID,
                                             LANDOS_LEAD_ID, SOURCE_STAMP,
                                             SYNC_THRESHOLD, build_phones,
                                             confidence_for, is_scrub_pass,
                                             load_staged, norm_type,
                                             merge_clean_phone_evidence,
                                             select_key_convention,
                                             summarize_key_census,
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


@pytest.mark.parametrize(
    ("filename", "csv_text", "expected_column"),
    [
        (
            "skiptrace_phones.csv",
            "target_key,phone,scrub_status\nCRM-1,5171112222,clean\n",
            "target_key",
        ),
        (
            "tracerfy_resolved_fixture.csv",
            "lead_id,phone_1,dnc_status\nLANDOS-1,5171112222,unchecked\n",
            "lead_id",
        ),
    ],
    ids=["staged-crm-key", "resolved-landos-key"],
)
def test_load_staged_reads_existing_key_columns_without_rekey(
    tmp_path, filename, csv_text, expected_column,
):
    path = tmp_path / filename
    path.write_text(csv_text)
    by_target, stats = load_staged(str(path))
    assert list(by_target) == [csv_text.splitlines()[1].split(",", 1)[0]]
    assert stats["key_column"] == expected_column


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


def test_module_contains_no_crm_mutation_statement():
    source = inspect.getsource(intake).lower()
    assert not re.search(r"\b(update|insert\s+into|delete\s+from)\s+public\.", source)


def test_bridge_maps_target_key_through_source_lead_id():
    s = _flat(BRIDGE_SQL)
    assert "a.lead_id::text" in s and "a.source_lead_id::text" in s
    assert "left join landos.owner_leads ol" in s
    assert "on ol.lead_id = a.source_lead_id" in s
    assert "a.dnc" in s                                    # consent surface read
    assert "a.tenant_id = %(tenant)s" in s


def _census_row(key, crm_pk, crm_source, landos):
    return {
        "target_key": key,
        "crm_lead_id_matches": crm_pk,
        "crm_source_lead_id_matches": crm_source,
        "landos_lead_id_matches": landos,
    }


@pytest.fixture(
    params=[
        {
            "convention": CRM_LEAD_ID,
            "rows": [
                _census_row("crm-key-1", 1, 0, 0),
                _census_row("crm-key-2", 1, 0, 0),
            ],
            "summary": (2, 0, 0),
        },
        {
            "convention": LANDOS_LEAD_ID,
            "rows": [
                _census_row("landos-key-1", 0, 1, 1),
                _census_row("landos-key-2", 0, 1, 1),
            ],
            "summary": (0, 2, 2),
        },
    ],
    ids=["staged-crm-lead-id", "resolved-landos-lead-id"],
)
def key_convention_fixture(request):
    return request.param


def test_census_fixture_selects_each_real_estate_convention(key_convention_fixture):
    fixture = key_convention_fixture
    summary = summarize_key_census(fixture["rows"])
    assert (
        summary["crm_lead_id"],
        summary["crm_source_lead_id"],
        summary["landos_lead_id"],
    ) == fixture["summary"]
    assert select_key_convention(fixture["rows"]) == fixture["convention"]


@pytest.mark.parametrize(
    "rows",
    [
        [],  # empty
        [_census_row("collision", 1, 1, 1)],  # both namespaces
        [  # mixed file: individually valid keys, collectively ambiguous
            _census_row("crm-key", 1, 0, 0),
            _census_row("landos-key", 0, 1, 1),
        ],
        [_census_row("orphan-source", 0, 1, 0)],
        [_census_row("unmapped", 0, 0, 0)],
        [_census_row("duplicate-source", 0, 2, 1)],
    ],
)
def test_census_refuses_ambiguous_mixed_or_incomplete_files(rows):
    with pytest.raises(RuntimeError, match="STOP"):
        select_key_convention(rows)


def test_census_queries_all_three_tenant_scoped_candidate_keys():
    s = _flat(KEY_CENSUS_SQL)
    assert "public.acquisition_leads" in s
    assert "a.lead_id::text = k.target_key" in s
    assert "a.source_lead_id::text = k.target_key" in s
    assert "landos.owner_leads" in s
    assert "ol.lead_id::text = k.target_key" in s
    assert s.count("tenant_id = %(tenant)s") == 3


class _FakeCursor:
    def __init__(self, census_rows, bridge_rows):
        self.census_rows = census_rows
        self.bridge_rows = bridge_rows
        self.calls = []
        self._fetched = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if sql == KEY_CENSUS_SQL:
            self._fetched = self.census_rows
        elif sql == BRIDGE_SQL:
            self._fetched = self.bridge_rows
        else:
            raise AssertionError("dry-run executed an unexpected SQL statement")

    def fetchall(self):
        return list(self._fetched)


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rolled_back = False
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.rolled_back = True
        return False

    def cursor(self):
        return self._cursor

    def rollback(self):
        self.rolled_back = True

    def commit(self):
        self.committed = True


def test_run_censuses_full_file_before_limit_then_hops_to_owner(
    tmp_path, monkeypatch, capsys,
):
    first = "10000000-0000-4000-8000-000000000001"
    second = "20000000-0000-4000-8000-000000000002"
    owner = "90000000-0000-4000-8000-000000000009"
    csv_path = tmp_path / "staged.csv"
    csv_path.write_text(
        "target_key,full_name,phone,phone_type,scrub_status,scrub_flags,scrubbed_at\n"
        f"{first},ALICE,5171112222,Mobile,clean,,2026-07-13T01:00:00Z\n"
        f"{second},BOB,5173334444,Landline,clean,,2026-07-13T01:00:00Z\n"
    )
    census_rows = [
        _census_row(first, 1, 0, 0),
        _census_row(second, 1, 0, 0),
    ]
    bridge_rows = [{
        "target_key": first,
        "owner_lead_id": owner,
        "acq_dnc": False,
        "has_spine_row": True,
        "existing_phones": 0,
        "existing_dnc_status": None,
        "meta": None,
    }]
    cursor = _FakeCursor(census_rows, bridge_rows)
    connection = _FakeConnection(cursor)
    monkeypatch.setattr(
        intake.psycopg, "connect", lambda *args, **kwargs: connection
    )

    result = intake.run(str(csv_path), limit=1)

    assert result["key_convention"] == CRM_LEAD_ID
    assert result["key_census"]["target_keys"] == 2
    assert result["outcome"]["landed"] == 1
    assert [call[0] for call in cursor.calls] == [KEY_CENSUS_SQL, BRIDGE_SQL]
    assert cursor.calls[0][1]["keys"] == [first, second]  # full-file census
    assert cursor.calls[1][1]["keys"] == [first]          # limit after census
    assert connection.rolled_back and not connection.committed
    output = capsys.readouterr().out
    assert owner[:8] in output                             # planned hop result
    assert "public.acquisition_leads.lead_id:" in output
    assert "public.acquisition_leads.source_lead_id:" in output
    assert "landos.owner_leads.lead_id:" in output
    assert "2/2" in output and output.count("0/2") == 2


@pytest.mark.parametrize(
    ("census_rows", "error"),
    [
        ([_census_row("LANDOS-1", 0, 1, 1)], "scripts.import_resolved"),
        (
            [
                _census_row("CRM-1", 1, 0, 0),
                _census_row("LANDOS-1", 0, 1, 1),
            ],
            "ambiguous",
        ),
    ],
    ids=["recognized-landos-routes-away", "mixed-file-refused"],
)
def test_run_stops_after_census_before_bridge_for_non_staged_convention(
    tmp_path, monkeypatch, census_rows, error,
):
    csv_path = tmp_path / "input.csv"
    csv_path.write_text(
        "lead_id,phone_1,dnc_status\n"
        + "\n".join(f"{row['target_key']},5171112222,unchecked" for row in census_rows)
        + "\n"
    )
    cursor = _FakeCursor(census_rows, [])
    connection = _FakeConnection(cursor)
    monkeypatch.setattr(
        intake.psycopg, "connect", lambda *args, **kwargs: connection
    )

    with pytest.raises(RuntimeError, match=error):
        intake.run(str(csv_path))

    assert [call[0] for call in cursor.calls] == [KEY_CENSUS_SQL]
    assert connection.rolled_back and not connection.committed


@pytest.mark.parametrize(
    ("extra_args", "expected_dry_run"),
    [([], True), (["--apply"], False)],
    ids=["default-dry-run", "apply-is-sole-write-gate"],
)
def test_main_maps_apply_flag_to_write_gate(
    tmp_path, monkeypatch, extra_args, expected_dry_run,
):
    csv_path = tmp_path / "staged.csv"
    csv_path.write_text("target_key,phone,scrub_status\nCRM-1,5171112222,clean\n")
    calls = []
    monkeypatch.setattr(intake, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setattr(
        sys, "argv", ["intake_tracerfy_phones", "--csv", str(csv_path), *extra_args]
    )

    intake.main()

    assert calls[0][1]["dry_run"] is expected_dry_run

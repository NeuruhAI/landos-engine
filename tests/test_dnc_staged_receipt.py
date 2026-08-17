"""T4 — staged DNC receipt last-mile. Synthetic fixtures only. No Tracerfy. No live DB."""
from __future__ import annotations

import csv
import hashlib
import inspect
import json
import re
from pathlib import Path

import pytest

import scripts.dnc_staged_receipt as receipt_mod
import scripts.intake_tracerfy_phones as intake
import scripts.skip_trace_tracerfy as tracerfy
import scripts.sync_to_crm as sync
from scripts.dnc_staged_receipt import (
    CRM_TARGET_KEY_SQL,
    CSV_FIELDS,
    DncReceiptError,
    classify_vendor_result,
    compile_crm_target_key_map,
    persist_dnc_staged_receipt,
    persist_vendor_payload,
)
from scripts.intake_tracerfy_phones import (
    build_phones,
    is_scrub_pass,
    load_staged,
    merge_clean_phone_evidence,
    target_verdict,
)


OWNER_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
OWNER_B = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa2"
CRM_A = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
CRM_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb2"

CLEAN_PHONE = "5171112222"       # NXX 111 — not 555
DNC_PHONE = "5173334444"         # explicit vendor block
UNKNOWN_PHONE = "3134446677"     # vendor row present, no verdict
SYNTH_555 = "+15175550100"       # NANP 555 exchange — never callable
DUP_PHONE = "517-111-2222"       # alias of CLEAN_PHONE


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower())


def _clean_info(**overrides):
    info = {
        "is_clean": True,
        "national_dnc": False,
        "state_dnc": False,
        "dma": False,
        "litigator": False,
    }
    info.update(overrides)
    return info


def _recs():
    return [
        (
            {"lead_id": OWNER_A, "owner_name": "SYNTHETIC OWNER A"},
            {"phones": [
                {"number": CLEAN_PHONE, "type": "mobile", "confidence": 0.7, "dnc": False},
                {"number": DUP_PHONE, "type": "mobile", "confidence": 0.7, "dnc": False},
                {"number": DNC_PHONE, "type": "landline", "confidence": 0.7, "dnc": True},
            ], "emails": []},
        ),
        (
            {"lead_id": OWNER_B, "owner_name": "SYNTHETIC OWNER B"},
            {"phones": [
                {"number": UNKNOWN_PHONE, "type": "mobile", "confidence": 0.7, "dnc": False},
                {"number": SYNTH_555, "type": "mobile", "confidence": 0.7, "dnc": False},
            ], "emails": []},
        ),
    ]


def _vendor_index():
    return {
        CLEAN_PHONE: _clean_info(),
        DNC_PHONE: _clean_info(is_clean=False, national_dnc=True),
        UNKNOWN_PHONE: {"is_clean": None, "national_dnc": False,
                        "state_dnc": False, "dma": False, "litigator": False},
        "15175550100": _clean_info(),  # vendor lies: 555 is still not clean
    }


def _mapping():
    return {OWNER_A: CRM_A, OWNER_B: CRM_B}


def _persist(tmp_path, recs=None, vendor=None, mapping=None, queue_id="9001",
             scrubbed_at="2026-08-17T00:00:00Z", **kwargs):
    return persist_dnc_staged_receipt(
        recs if recs is not None else _recs(),
        vendor if vendor is not None else _vendor_index(),
        output_dir=tmp_path,
        dnc_queue_id=queue_id,
        target_key_by_owner=mapping if mapping is not None else _mapping(),
        scrubbed_at=scrubbed_at,
        trace_queue_id="8001",
        vendor_rows=[
            {"phone": CLEAN_PHONE, **_clean_info()},
            {"phone": DNC_PHONE, **_clean_info(is_clean=False, national_dnc=True)},
            {"phone": UNKNOWN_PHONE, "is_clean": None},
            {"phone": "15175550100", **_clean_info()},
        ],
        **kwargs,
    )


# ---- 1. provider dry-run produces no spend ---------------------------------

def test_1_dry_run_produces_no_spend_and_no_receipt_write():
    src = inspect.getsource(tracerfy.run_dry)
    assert "DRY-RUN (no network, no spend)" in src
    assert "submit_trace" not in src
    assert "submit_dnc" not in src
    assert "_request" not in src
    assert "persist_dnc_staged_receipt" not in src
    assert "NOT written on dry-run" in src
    main_src = inspect.getsource(tracerfy.main)
    dry_idx = main_src.index("run_dry")
    key_idx = main_src.index("TRACERFY_API_KEY")
    assert dry_idx < key_idx


# ---- 2. exact cohort restriction --------------------------------------------

def test_2_exact_cohort_restriction_is_lead_id_any():
    src = inspect.getsource(tracerfy.select_targets)
    assert 'only = "and lead_id = any(%s)" if only_leads else ""' in src
    assert "{only}" in tracerfy.TARGET_QUERY
    assert "from landos.owner_leads" in tracerfy.TARGET_QUERY
    help_src = inspect.getsource(tracerfy)
    assert "exact-cohort spend gate" in help_src


def test_2_receipt_is_scoped_to_submitted_phones_only(tmp_path):
    out = _persist(tmp_path)
    phones = {receipt_mod.nanp_key(r["phone"]) for r in out["staged"]}
    assert phones == {
        receipt_mod.nanp_key(CLEAN_PHONE),
        receipt_mod.nanp_key(DNC_PHONE),
        receipt_mod.nanp_key(UNKNOWN_PHONE),
        receipt_mod.nanp_key(SYNTH_555),
    }
    extra = _vendor_index()
    extra["2481119999"] = _clean_info()
    with pytest.raises(DncReceiptError, match="extra="):
        _persist(tmp_path / "extra", vendor=extra)


# ---- 3. deterministic receipt generation ------------------------------------

def test_3_receipt_sha256_is_deterministic(tmp_path):
    a = _persist(tmp_path / "a")
    b = _persist(tmp_path / "b")
    assert a["csv_sha256"] == b["csv_sha256"]
    assert a["csv_sha256"] == hashlib.sha256(a["csv"].read_bytes()).hexdigest()
    assert a["staged"] == b["staged"]


# ---- 4. provider result provenance preserved --------------------------------

def test_4_provenance_names_exact_provider_operation(tmp_path):
    out = _persist(tmp_path)
    body = out["manifest_body"]
    assert body["dnc_queue_id"] == "9001"
    assert body["trace_queue_id"] == "8001"
    assert body["provider_operation"] == "https://tracerfy.com/v1/api/dnc/queue/9001"
    assert body["csv_sha256"] == out["csv_sha256"]
    vendor = json.loads(out["vendor"].read_text())
    assert vendor["dnc_queue_id"] == "9001"
    assert vendor["provider_operation"] == body["provider_operation"]
    assert vendor["rows"][0]["phone"] == CLEAN_PHONE


# ---- 5. DNC blocked remains blocked -----------------------------------------

def test_5_dnc_blocked_remains_blocked(tmp_path):
    out = _persist(tmp_path)
    blocked = [r for r in out["staged"] if receipt_mod.nanp_key(r["phone"]) == DNC_PHONE]
    assert blocked[0]["scrub_status"] == "dnc"
    assert "national_dnc" in blocked[0]["scrub_flags"]
    assert not is_scrub_pass(blocked[0])
    by_target, _stats = load_staged(str(out["csv"]))
    # CRM_A has clean + dnc → verdict clear but DNC digits never enter phones[]
    assert target_verdict(by_target[CRM_A]) == "clear"
    numbers = [p["number"] for p in build_phones(by_target[CRM_A])]
    assert "+15171112222" in numbers
    assert "+15173334444" not in numbers


# ---- 6. DNC unknown remains non-callable ------------------------------------

def test_6_dnc_unknown_remains_non_callable(tmp_path):
    out = _persist(tmp_path)
    unknown = [r for r in out["staged"] if receipt_mod.nanp_key(r["phone"]) == UNKNOWN_PHONE]
    assert unknown[0]["scrub_status"] == "unchecked"
    assert unknown[0]["scrub_flags"] == "vendor_unknown"
    assert not is_scrub_pass(unknown[0])
    by_target, _stats = load_staged(str(out["csv"]))
    assert target_verdict(by_target[CRM_B]) is None  # unchecked + 555, no clean
    assert build_phones(by_target[CRM_B]) == []
    assert classify_vendor_result(None, CLEAN_PHONE) == ("unchecked", "vendor_missing")
    assert classify_vendor_result({}) == ("unchecked", "vendor_unknown")


# ---- 7. DNC clear requires canonical evidence --------------------------------

def test_7_clear_requires_scrub_status_clean(tmp_path):
    out = _persist(tmp_path)
    clean_rows = [r for r in out["staged"] if r["scrub_status"] == "clean"]
    assert all(r["scrub_flags"] == "" for r in clean_rows)
    assert all(is_scrub_pass(r) for r in clean_rows)
    by_target, _stats = load_staged(str(out["csv"]))
    phones = build_phones(by_target[CRM_A])
    assert phones[0]["dnc_status"] == "clear"
    assert phones[0]["dnc_scrubbed_at"] == "2026-08-17T00:00:00Z"
    resolved_src = inspect.getsource(tracerfy.write_resolved)
    assert 'dnc_status, reason = clean, "unchecked", "callable"' in resolved_src
    assert "dnc_status='clear'" not in resolved_src.replace(" ", "").replace('"', "'")


def test_7_write_resolved_clean_stays_unchecked(tmp_path):
    recs = [(
        {"lead_id": OWNER_A, "owner_name": "SYNTHETIC", "provenance": "taxsale"},
        {"phones": [{"number": CLEAN_PHONE, "type": "mobile",
                     "confidence": 0.7, "dnc": False}], "emails": []},
    )]
    path = tmp_path / "resolved.csv"
    tracerfy.write_resolved(str(path), recs, signal_filter=True)
    row = list(csv.DictReader(path.open()))[0]
    assert row["dnc_status"] == "unchecked"
    assert row["phone_1"] == CLEAN_PHONE


# ---- 8. opt-out cannot be weakened ------------------------------------------

def test_8_opt_out_cannot_be_weakened():
    view = Path("migrations/037_callable_leads_dnc_clear_gate.sql").read_text()
    assert "comms.opt_outs" in view
    assert "channel = any (array['call'::text, 'all'::text])" in view
    assert "opt_outs" not in Path("scripts/dnc_staged_receipt.py").read_text()
    sticky = inspect.getsource(sync.main)
    assert "when dnc is true then true" in sticky


# ---- 9. consent cannot be inferred ------------------------------------------

def test_9_consent_cannot_be_inferred():
    assert "consent" not in CSV_FIELDS
    assert "opted_in" not in CSV_FIELDS
    src = Path("scripts/dnc_staged_receipt.py").read_text().lower()
    assert "opted_in" not in src
    assert "consent cannot" in Path("scripts/dnc_staged_receipt.py").read_text().lower() \
        or "infers consent" in src or "infer consent" in src
    intake_src = inspect.getsource(intake.run)
    assert "acq_dnc" in intake_src
    assert "DNC_CONSENT" in intake_src
    view = Path("migrations/037_callable_leads_dnc_clear_gate.sql").read_text()
    assert "consent_status = 'opted_in'" in view
    assert "coalesce((" in view  # missing consent -> false, not inferred


# ---- 10. 555/synthetic phone cannot become callable -------------------------

def test_10_synthetic_555_cannot_become_clean_or_callable(tmp_path):
    assert classify_vendor_result(_clean_info(), SYNTH_555) == ("unchecked", "synthetic_555")
    out = _persist(tmp_path)
    row = [r for r in out["staged"] if receipt_mod.nanp_key(r["phone"])
           == receipt_mod.nanp_key(SYNTH_555)][0]
    assert row["scrub_status"] == "unchecked"
    assert row["scrub_flags"] == "synthetic_555"
    by_target, _stats = load_staged(str(out["csv"]))
    assert build_phones(by_target[CRM_B]) == []
    view = Path("migrations/037_callable_leads_dnc_clear_gate.sql").read_text()
    assert "<> '555'" in view


# ---- 11. intake replay is idempotent ----------------------------------------

def test_11_intake_replay_is_idempotent(tmp_path):
    out = _persist(tmp_path)
    by_target_1, stats_1 = load_staged(str(out["csv"]))
    by_target_2, stats_2 = load_staged(str(out["csv"]))
    assert stats_1 == stats_2
    phones = build_phones(by_target_1[CRM_A])
    merged_1 = merge_clean_phone_evidence(CRM_A, phones, phones, "2026-08-17T00:00:00Z")
    merged_2 = merge_clean_phone_evidence(CRM_A, merged_1, phones, "2026-08-17T00:00:00Z")
    assert [p["number"] for p in merged_1] == [p["number"] for p in merged_2]
    assert all(p["dnc_status"] == "clear" for p in merged_2)


# ---- 12. targeted CRM sync replay is idempotent -----------------------------

def test_12_targeted_crm_sync_replay_is_idempotent():
    src = inspect.getsource(sync.main)
    assert "lead_ids" in src or "lead_filter" in src
    argparse_src = Path("scripts/sync_to_crm.py").read_text()
    assert "--lead-id" in argparse_src
    assert "idempotent" in argparse_src.lower() or "idempotent" in src.lower()
    assert sync.dnc_verdict("unchecked") is None
    assert sync.dnc_verdict("clear") is False
    assert "when dnc is true then true" in src


# ---- 13. no duplicate contacts ----------------------------------------------

def test_13_no_duplicate_contact_rows_in_receipt(tmp_path):
    out = _persist(tmp_path)
    pairs = [(r["target_key"], receipt_mod.nanp_key(r["phone"])) for r in out["staged"]]
    assert len(pairs) == len(set(pairs))
    crm_a = [r for r in out["staged"] if r["target_key"] == CRM_A]
    assert sum(1 for r in crm_a if receipt_mod.nanp_key(r["phone"]) == CLEAN_PHONE) == 1


# ---- 14. no orphan callable records -----------------------------------------

def test_14_receipt_cannot_mint_orphan_callable_rows():
    src = Path("scripts/dnc_staged_receipt.py").read_text()
    assert not re.search(r"\b(insert\s+into|update\s+|delete\s+from)\b", src, re.I)
    assert "from scripts.sync_to_crm" not in src
    orphan_src = inspect.getsource(sync.assert_no_orphan_callable_leads)
    assert "CONTACT LEAK" in orphan_src
    assert "callable lead" in orphan_src.lower()


# ---- 15. no broad DB scan ---------------------------------------------------

def test_15_no_broad_db_scan():
    sql = _flat(CRM_TARGET_KEY_SQL)
    assert "source_lead_id = any(%(owner_lead_ids)s::uuid[])" in sql
    assert "from public.acquisition_leads" in sql
    assert "tenant_id = %(tenant)s" in sql
    assert "select *" not in sql
    only_sql = _flat(tracerfy.TARGET_QUERY)
    assert "lead_id = any(%s)" in _flat(
        inspect.getsource(tracerfy.select_targets))
    assert "from landos.owner_leads" in only_sql


# ---- 16. no second writer ---------------------------------------------------

def test_16_no_second_writer():
    receipt_src = Path("scripts/dnc_staged_receipt.py").read_text()
    assert "from scripts.sync_to_crm" not in receipt_src
    assert "from scripts.import_resolved" not in receipt_src
    assert not re.search(r"\b(update|insert\s+into|delete\s+from)\s+", receipt_src, re.I)
    intake_write = _flat(intake.WRITE_SQL)
    assert "update landos.owner_leads" in intake_write
    assert "public.acquisition_leads" not in intake_write
    skip_src = Path("scripts/skip_trace_tracerfy.py").read_text()
    assert "from scripts.intake_tracerfy_phones" not in skip_src
    assert "intake_tracerfy_phones.run" not in skip_src
    assert "persist_batch_dnc_receipt" in skip_src
    assert "persist_dnc_staged_receipt" in skip_src


# ---- 17. crash/restart reconciliation from staged receipt -------------------

def test_17_crash_restart_reuses_identical_staged_receipt(tmp_path):
    first = _persist(tmp_path)
    second = _persist(tmp_path)
    assert second["reused"] is True
    assert first["csv_sha256"] == second["csv_sha256"]
    dump = persist_vendor_payload(
        tmp_path,
        dnc_queue_id="9001",
        vendor_rows=json.loads(first["vendor"].read_text())["rows"],
        trace_queue_id="8001",
    )
    assert dump["reused"] is True
    # disagreeing restart fails closed rather than clobbering the court record
    with pytest.raises(DncReceiptError, match="disagrees"):
        persist_vendor_payload(
            tmp_path,
            dnc_queue_id="9001",
            vendor_rows=[{"phone": "0000000000"}],
            trace_queue_id="8001",
        )
    src = inspect.getsource(tracerfy.trace_pool)
    assert "resume_dnc_queue_id" in src
    assert "no new /dnc/scrub/" in src
    assert "expected_phones=nums" in src


def test_17_persist_batch_helper_does_not_call_network(tmp_path, monkeypatch):
    calls = []

    def boom(*_a, **_k):
        calls.append(1)
        raise AssertionError("network must not run")

    monkeypatch.setattr(tracerfy, "submit_dnc", boom)
    monkeypatch.setattr(tracerfy, "poll_dnc", boom)
    monkeypatch.setattr(tracerfy, "_request", boom)
    recs = _recs()
    stats = {
        "dnc_queue_id": "9001",
        "dnc_scrubbed": 4,
        "dnc_rows": [
            {"phone": CLEAN_PHONE, **_clean_info()},
            {"phone": DNC_PHONE, **_clean_info(is_clean=False, national_dnc=True)},
            {"phone": UNKNOWN_PHONE, "is_clean": None},
            {"phone": "15175550100", **_clean_info()},
        ],
        "trace_queue_id": "8001",
    }
    out = tracerfy.persist_batch_dnc_receipt(
        recs, stats, out_dir=str(tmp_path), map_keys=lambda _ids: _mapping(),
        scrubbed_at="2026-08-17T00:00:00Z",
        provider_base="https://tracerfy.com/v1/api",
    )
    assert out["csv"].exists()
    assert calls == []


# ---- 18. receipt audited back to exact provider operation -------------------

def test_18_receipt_audits_to_dnc_queue_id(tmp_path):
    out = _persist(tmp_path)
    manifest = json.loads(out["manifest"].read_text())
    vendor = json.loads(out["vendor"].read_text())
    assert manifest["dnc_queue_id"] == vendor["dnc_queue_id"] == "9001"
    assert manifest["provider_operation"].endswith("/dnc/queue/9001")
    assert out["csv"].stat().st_mode & 0o777 == 0o600
    assert out["manifest"].stat().st_mode & 0o777 == 0o600
    assert out["vendor"].stat().st_mode & 0o777 == 0o600


# ---- mapping fail-closed + CRM convention -----------------------------------

def test_crm_mapping_requires_1to1_acquisition_lead_id():
    rows = [
        {"owner_lead_id": OWNER_A, "crm_lead_id": CRM_A},
        {"owner_lead_id": OWNER_B, "crm_lead_id": CRM_B},
    ]
    assert compile_crm_target_key_map(rows, [OWNER_A, OWNER_B]) == _mapping()
    with pytest.raises(DncReceiptError, match="missing"):
        compile_crm_target_key_map(rows[:1], [OWNER_A, OWNER_B])
    with pytest.raises(DncReceiptError, match="multiple CRM"):
        compile_crm_target_key_map(
            rows + [{"owner_lead_id": OWNER_A, "crm_lead_id": CRM_B}],
            [OWNER_A, OWNER_B],
        )
    with pytest.raises(DncReceiptError, match="multiple owner"):
        compile_crm_target_key_map(
            [
                {"owner_lead_id": OWNER_A, "crm_lead_id": CRM_A},
                {"owner_lead_id": OWNER_B, "crm_lead_id": CRM_A},
            ],
            [OWNER_A, OWNER_B],
        )


def test_missing_vendor_membership_writes_nothing(tmp_path):
    vendor = dict(_vendor_index())
    vendor.pop(CLEAN_PHONE)
    with pytest.raises(DncReceiptError, match="missing="):
        _persist(tmp_path, vendor=vendor)
    assert list(tmp_path.iterdir()) == []


def test_contradictory_clean_plus_flag_is_dnc():
    assert classify_vendor_result(
        _clean_info(national_dnc=True), CLEAN_PHONE) == ("dnc", "national_dnc")


def test_intake_load_staged_uses_crm_target_key(tmp_path):
    out = _persist(tmp_path)
    _by_target, stats = load_staged(str(out["csv"]))
    assert stats["key_column"] == "target_key"
    assert set(_by_target) == {CRM_A, CRM_B}


def test_resume_dnc_cli_requires_dnc_flag():
    src = Path("scripts/skip_trace_tracerfy.py").read_text()
    assert "--resume-dnc-queue requires --dnc" in src
    assert "resume_dnc_queue_id" in inspect.getsource(tracerfy.run_batch)

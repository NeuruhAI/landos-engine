"""T5 independent tri-state DNC recourt.

Walks the actual skip-trace last-mile on a clean checkout:

  persist_batch_dnc_receipt → _dnc_index → classify_vendor_result
  → staged receipt → canonical intake → targeted sync → public.callable_leads

Synthetic fixtures only. No Tracerfy network. No production DB.
Unknown must never become DNC.
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

import scripts.dnc_staged_receipt as receipt_mod
import scripts.intake_tracerfy_phones as intake
import scripts.skip_trace_tracerfy as tracerfy
import scripts.sync_to_crm as sync
from scripts.dnc_staged_receipt import DncReceiptError, classify_vendor_result
from scripts.intake_tracerfy_phones import (
    build_phones,
    is_scrub_pass,
    load_staged,
    target_verdict,
)


OWNER = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaa1"
CRM = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbb1"
CLEAN = "5171112222"
BLOCKED = "5173334444"
UNKNOWN = "3134446677"
SYNTH_555 = "5175550100"

VIEW_SQL = Path("migrations/037_callable_leads_dnc_clear_gate.sql").read_text()


def _flat(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower())


def _recs(phone):
    return [(
        {"lead_id": OWNER, "owner_name": "OWNER SYNTHETIC"},
        {"phones": [{"number": phone, "type": "mobile", "confidence": 0.7, "dnc": False}],
         "emails": []},
    )]


def _through_batch(tmp_path, phone, vendor_row):
    """Required hop: persist_batch_dnc_receipt → _dnc_index → classifier."""
    stats = {
        "dnc_queue_id": "9001",
        "dnc_scrubbed": 1,
        "dnc_rows": [vendor_row],
        "trace_queue_id": "8001",
    }
    out = tracerfy.persist_batch_dnc_receipt(
        _recs(phone), stats, out_dir=str(tmp_path),
        map_keys=lambda _ids: {OWNER: CRM},
        scrubbed_at="2026-08-17T15:00:00Z",
        provider_base="https://tracerfy.com/v1/api",
    )
    indexed = tracerfy._dnc_index(stats["dnc_rows"])
    assert out is not None
    row = out["staged"][0]
    nanp = receipt_mod.nanp_key(phone)
    info = indexed.get(nanp)
    if info is None:
        for key, value in indexed.items():
            if receipt_mod.nanp_key(key) == nanp:
                info = value
                break
    classified = classify_vendor_result(info, phone)
    assert (row["scrub_status"], row["scrub_flags"]) == classified
    return out, row


def _pipeline(row, csv_path):
    """Staged receipt → intake shaping → sync tri-state → callable_leads predicates."""
    by_target, _stats = load_staged(str(csv_path))
    staged = by_target[CRM]
    verdict = target_verdict(staged)
    phones = build_phones(staged)
    projected = sync.dnc_verdict(
        "clear" if verdict == "clear" else
        "do_not_call" if verdict == "do_not_call" else "unchecked"
    )
    return {
        "scrub_status": row["scrub_status"],
        "intake_verdict": verdict,
        "intake_phones": phones,
        "sync_dnc": projected,
        "scrub_pass": is_scrub_pass(row),
        "callable_view_would_admit_unknown": projected is False and bool(phones),
    }


def test_t5_persist_batch_uses_dnc_index_then_classifier():
    src = inspect.getsource(tracerfy.persist_batch_dnc_receipt)
    assert "_dnc_index(dnc_rows)" in src
    assert "persist_dnc_staged_receipt" in src
    assert "classify_vendor_result" in inspect.getsource(receipt_mod.build_staged_rows)


def test_t5_is_clean_true_can_reach_clear_evidence_only(tmp_path):
    out, row = _through_batch(tmp_path, CLEAN, {
        "phone": CLEAN, "is_clean": True,
        "national_dnc": False, "state_dnc": False, "dma": False, "litigator": False,
    })
    pipe = _pipeline(row, out["csv"])
    assert pipe["scrub_status"] == "clean"
    assert pipe["intake_verdict"] == "clear"
    assert pipe["intake_phones"][0]["dnc_status"] == "clear"
    assert pipe["sync_dnc"] is False
    assert "fn_dnc_clear_evidence" in VIEW_SQL
    assert "dnc is false" in VIEW_SQL


def test_t5_is_clean_false_is_explicit_dnc_not_unknown(tmp_path):
    out, row = _through_batch(tmp_path, BLOCKED, {
        "phone": BLOCKED, "is_clean": False,
        "national_dnc": False, "state_dnc": False, "dma": False, "litigator": False,
    })
    pipe = _pipeline(row, out["csv"])
    assert pipe["scrub_status"] == "dnc"
    assert pipe["intake_verdict"] == "do_not_call"
    assert pipe["intake_phones"] == []
    assert pipe["sync_dnc"] is True
    assert pipe["scrub_pass"] is False


def test_t5_null_is_clean_stays_unchecked_never_dnc(tmp_path):
    out, row = _through_batch(tmp_path, UNKNOWN, {
        "phone": UNKNOWN, "is_clean": None,
        "national_dnc": False, "state_dnc": False, "dma": False, "litigator": False,
    })
    pipe = _pipeline(row, out["csv"])
    assert pipe["scrub_status"] == "unchecked"
    assert pipe["intake_verdict"] is None
    assert pipe["intake_phones"] == []
    assert pipe["sync_dnc"] is None
    assert pipe["callable_view_would_admit_unknown"] is False
    assert row["scrub_status"] != "dnc"


def test_t5_missing_is_clean_key_stays_unchecked_never_dnc(tmp_path):
    out, row = _through_batch(tmp_path, UNKNOWN, {
        "phone": UNKNOWN,
        "national_dnc": False, "state_dnc": False, "dma": False, "litigator": False,
    })
    pipe = _pipeline(row, out["csv"])
    idx = tracerfy._dnc_index([{"phone": UNKNOWN}])
    assert idx[UNKNOWN]["is_clean"] is None
    assert classify_vendor_result(idx[UNKNOWN], UNKNOWN) == ("unchecked", "vendor_unknown")
    assert pipe["scrub_status"] == "unchecked"
    assert pipe["intake_verdict"] is None
    assert pipe["sync_dnc"] is None
    assert row["scrub_status"] != "dnc"


def test_t5_absent_vendor_record_is_unchecked_not_dnc_and_writes_no_receipt(tmp_path):
    assert classify_vendor_result(None, UNKNOWN) == ("unchecked", "vendor_missing")
    stats = {
        "dnc_queue_id": "9001",
        "dnc_scrubbed": 1,
        "dnc_rows": [{"phone": CLEAN, "is_clean": True,
                      "national_dnc": False, "state_dnc": False,
                      "dma": False, "litigator": False}],
        "trace_queue_id": "8001",
    }
    with pytest.raises(DncReceiptError, match="missing="):
        tracerfy.persist_batch_dnc_receipt(
            _recs(UNKNOWN), stats, out_dir=str(tmp_path),
            map_keys=lambda _ids: {OWNER: CRM},
            scrubbed_at="2026-08-17T15:00:00Z",
            provider_base="https://tracerfy.com/v1/api",
        )
    assert not (tmp_path / "tracerfy_dnc_receipt_9001.csv").exists()


def test_t5_explicit_national_dnc_blocked(tmp_path):
    out, row = _through_batch(tmp_path, BLOCKED, {
        "phone": BLOCKED, "is_clean": False, "national_dnc": True,
        "state_dnc": False, "dma": False, "litigator": False,
    })
    pipe = _pipeline(row, out["csv"])
    assert pipe["scrub_status"] == "dnc"
    assert "national_dnc" in row["scrub_flags"]
    assert pipe["intake_phones"] == []
    assert pipe["sync_dnc"] is True


def test_t5_synthetic_555_never_callable_even_if_vendor_clean(tmp_path):
    out, row = _through_batch(tmp_path, SYNTH_555, {
        "phone": SYNTH_555, "is_clean": True,
        "national_dnc": False, "state_dnc": False, "dma": False, "litigator": False,
    })
    pipe = _pipeline(row, out["csv"])
    assert pipe["scrub_status"] == "unchecked"
    assert row["scrub_flags"] == "synthetic_555"
    assert pipe["intake_verdict"] is None
    assert pipe["intake_phones"] == []
    assert "<> '555'" in VIEW_SQL


def test_t5_opt_out_cannot_be_weakened_by_receipt():
    assert "comms.opt_outs" in VIEW_SQL
    assert "channel = any (array['call'::text, 'all'::text])" in VIEW_SQL
    receipt_src = Path("scripts/dnc_staged_receipt.py").read_text()
    assert "opt_outs" not in receipt_src
    sticky = inspect.getsource(sync.main)
    assert "when dnc is true then true" in sticky


def test_t5_missing_consent_is_not_inferred():
    assert "consent_status = 'opted_in'" in VIEW_SQL
    assert "coalesce((" in VIEW_SQL
    assert ", false)" in _flat(VIEW_SQL)
    intake_src = inspect.getsource(intake.run)
    assert "acq_dnc" in intake_src
    assert "DNC_CONSENT" in intake_src
    assert "opted_in" not in Path("scripts/dnc_staged_receipt.py").read_text()
    assert sync.dnc_verdict("unchecked") is None
    assert sync.dnc_verdict(None) is None


def test_t5_unknown_never_becomes_dnc_through_index_or_classifier():
    for raw in (
        {"phone": UNKNOWN, "is_clean": None},
        {"phone": UNKNOWN},
        {"phone": UNKNOWN, "is_clean": "unknown"},
        {"phone": UNKNOWN, "is_clean": ""},
    ):
        info = tracerfy._dnc_index([raw])[UNKNOWN]
        status, _flags = classify_vendor_result(info, UNKNOWN)
        assert info["is_clean"] is None
        assert status == "unchecked"
        assert status != "dnc"
    assert tracerfy._as_bool(None) is None
    assert classify_vendor_result(None, UNKNOWN)[0] == "unchecked"


def test_t5_crash_resume_is_get_only_reconstructs_receipt(tmp_path, monkeypatch):
    calls = []

    def fake_request(method, url, key, **kwargs):
        calls.append((method, url))
        if method != "GET":
            raise AssertionError(f"resume must not {method} {url}")
        if url.endswith("/queue/8001"):
            return json.dumps({
                "id": "8001",
                "pending": False,
                "results": [{"primary_phone": UNKNOWN}],
            }).encode()
        if url.endswith("/dnc/queue/9001"):
            return json.dumps({
                "id": "9001",
                "pending": False,
                "results": [{
                    "phone": UNKNOWN, "is_clean": None,
                    "national_dnc": False, "state_dnc": False,
                    "dma": False, "litigator": False,
                }],
            }).encode()
        raise AssertionError(f"unexpected url {url}")

    monkeypatch.setattr(tracerfy, "_request", fake_request)
    recs, stats = tracerfy.trace_pool(
        [{"lead_id": OWNER, "owner_name": "OWNER SYNTHETIC"}],
        "test-key", do_dnc=True,
        resume_queue_id="8001", resume_dnc_queue_id="9001",
    )
    assert stats["trace_queue_id"] == "8001"
    assert stats["dnc_queue_id"] == "9001"
    assert all(method == "GET" for method, _url in calls)
    assert any(url.endswith("/queue/8001") for _method, url in calls)
    assert any(url.endswith("/dnc/queue/9001") for _method, url in calls)
    assert not any("/trace/" in url and method == "POST" for method, url in calls)
    assert not any("/dnc/scrub/" in url for _method, url in calls)

    first = tracerfy.persist_batch_dnc_receipt(
        recs, stats, out_dir=str(tmp_path),
        map_keys=lambda _ids: {OWNER: CRM},
        scrubbed_at="2026-08-17T15:00:00Z",
        provider_base="https://tracerfy.com/v1/api",
    )
    assert first["staged"][0]["scrub_status"] == "unchecked"
    second = tracerfy.persist_batch_dnc_receipt(
        recs, stats, out_dir=str(tmp_path),
        map_keys=lambda _ids: {OWNER: CRM},
        scrubbed_at="2026-08-17T15:00:00Z",
        provider_base="https://tracerfy.com/v1/api",
    )
    assert second["reused"] is True
    assert second["csv_sha256"] == first["csv_sha256"]
    by_target, _stats = load_staged(str(second["csv"]))
    assert target_verdict(by_target[CRM]) is None
    assert build_phones(by_target[CRM]) == []


def test_t5_intake_and_sync_replay_do_not_invent_clear():
    assert sync.dnc_verdict("unchecked") is None
    assert sync.dnc_verdict("") is None
    assert intake.target_verdict([{"scrub_status": "unchecked"}]) is None
    assert intake.target_verdict([{"scrub_status": "dnc"}]) == "do_not_call"
    src = inspect.getsource(sync.main)
    assert "--lead-id" in Path("scripts/sync_to_crm.py").read_text()
    assert "lead_filter" in src or "lead_ids" in src

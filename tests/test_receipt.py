"""
test_receipt.py — unit tests for the AUDITABLE-ACQUISITION lead receipt (app/receipt.py).

Pure, no DB / no network — exercises the chain-hash spine with in-memory fixtures.
Locks the two properties the directive demands:
  * chain hash is STABLE across runs for unchanged data (byte-for-byte reproducible)
  * chain hash CHANGES when any section's underlying data changes (tamper-evident),
    and the specific section hash that moved is the one whose data changed.
Plus the PII doctrine: raw payloads / relative names / phone digits never leak into
the receipt body.
"""
import json
from datetime import date

from app.receipt import (
    SECTION_ORDER,
    build_chain,
    section_hash,
    canonical_json,
    sha256_text,
    build_signal_section,
    build_scoring_section,
    build_pierce_section,
    build_compliance_section,
    build_freshness_section,
    render_markdown,
    assemble_receipt,
)


def _sections():
    """A representative five-section fixture (shapes match the DB builders' output)."""
    signals = [{
        "source_type": "taxsale", "source_ref": "WILCO-2026-07-07-T01",
        "source_url": "https://mvbalaw.com/x.pdf", "county_fips": "48491",
        "signal_date": date(2026, 7, 7), "signal_class": "temporal", "status": "matched",
        "ingested_at": "2026-07-04T00:00:00Z",
        "raw_payload": {"owner": "SECRET OWNER LLC", "min_bid": 2500, "city": "Taylor"},
        "owner_name": "SECRET OWNER LLC",
    }]
    lead = {"final_lead_score": 41.0, "emerging_score": 32.5, "absentee_score": None,
            "confidence_score": None, "provenance": "discovered_taxsale", "source": "taxsale",
            "owner_name": "SECRET OWNER LLC", "skip_trace_status": None}
    relations = [{"relative_name": "Jane Q Heir", "relationship": "daughter", "phone": "5125551234",
                  "phone_type": "mobile", "is_heir": True, "confidence": 0.82, "source": "tracerfy",
                  "added_at": "2026-07-01T00:00:00Z"}]
    acq = {"lead_id": "aaaaaaaa-0000-0000-0000-000000000001", "dnc": False, "call_status": "new",
           "status": "discovered", "phone_1": "5125551234", "phone_1_type": "mobile",
           "phone_2": None, "last_call_at": None}
    return {
        "SIGNAL": build_signal_section(signals),
        "SCORING": build_scoring_section(lead, None, None),
        "PIERCE": build_pierce_section(relations),
        "COMPLIANCE": build_compliance_section(acq, lead),
        "FRESHNESS": build_freshness_section(signals, lead, date(2026, 7, 5)),
    }


def test_chain_stable_across_runs():
    # Same data built twice -> identical chain hash and identical section hashes.
    a = build_chain(_sections())
    b = build_chain(_sections())
    assert a["chain_sha256"] == b["chain_sha256"]
    assert a["section_hashes"] == b["section_hashes"]
    # and the chain hash is a real 64-hex sha256
    assert len(a["chain_sha256"]) == 64 and all(c in "0123456789abcdef" for c in a["chain_sha256"])


def test_chain_changes_when_any_section_changes():
    base = build_chain(_sections())
    for name in SECTION_ORDER:
        secs = _sections()
        # perturb exactly one section's body
        secs[name] = dict(secs[name])
        secs[name]["_perturb"] = "x"
        mutated = build_chain(secs)
        assert mutated["chain_sha256"] != base["chain_sha256"], f"{name} change didn't move chain"
        # only that section's hash moved; the others are untouched
        for other in SECTION_ORDER:
            if other == name:
                assert mutated["section_hashes"][other] != base["section_hashes"][other]
            else:
                assert mutated["section_hashes"][other] == base["section_hashes"][other]


def test_freshness_verdict_flips_chain():
    # An upcoming sale (LIVE) vs the same lead read after the sale (STALE) -> different chain.
    signals = _sections_signals()
    lead = {"owner_name": "X"}
    live = build_freshness_section(signals, lead, date(2026, 7, 5))
    stale = build_freshness_section(signals, lead, date(2026, 8, 1))
    assert live["ok"] is True and stale["ok"] is False
    assert section_hash(live) != section_hash(stale)


def _sections_signals():
    return [{"source_type": "taxsale", "source_ref": "WILCO-1", "county_fips": "48491",
             "signal_date": date(2026, 7, 7), "signal_class": "temporal", "status": "matched",
             "ingested_at": "2026-07-04T00:00:00Z", "raw_payload": {}, "owner_name": "X"}]


def test_no_raw_payload_leaks():
    # SIGNAL must expose only a sha256 of raw_payload, never the payload contents.
    secs = _sections()
    blob = canonical_json(secs["SIGNAL"])
    assert "SECRET OWNER LLC" not in blob and "min_bid" not in blob
    assert "raw_payload_sha256" in blob


def test_pierce_withholds_name_and_phone_digits():
    secs = _sections()
    blob = canonical_json(secs["PIERCE"])
    assert "Jane Q Heir" not in blob            # literal relative name withheld
    assert "5125551234" not in blob             # phone digits withheld
    assert sha256_text("Jane Q Heir") in blob   # referenced by fingerprint instead
    # the audit substance IS present
    assert "daughter" in blob and "tracerfy" in blob


def test_compliance_no_phone_digits():
    secs = _sections()
    blob = canonical_json(secs["COMPLIANCE"])
    assert "5125551234" not in blob
    assert secs["COMPLIANCE"]["callable_leads_member"] is True  # dnc=False -> callable
    assert secs["COMPLIANCE"]["phone_1_present"] is True


def test_scoring_marks_missing_composite():
    secs = _sections()
    assert secs["SCORING"]["has_composite_score"] is False
    assert "emergence" in secs["SCORING"]["note"].lower()


def test_markdown_renders_and_hides_pii():
    # render a full receipt shell and confirm the human doc carries the chain but no PII.
    spine = build_chain(_sections())
    receipt = {
        "receipt_version": "1.0", "lead_id": "aaaaaaaa-0000-0000-0000-000000000009",
        "parcel_apn": "R123", "county_fips": "48491", "owner_name": "SECRET OWNER LLC",
        "situs_address": None, "as_of": "2026-07-05",
        "section_hashes": spine["section_hashes"], "chain_sha256": spine["chain_sha256"],
        "sections": spine["sections"],
    }
    md = render_markdown(receipt)
    assert spine["chain_sha256"] in md
    assert "Jane Q Heir" not in md and "5125551234" not in md
    assert "1 · SIGNAL" in md and "5 · FRESHNESS" in md


def _run():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run()

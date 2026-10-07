"""D0 adoption tests — custody gates, truth invariants, aggregation rules."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import adopt_t5m_mecklenburg_resolution as ad


def test_custody_gate_fails_closed(tmp_path):
    f = tmp_path / "x.csv"
    f.write_text("a,b\n1,2\n")
    with pytest.raises(SystemExit, match="CUSTODY GATE FAILED"):
        ad.gate(f, "0" * 64, "test file")


def test_state_parse_is_conservative():
    assert ad.state_from_text("HOLLY SPRINGS NC  27540-9169") == "NC"
    assert ad.state_from_text("3080 HOLIDAY SPRINGS BLVD APT 204 MARGATE FL 33063-5421") == "FL"
    # no confident parse -> unknown stays unknown
    assert ad.state_from_text("PO BOX 12") == ""
    assert ad.state_from_text("") == ""


def test_years_owned_never_fabricated():
    assert ad.years_owned_from_year(2006) == "20"
    assert ad.years_owned_from_year(None) == ""
    assert ad.years_owned_from_year(1700) == ""      # implausible year -> unknown


def test_six_county_rows_aggregate_bills_and_keep_unknowns_empty(tmp_path):
    sig = {
        "signal_id": "s1", "source_type": "taxdelinquent", "geography": "37183",
        "parcel_apn": "0648990797", "owner_name": "ADAMS, QUENTIN",
        "raw_address": "605 UTLEY ST HOLLY SPRINGS 27540",
        "detail_json": {"tax_balance": 100.5, "tax_year": 2024,
                        "total_real_value": 150000,
                        "mailing_address": "605 UTLEY ST, HOLLY SPRINGS NC 27540",
                        "source_label": "delq853", "source_url": "https://x",
                        "source_as_of": "2026-08-12"},
    }
    sig2 = dict(sig, signal_id="s2",
                detail_json=dict(sig["detail_json"], tax_balance=49.5, tax_year=2025))
    p = tmp_path / "sig.jsonl"
    p.write_text("\n".join(json.dumps(s) for s in (sig, sig2)) + "\n")
    rows, stats = ad.build_six_county_rows(p)
    assert stats == {"signals": 2, "distinct_properties": 1}
    r = rows[0]
    assert r["tax_balance"] == 150.0            # sum of real bills
    assert r["tax_years"] == "2024|2025"
    assert r["signal_count"] == "2"
    assert r["county"] == "Wake"
    assert r["out_of_state"] == "False"          # NC parsed confidently
    assert r["mortgage_balance"] == ""           # unknown stays empty
    assert r["heir_indicator"] == ""             # classification left to rank v2 layer
    assert r["tax_foreclosure_indicator"] == ""  # no foreclosure signal -> unknown
    assert r["contact_authorized"] == "False"


def test_foreclosure_signal_sets_indicator(tmp_path):
    sig = {"signal_id": "s1", "source_type": "tax_foreclosure_case", "geography": "37025",
           "parcel_apn": "04-082-0017.20", "owner_name": "ROBESON MARTHA TRUST",
           "raw_address": "61 UNION ST S", "detail_json": {
               "tax_value": 343650, "court_case_numbers": ["26CV006109-120"],
               "source_label": "feed", "source_url": "https://c", "source_as_of": "2026-08-12"}}
    p = tmp_path / "sig.jsonl"
    p.write_text(json.dumps(sig) + "\n")
    rows, _ = ad.build_six_county_rows(p)
    assert rows[0]["tax_foreclosure_indicator"] == "True"
    assert rows[0]["court_case_numbers"] == "26CV006109-120"
    assert rows[0]["assessed_value"] == 343650
    assert rows[0]["tax_balance"] == ""          # feed publishes no balance -> unknown


def test_parcel_normalization():
    assert ad.norm_parcel("04-082 -0017.20") == "04082001720"
    assert ad.norm_parcel(" 06508110 ") == "06508110"

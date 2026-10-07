"""T4E export factory tests — truth invariants, determinism, contract shape."""
from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from scripts import export_opportunities as xf
from scripts import validate_deedsonar_import as court


def _src_row(rank, fips, apn, recipe, score, tier, **over):
    base = {
        "rank": str(rank), "county": "Union" if fips == "37179" else "Mecklenburg",
        "county_fips": fips, "parcel_apn": apn, "owner_name": "DOE JANE HEIRS",
        "owner_type": "individual", "property_address": f"{rank} MAIN ST",
        "property_city": "MONROE", "property_zip": "28110",
        "owner_mailing_address": "1 ELM ST", "owner_mailing_city": "MONROE",
        "owner_mailing_state": "NC", "owner_mailing_zip": "28110",
        "out_of_state": "False", "assessed_value": "150000", "market_value": "",
        "tax_balance": "1234.5", "tax_years": "2023|2024", "years_owned": "30",
        "source_types": "taxdelinquent", "signal_count": "2",
        "court_case_numbers": "", "heir_indicator": "True",
        "fragmented_ownership_indicator": "True", "tax_foreclosure_indicator": "False",
        "parcel_match_status": "ONEMAP_MATCHED", "source_url": "https://x",
        "source_urls": "https://x|https://y", "mortgage_balance": "",
        "estimated_equity": "", "equity_status": "UNKNOWN_NEEDS_DEBT_SEARCH",
        "curative_priority_score": str(score), "priority_tier": tier,
        "recipe_id": recipe, "trigger_classes": "tax_distress|heir_or_estate",
        "score_reasons": "published_tax_distress:+10|heir_or_estate_signal:+20",
        "phone": "", "phone_status": "NOT_ENRICHED",
        "source_families": "county_tax_collector_publication",
        "independent_source_family_count": "1",
        "scoring_version": "nc_curative_rank.v3", "recipe_version": "nc_curative_recipes.v1",
    }
    base.update(over)
    return base


def _write_source(path: Path, rows):
    cols = list(rows[0].keys())
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


@pytest.fixture()
def factory(tmp_path):
    rows = [
        _src_row(1, "37179", "A1", "heir_property_acquisition_research", 90, "A"),
        _src_row(2, "37179", "A2", "heir_property_acquisition_research", 80, "B"),
        # deliberate score tie -> global rank breaks it deterministically
        _src_row(3, "37119", "B1", "published_tax_distress", 55, "C"),
        _src_row(4, "37119", "B2", "published_tax_distress", 55, "C"),
        _src_row(5, "37119", "B3", "published_tax_distress", 40, "D"),
    ]
    src = tmp_path / "src.csv"
    _write_source(src, rows)
    out = tmp_path / "out"
    manifest = xf.build(src, out, state=None, county=None, recipe=None, scope="all",
                        pending_signals=None, pending_audit=None, meck_staging=None,
                        exported_at="2026-08-12T00:00:00+00:00", export_run_id="exprun_test")
    return src, out, manifest


class TruthInvariantTests:
    pass


def test_unknown_is_never_zero_or_false():
    assert xf.norm_num("") == ""
    assert xf.norm_num("garbage") == ""
    assert xf.norm_num("$1,250.50") == "1250.5"
    assert xf.norm_bool("") == ""
    assert xf.norm_bool("True") == "true"
    assert xf.norm_bool("mystery") == ""


def test_map_row_leaves_unknown_economics_empty():
    r = xf.map_row(_src_row(1, "37179", "A1", "published_tax_distress", 50, "C"),
                   provenance={"source_run_id": "s", "scoring_run_id": "NOT_DEFINED",
                               "export_run_id": "e", "schema_version": xf.SCHEMA_VERSION,
                               "created_at": "2026-08-12", "exported_at": "t"})
    assert r["mortgage_balance"] == ""
    assert r["estimated_equity"] == ""
    assert r["property_value"] == ""      # market_value blank -> blank, never 0
    assert r["absentee_owner_flag"] == "" # not canonically computed -> UNKNOWN
    assert r["vacancy_flag"] == r["lien_flag"] == r["code_violation_flag"] == ""
    assert r["global_rank"] == "1" and r["global_score"] == "50"
    assert r["property_key"] == "NC:37179:A1"
    assert r["recipe_name"] == r["recipe_id"]   # no invented display names


def test_score_reason_split_orders_by_points():
    primary, secondary = xf.parse_score_reasons(
        "published_tax_distress:+10|active_tax_foreclosure:+25|multi_trigger_3_plus:+10")
    assert primary == "active_tax_foreclosure"
    assert secondary == "published_tax_distress|multi_trigger_3_plus"


def test_per_recipe_files_rank_contiguous_and_sorted(factory):
    _, out, _ = factory
    p = out / "03_FULL_RECIPE_EXPORTS" / "published_tax_distress_FULL.csv"
    rows = list(csv.DictReader(open(p)))
    assert [r["recipe_rank"] for r in rows] == ["1", "2", "3"]
    scores = [float(r["recipe_score"]) for r in rows]
    assert scores == sorted(scores, reverse=True)
    # tie at 55 broken by canonical global rank (3 before 4)
    assert [r["global_rank"] for r in rows[:2]] == ["3", "4"]


def test_no_top_slice_when_full_is_smaller(factory):
    _, out, manifest = factory
    assert not (out / "04_TOP100").exists() or not list((out / "04_TOP100").glob("*.csv"))
    names = [e["filename"] for e in manifest["files"]]
    assert "published_tax_distress_FULL.csv" in names
    assert not any("TOP" in n for n in names)


def test_master_in_canonical_global_order(factory):
    _, out, _ = factory
    rows = list(csv.DictReader(open(out / "08_MASTER" / "ALL_OPPORTUNITIES_MASTER.csv")))
    assert [r["global_rank"] for r in rows] == ["1", "2", "3", "4", "5"]
    assert len({r["property_key"] for r in rows}) == 5


def test_duplicate_property_keys_refuse_to_export(tmp_path):
    rows = [_src_row(1, "37179", "SAME", "published_tax_distress", 50, "C"),
            _src_row(2, "37179", "SAME", "published_tax_distress", 40, "D")]
    src = tmp_path / "dupe.csv"
    _write_source(src, rows)
    with pytest.raises(SystemExit):
        xf.build(src, tmp_path / "o", state=None, county=None, recipe=None, scope="all",
                 pending_signals=None, pending_audit=None, meck_staging=None,
                 exported_at="t", export_run_id="e")


def test_manifest_hashes_match_files(factory):
    _, out, manifest = factory
    import hashlib
    for e in manifest["files"]:
        for d in ("03_FULL_RECIPE_EXPORTS", "08_MASTER"):
            fp = out / d / e["filename"]
            if fp.exists():
                assert hashlib.sha256(fp.read_bytes()).hexdigest() == e["sha256"]
                break
        else:
            pytest.fail(f"manifest file not found: {e['filename']}")


def test_deedsonar_import_ready_passes_the_import_court(factory):
    _, out, _ = factory
    report = court.validate(out / "08_MASTER" / "DEEDSONAR_IMPORT_READY_MASTER.csv")
    assert report["overall"] == "PASS", report["checks"]
    rows = list(csv.DictReader(open(out / "08_MASTER" / "DEEDSONAR_IMPORT_READY_MASTER.csv")))
    # canonical headers + stable explicit identity + required fields
    assert all(r["source_row_id"] == r["property_key"] for r in rows)
    assert all(r["state"] == "NC" for r in rows)
    assert all(r["apn"] for r in rows)
    assert "dnc" not in rows[0]


def test_deterministic_rerun_same_bytes(tmp_path, factory):
    src, out, _ = factory
    out2 = tmp_path / "out2"
    xf.build(src, out2, state=None, county=None, recipe=None, scope="all",
             pending_signals=None, pending_audit=None, meck_staging=None,
             exported_at="2026-08-12T00:00:00+00:00", export_run_id="exprun_test")
    a = (out / "08_MASTER" / "ALL_OPPORTUNITIES_MASTER.csv").read_bytes()
    b = (out2 / "08_MASTER" / "ALL_OPPORTUNITIES_MASTER.csv").read_bytes()
    assert a == b

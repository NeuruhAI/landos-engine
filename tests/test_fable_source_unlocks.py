"""Fable cheapest unlocks: VALUATION derive + PARCEL estate-query projection.

No scraper. No Supabase. No DeedSonar. No DEED_TENURE. No Kofile/Fidlar/GovOS.
"""
from __future__ import annotations

from pathlib import Path

from app.projection.classes import (
    GAP_NORMALIZED_FACT_MISSING,
    GAP_PROJECTION_MISSING,
    NOT_RAW_SOURCE_MISSING,
    UNLOCK_DERIVED_FROM_EXISTING,
    UNLOCK_PROJECTION_OF_INGESTED,
    unlock_classes,
)
from app.projection.equity import FORBIDDEN_BASIS_KEYS, derive_equity_estimated_pct
from app.projection.estate_query import (
    index_ingested_parcels,
    load_ingested_parcels,
    project_estate_query,
    project_onto_estate_row,
)
from app.projection.parcel import project_parcel_facts
from app.free_routes.estate_rows import load_estate_rows


def test_unlock_classes_are_derive_and_project_never_raw_source():
    gap, mode = unlock_classes("VALUATION")
    assert gap == GAP_NORMALIZED_FACT_MISSING
    assert mode == UNLOCK_DERIVED_FROM_EXISTING
    gap, mode = unlock_classes("PARCEL")
    assert gap == GAP_PROJECTION_MISSING
    assert mode == UNLOCK_PROJECTION_OF_INGESTED


def test_unhandled_family_fails_closed():
    try:
        unlock_classes("DEED_TENURE")
    except AssertionError as exc:
        assert "unhandled unlock family" in str(exc)
    else:
        raise AssertionError("DEED_TENURE must stay out of this lane")


def test_equity_derived_from_assessed_and_tax_balance():
    fact = derive_equity_estimated_pct(
        {"assessed_value": "150000", "tax_balance": "1234.5"}
    )
    assert fact["state"] == "KNOWN"
    assert fact["value"] == 99.2
    assert fact["gap_class"] == GAP_NORMALIZED_FACT_MISSING
    assert fact["unlock_mode"] == UNLOCK_DERIVED_FROM_EXISTING
    assert fact["not_class"] == NOT_RAW_SOURCE_MISSING
    assert fact["proxy"] is True
    assert fact["basis"]["assessed_value"] == 150000.0
    assert fact["debt_proxy_kind"] == "tax_balance"


def test_equity_accepts_amount_owed_alias():
    fact = derive_equity_estimated_pct(assessed_value=200_000, amount_owed=50_000)
    assert fact["value"] == 75.0
    assert fact["debt_proxy_kind"] == "amount_owed"


def test_equity_known_zero_tax_is_100_percent_proxy_not_unknown():
    fact = derive_equity_estimated_pct(assessed_value=80_000, tax_balance=0)
    assert fact["state"] == "KNOWN"
    assert fact["value"] == 100.0


def test_equity_negative_when_tax_exceeds_assessed():
    fact = derive_equity_estimated_pct(assessed_value=10_000, tax_balance=12_000)
    assert fact["value"] == -20.0


def test_missing_debt_is_pending_never_fabricated_100():
    fact = derive_equity_estimated_pct({"assessed_value": 150000})
    assert fact["state"] == "UNKNOWN"
    assert fact["value"] is None
    assert fact["reason"] == "DEBT_PROXY_PENDING"
    assert fact["gap_class"] == GAP_NORMALIZED_FACT_MISSING
    assert fact["not_class"] == NOT_RAW_SOURCE_MISSING
    assert "100" not in (fact.get("why") or "")


def test_missing_assessed_is_not_raw_source_missing():
    fact = derive_equity_estimated_pct({"tax_balance": 500})
    assert fact["state"] == "UNKNOWN"
    assert fact["reason"] == "ASSESSED_VALUE_ABSENT"
    assert fact["not_class"] == NOT_RAW_SOURCE_MISSING
    assert fact["gap_class"] == GAP_NORMALIZED_FACT_MISSING


def test_non_positive_assessed_stays_unknown():
    fact = derive_equity_estimated_pct(assessed_value=0, tax_balance=10)
    assert fact["state"] == "UNKNOWN"
    assert fact["reason"] == "ASSESSED_VALUE_NOT_POSITIVE"


def test_equity_does_not_read_deed_tenure_or_mortgage_as_basis():
    fact = derive_equity_estimated_pct({
        "assessed_value": 100000,
        "tax_balance": 10000,
        "years_owned": 40,
        "last_sale_date": "1986-01-01",
        "mortgage_balance": 90000,
        "estimated_equity": "",
    })
    assert fact["value"] == 90.0
    for key in FORBIDDEN_BASIS_KEYS:
        assert key not in fact["basis"]


def test_parcel_projects_ingested_acreage_and_land_use():
    parcel = project_parcel_facts(
        {"apn": "0648080648", "owner_name": "DOE"},
        {"acreage": "12.4", "land_use_class": "VACANT AGRICULTURAL",
         "land_value": 40000, "improvement_value": 2500},
    )
    assert parcel["unlock_mode"] == UNLOCK_PROJECTION_OF_INGESTED
    assert parcel["gap_class"] == GAP_PROJECTION_MISSING
    assert parcel["deed_tenure_projected"] is False
    facts = parcel["facts"]
    assert facts["acreage.acres"]["state"] == "KNOWN"
    assert facts["acreage.acres"]["value"] == 12.4
    assert facts["land.use"]["value"] == "VACANT AGRICULTURAL"
    assert facts["parcel.land_value"]["value"] == 40000.0
    assert facts["parcel.improvement_value"]["value"] == 2500.0


def test_parcel_falls_back_to_acreage_calc_when_acreage_is_zero():
    parcel = project_parcel_facts(
        {},
        {"acreage": "0.0000", "acreage_calc": "0.15", "land_use": "Residential"},
    )
    assert parcel["facts"]["acreage.acres"]["value"] == 0.15
    assert parcel["facts"]["acreage.acres"]["source_field"] == "acreage_calc"


def test_parcel_unknown_when_not_ingested_is_projection_missing():
    parcel = project_parcel_facts({"apn": "X", "owner_name": "DOE"})
    assert parcel["facts"]["acreage.acres"]["state"] == "UNKNOWN"
    assert parcel["facts"]["land.use"]["state"] == "UNKNOWN"
    assert parcel["facts"]["acreage.acres"]["gap_class"] == GAP_PROJECTION_MISSING
    assert parcel["facts"]["acreage.acres"]["not_class"] == NOT_RAW_SOURCE_MISSING


def test_parcel_refuses_to_project_deed_tenure_from_overlay():
    parcel = project_parcel_facts(
        {"apn": "A1"},
        {"acreage": 3.2, "years_owned": 28, "last_sale_date": "1998-04-01",
         "deed_date": "1998-04-01"},
    )
    assert parcel["deed_tenure_projected"] is False
    assert parcel["merged_aliases_ignored"] == ["deed_date", "last_sale_date", "years_owned"]
    dumped = str(parcel["facts"])
    assert "1998" not in dumped
    assert "28" not in dumped


def test_estate_query_attaches_both_unlocks():
    ingested = index_ingested_parcels([{
        "parcel_apn": "0648-08-0648.990",
        "acreage": 8.0,
        "land_use_class": "401",
        "land_value": 22000,
        "improvement_value": 0,
    }])
    row = project_onto_estate_row(
        {"apn": "0648080648", "assessed_value": 90000, "tax_balance": 4500,
         "owner_name": "SMITH HEIRS"},
        ingested,
    )
    projected = row["projected"]
    assert projected["valuation"]["value"] == 95.0
    assert projected["parcel"]["facts"]["acreage.acres"]["value"] == 8.0
    assert row["equity_estimated_pct"] == 95.0
    assert row["acreage"] == 8.0
    assert row["land_use"] == "401"
    assert row["land_use_class"] == "401"
    assert projected["scraper"] is False
    assert projected["supabase_mutated"] is False
    assert projected["deed_tenure_projected"] is False


def test_estate_query_does_not_clobber_existing_acreage_column():
    row = project_onto_estate_row(
        {"apn": "A", "acreage": 1.1, "assessed_value": 10, "tax_balance": 1},
        {"A": {"acreage": 99.0}},
    )
    assert row["acreage"] == 1.1
    # overlay still wins inside the projected fact
    assert row["projected"]["parcel"]["facts"]["acreage.acres"]["value"] == 99.0


def test_load_estate_rows_projects_assessed_and_ingested_parcel(tmp_path):
    master = tmp_path / "Wake" / "master"
    master.mkdir(parents=True)
    (master / "wake_master_0001.csv").write_text(
        "candidate_id,owner_name,apn,assessed_value,tax_balance,global_rank\n"
        "c1,SMITH JOHN,0648080648,150000,1500,1\n"
        "c2,ACME LLC,0648080649,80000,,2\n",
        encoding="utf-8",
    )
    (tmp_path / "bundle").mkdir()
    (tmp_path / "bundle" / "CANDIDATE_CONTACT_RESOLUTION.csv").write_text(
        "candidate_id,owner_class,contact_resolution_state\n"
        "c1,DECEASED,DECEASED_OWNER_NEEDS_HEIR_IDENTITY\n"
        "c2,LLC,ENTITY_NEEDS_LAWFUL_SOS_IMPORT\n",
        encoding="utf-8",
    )
    ingested = index_ingested_parcels([
        {"apn": "0648080648", "acreage": 2.5, "land_use": "RESIDENTIAL"},
    ])
    rows = load_estate_rows(tmp_path, ["Wake"], tmp_path / "bundle", ingested)
    assert rows[0]["res_owner_class"] == "DECEASED"
    assert rows[0]["equity_estimated_pct"] == 99.0
    assert rows[0]["acreage"] == 2.5
    assert rows[0]["land_use"] == "RESIDENTIAL"
    assert rows[0]["projected"]["valuation"]["not_class"] == NOT_RAW_SOURCE_MISSING
    assert rows[1]["projected"]["valuation"]["reason"] == "DEBT_PROXY_PENDING"
    assert "acreage" not in rows[1] or rows[1].get("acreage") in ("", None)


def test_load_ingested_parcels_from_derive_shaped_csv(tmp_path):
    path = tmp_path / "parcels.csv"
    path.write_text(
        "parcel_id,acreage,land_use_class,land_value,improvement_value\n"
        "ABC123,4.75,AG,18000,0\n",
        encoding="utf-8",
    )
    index = load_ingested_parcels(path)
    rows = project_estate_query(
        [{"apn": "ABC123", "assessed_value": 18000, "tax_balance": 180}],
        index,
    )
    assert rows[0]["acreage"] == 4.75
    assert rows[0]["land_use_class"] == "AG"
    assert rows[0]["equity_estimated_pct"] == 99.0


def test_projection_modules_do_not_import_forbidden_lanes():
    root = Path(__file__).resolve().parents[1] / "app" / "projection"
    blob = "".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    lowered = blob.lower()
    for needle in (
        "import supabase",
        "from supabase",
        "kofile",
        "fidlar",
        "govos",
        "import deedsonar",
        "from deedsonar",
        "requests.get",
        "playwright",
        "selenium",
    ):
        assert needle not in lowered
    assert "years_owned" in blob  # named only as a refuse list

"""Fable TAX_SALE_CALENDAR unlock: persist TaxSaleInfoNormalizer as clock rows.

Gap: NORMALIZED_FACT_MISSING. Mode: PERSIST_FROM_EXISTING_NORMALIZER.
No scraper. No Supabase. No DeedSonar. No LIVE product path.
Missing sale dates / redemption deadlines stay UNKNOWN — never fabricated.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from app.normalizers.tax_sale_info import TaxSaleInfoNormalizer
from app.projection.classes import (
    FAMILY_TAX_SALE_CALENDAR,
    GAP_NORMALIZED_FACT_MISSING,
    INGREDIENT_TAX_JURISDICTION,
    INGREDIENT_TAX_MIN_BID,
    INGREDIENT_TAX_REDEMPTION_DEADLINE,
    INGREDIENT_TAX_SALE_DATE,
    NOT_RAW_SOURCE_MISSING,
    UNLOCK_PERSIST_FROM_EXISTING_NORMALIZER,
    unlock_classes,
)
from app.projection.estate_query import (
    index_ingested_parcels,
    project_onto_estate_row,
)
from app.projection.tax_sale_calendar import (
    field_census,
    persist_from_existing_normalizer,
    persist_process_clock_rows,
    project_tax_sale_calendar_facts,
    recipes_touched,
)
from app.projection.tax_years import (
    CLOCK_TAX_DELINQUENCY,
    CLOCK_TAX_SALE_CALENDAR,
    PROCESS_CLOCK_REGISTRY,
    RECIPE_PUBLISHED_TAX_DISTRESS,
    RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP,
)

# Same fixture as tests/test_signals.py — already-parsed tax-sale.info CSV.
SAMPLE_CSV = (
    "Lot Number,Minimum Bid,Current Taxes,Parcel Id,County,Address,Local Unit,SEV,Legal Description,Latitude,Longitude\n"
    '1,"1,500.00","3,200.00",11-54-0340-0151-02-5,berrien,133 E MAIN ST,Benton Harbor,40000,LOT 1,N42.1159,W86.4573\n'
    "2,8000,900,11-03-5800-0001-00-4,berrien,1850 PIPESTONE RD,Benton Twp,30000,LOT 2,42.0848,W86.4201\n"
    ",,,,,,,,,,\n"
)


def test_gap_mode_is_normalized_fact_missing_persist_from_existing_normalizer():
    gap, mode = unlock_classes(FAMILY_TAX_SALE_CALENDAR)
    assert gap == GAP_NORMALIZED_FACT_MISSING
    assert mode == UNLOCK_PERSIST_FROM_EXISTING_NORMALIZER
    assert gap == "NORMALIZED_FACT_MISSING"
    assert mode == "PERSIST_FROM_EXISTING_NORMALIZER"


def test_census_names_existing_normalizer_fields_only():
    census = field_census()
    fields = {row["field"] for row in census}
    assert fields == {
        "raw_meta.minimum_bid",
        "raw_meta.county",
        "raw_meta.catalog_id",
        "sale_date / event_date",
        "redemption_deadline",
    }
    blob = " ".join(row["writer"].lower() for row in census)
    assert "invent" not in blob
    assert "scrape" not in blob


def test_existing_normalizer_still_parses_min_bid_and_skips_blank():
    cands = TaxSaleInfoNormalizer("t", 2049, "test", 2022).normalize(SAMPLE_CSV)
    assert len(cands) == 2
    assert cands[0]["raw_meta"]["minimum_bid"] == 1500.0
    assert cands[0]["raw_meta"]["county"] == "berrien"
    assert cands[0]["raw_meta"]["catalog_id"] == 2049
    assert "sale_date" not in cands[0]
    assert "redemption_deadline" not in cands[0]["raw_meta"]
    assert cands[0]["signal_date"] == date(2022, 1, 1)


def test_persist_from_existing_normalizer_writes_in_process_clock_rows():
    rows = persist_from_existing_normalizer(
        SAMPLE_CSV,
        catalog_id=2049,
        label="test",
        signal_year=2022,
        county="Berrien",
    )
    assert len(rows) == 2
    row = rows[0]
    assert row["clock_id"] == CLOCK_TAX_SALE_CALENDAR
    assert row["apn"] == "11-54-0340-0151-02-5"
    assert row["min_bid"] == 1500.0
    assert row["jurisdiction"] == "MI"
    assert row["county"] == "berrien"
    assert row["catalog_id"] == 2049
    assert row["sale_date"] is None
    assert row["redemption_deadline"] is None
    assert row["live"] is False
    assert row["persisted"] == "IN_PROCESS"
    assert row["storage"] == "IN_PROCESS_PROJECTION"
    assert row["supabase_mutated"] is False
    assert row["invented_sale_dates"] == []
    assert row["invented_redemption_deadlines"] == []
    facts = row["facts"]
    assert facts[INGREDIENT_TAX_MIN_BID]["state"] == "KNOWN"
    assert facts[INGREDIENT_TAX_MIN_BID]["source_field"] == "minimum_bid"
    assert facts[INGREDIENT_TAX_JURISDICTION]["state"] == "KNOWN"
    assert facts[INGREDIENT_TAX_JURISDICTION]["value"] == {
        "state": "MI",
        "county": "berrien",
    }
    assert facts[INGREDIENT_TAX_SALE_DATE]["state"] == "UNKNOWN"
    assert facts[INGREDIENT_TAX_SALE_DATE]["reason"] == (
        "SALE_DATE_NOT_ON_NORMALIZER_OUTPUT"
    )
    assert facts[INGREDIENT_TAX_REDEMPTION_DEADLINE]["state"] == "UNKNOWN"
    assert facts[INGREDIENT_TAX_REDEMPTION_DEADLINE]["reason"] == (
        "REDEMPTION_DEADLINE_NOT_ON_NORMALIZER_OUTPUT"
    )


def test_signal_date_jan_1_is_not_fabricated_as_sale_date():
    cand = TaxSaleInfoNormalizer("t", 2049, "test", 2022).normalize(SAMPLE_CSV)[0]
    assert cand["signal_date"] == date(2022, 1, 1)
    fact = project_tax_sale_calendar_facts(cand)
    assert fact["facts"][INGREDIENT_TAX_SALE_DATE]["state"] == "UNKNOWN"
    assert fact["facts"][INGREDIENT_TAX_SALE_DATE]["value"] is None
    assert fact["invented_sale_dates"] == []


def test_persists_sale_date_and_redemption_when_already_on_artifact():
    cand = TaxSaleInfoNormalizer("t", 2906, "DNR", 2026).normalize(SAMPLE_CSV)[0]
    cand["raw_meta"]["sale_date"] = "2026-08-06"
    cand["raw_meta"]["redemption_deadline"] = "2026-03-31"
    rows = persist_process_clock_rows([cand])
    assert rows[0]["sale_date"] == "2026-08-06"
    assert rows[0]["redemption_deadline"] == "2026-03-31"
    facts = rows[0]["facts"]
    assert facts[INGREDIENT_TAX_SALE_DATE]["state"] == "KNOWN"
    assert facts[INGREDIENT_TAX_SALE_DATE]["source_field"] == "sale_date"
    assert facts[INGREDIENT_TAX_REDEMPTION_DEADLINE]["value"] == "2026-03-31"


def test_assessor_sale_date_on_estate_row_is_not_a_tax_sale():
    fact = project_tax_sale_calendar_facts({
        "apn": "0648080648",
        "sale_date": "1998-04-01",
        "last_sale_date": "1998-04-01",
        "state": "NC",
        "county": "Wake",
    })
    assert fact["facts"][INGREDIENT_TAX_SALE_DATE]["state"] == "UNKNOWN"
    assert fact["facts"][INGREDIENT_TAX_REDEMPTION_DEADLINE]["state"] == "UNKNOWN"
    assert fact["facts"][INGREDIENT_TAX_JURISDICTION]["state"] == "UNKNOWN"
    assert fact["invented_sale_dates"] == []


def test_missing_min_bid_stays_unknown_never_fabricated_floor():
    fact = project_tax_sale_calendar_facts({"apn": "X", "signal_type": "tax_delinquent"})
    assert fact["facts"][INGREDIENT_TAX_MIN_BID]["state"] == "UNKNOWN"
    assert fact["facts"][INGREDIENT_TAX_MIN_BID]["value"] is None
    assert fact["facts"][INGREDIENT_TAX_MIN_BID]["reason"] == (
        "MIN_BID_NOT_ON_NORMALIZER_OUTPUT"
    )


def test_process_clock_registry_extends_canonical_home_not_a_parallel_bus():
    assert CLOCK_TAX_DELINQUENCY in PROCESS_CLOCK_REGISTRY
    clock = PROCESS_CLOCK_REGISTRY[CLOCK_TAX_SALE_CALENDAR]
    assert clock["clock_id"] == CLOCK_TAX_SALE_CALENDAR
    assert clock["live"] is False
    assert clock["persist_mode"] == "IN_PROCESS_FROM_EXISTING_NORMALIZER"
    assert clock["storage"] == "IN_PROCESS_PROJECTION"
    assert clock["related_clock"] == CLOCK_TAX_DELINQUENCY
    assert RECIPE_PUBLISHED_TAX_DISTRESS in clock["recipes"]
    assert RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP in clock["recipes"]
    assert "tax_sale_info" in clock["source"]


def test_estate_query_exposes_clock_row_from_normalizer_overlay():
    cand = TaxSaleInfoNormalizer("t", 2049, "test", 2022).normalize(SAMPLE_CSV)[0]
    ingested = index_ingested_parcels([cand])
    hit = project_onto_estate_row(
        {"apn": "11-54-0340-0151-02-5", "owner_name": "DOE"},
        ingested,
    )
    miss = project_onto_estate_row(
        {"apn": "NO-MATCH", "owner_name": "NONE", "state": "NC"},
        ingested,
    )
    assert hit["min_bid"] == 1500.0
    assert hit["tax_jurisdiction"]["state"] == "MI"
    assert "tax_sale_date" not in hit
    assert "tax_redemption_deadline" not in hit
    cal = hit["projected"]["tax_sale_calendar"]
    assert cal["family"] == FAMILY_TAX_SALE_CALENDAR
    assert cal["gap_class"] == GAP_NORMALIZED_FACT_MISSING
    assert cal["unlock_mode"] == UNLOCK_PERSIST_FROM_EXISTING_NORMALIZER
    assert cal["not_class"] == NOT_RAW_SOURCE_MISSING
    assert cal["live"] is False
    assert cal["facts"][INGREDIENT_TAX_MIN_BID]["value"] == 1500.0
    assert cal["facts"][INGREDIENT_TAX_SALE_DATE]["state"] == "UNKNOWN"
    clocks = hit["projected"]["process_clock_rows"]
    assert len(clocks) == 1
    assert clocks[0]["clock_id"] == CLOCK_TAX_SALE_CALENDAR
    assert clocks[0]["min_bid"] == 1500.0
    assert FAMILY_TAX_SALE_CALENDAR in hit["projected"]["families"]
    assert hit["projected"]["invented_sale_dates"] == []
    assert hit["projected"]["supabase_mutated"] is False
    assert hit["projected"]["scraper"] is False
    assert "min_bid" not in miss
    assert miss["projected"]["process_clock_rows"] == []
    assert miss["projected"]["tax_sale_calendar"]["facts"][INGREDIENT_TAX_MIN_BID]["state"] == (
        "UNKNOWN"
    )


def test_estate_query_does_not_clobber_existing_min_bid():
    row = project_onto_estate_row(
        {
            "apn": "11-54-0340-0151-02-5",
            "min_bid": 99,
            "signal_type": "tax_delinquent",
        },
        {"11-54-0340-0151-02-5": {
            "apn": "11-54-0340-0151-02-5",
            "signal_type": "tax_delinquent",
            "raw_meta": {"minimum_bid": 1500.0, "county": "berrien", "catalog_id": 2049},
        }},
    )
    assert row["min_bid"] == 99
    assert row["projected"]["tax_sale_calendar"]["facts"][INGREDIENT_TAX_MIN_BID]["value"] == (
        1500.0
    )


def test_estate_query_exposes_known_sale_date_from_overlay():
    ingested = index_ingested_parcels([{
        "apn": "11-54-0340-0151-02-5",
        "signal_type": "tax_delinquent",
        "raw_meta": {
            "minimum_bid": 8000.0,
            "county": "berrien",
            "catalog_id": 2906,
            "sale_date": "2026-08-06",
            "redemption_deadline": "2026-03-31",
        },
    }])
    row = project_onto_estate_row({"apn": "11-54-0340-0151-02-5"}, ingested)
    assert row["tax_sale_date"] == "2026-08-06"
    assert row["tax_redemption_deadline"] == "2026-03-31"
    assert row["min_bid"] == 8000.0
    clocks = row["projected"]["process_clock_rows"]
    assert clocks[0]["sale_date"] == "2026-08-06"
    assert clocks[0]["redemption_deadline"] == "2026-03-31"


def test_recipes_touched_stay_runnable_not_live():
    rows = {row["recipe_id"]: row for row in recipes_touched()}
    assert set(rows) == {
        RECIPE_PUBLISHED_TAX_DISTRESS,
        RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP,
    }
    assert rows[RECIPE_PUBLISHED_TAX_DISTRESS]["live"] is False
    assert rows[RECIPE_PUBLISHED_TAX_DISTRESS]["board_status"] == "RUNNABLE_NOT_LIVE"
    assert rows[RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP]["live"] is False
    assert rows[RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP]["export_registry_status"] == "ACTIVE"


def test_projection_modules_still_refuse_forbidden_lanes():
    root = Path(__file__).resolve().parents[1] / "app" / "projection"
    blob = "".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    lowered = blob.lower()
    for needle in (
        "import supabase",
        "from supabase",
        "import deedsonar",
        "from deedsonar",
        "requests.get",
        "playwright",
        "selenium",
        "import kofile",
        "import fidlar",
        "import govos",
    ):
        assert needle not in lowered
    assert "tax_sale_info.normalize" in blob or "TaxSaleInfoNormalizer" in blob
    assert ".fetch(" not in blob

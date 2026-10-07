"""Fable TAX_YEARS_THRESHOLD unlock: expose stored counts + NC/MI/IN config.

Gap: PROJECTION_MISSING. Mode: EXPOSE_EXISTING_PLUS_CONFIG.
No scraper. No Supabase. No DeedSonar. No LIVE product path.
Missing years stay UNKNOWN — never a fabricated 0.
"""
from __future__ import annotations

from pathlib import Path

from app.free_routes.t8_open_data import match_wake
from app.free_routes.wake_delinquent import LAYOUT, RECORD_SIZE
from app.nc_dps_contract import RAW_DPS_POINTS
from app.nc_dps_raw_writer import build_raw_candidates
from app.nc_dps_wave_a_pipeline import build_property
from app.projection.classes import (
    FAMILY_TAX_YEARS_THRESHOLD,
    GAP_PROJECTION_MISSING,
    INGREDIENT_TAX_DELINQUENT_YEARS,
    NOT_RAW_SOURCE_MISSING,
    UNLOCK_EXPOSE_EXISTING_PLUS_CONFIG,
    unlock_classes,
)
from app.projection.estate_query import (
    index_ingested_parcels,
    project_onto_estate_row,
)
from app.projection.tax_years import (
    CLOCK_TAX_DELINQUENCY,
    PROCESS_CLOCK_REGISTRY,
    RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP,
    RECIPE_TAX_THRESHOLD_WATCH,
    TAX_YEAR_THRESHOLDS,
    compare_tax_years_to_threshold,
    field_census,
    parse_year_list,
    project_tax_years_facts,
    recipes_touched,
    resolve_jurisdiction,
)


def _wake_line(pin, year="2025", total_due="000000075.00"):
    buf = [" "] * RECORD_SIZE

    def put(name, val, num=False):
        start, end = next((s, e) for n, s, e in LAYOUT if n == name)
        width = end - start + 1
        text = str(val).rjust(width, "0") if num else str(val).ljust(width)
        buf[start - 1:end] = list(text[:width])

    put("tape_date", "09012026")
    put("account_number", "1", True)
    put("tax_year", year)
    put("reid", "1", True)
    put("pin", pin)
    put("primary_owner", "DOE, JANE")
    put("mailing_1", "1 MAIN ST")
    put("mailing_2", "RALEIGH NC 27601")
    put("deed_book", "12345", True)
    put("deed_page", "678", True)
    put("total_real_value", "100000", True)
    put("total_due", total_due)
    put("total_principal_due", "000000070.00")
    return "".join(buf)


def test_gap_mode_is_projection_missing_expose_existing_plus_config():
    gap, mode = unlock_classes(FAMILY_TAX_YEARS_THRESHOLD)
    assert gap == GAP_PROJECTION_MISSING
    assert mode == UNLOCK_EXPOSE_EXISTING_PLUS_CONFIG
    assert gap == "PROJECTION_MISSING"
    assert mode == "EXPOSE_EXISTING_PLUS_CONFIG"


def test_census_names_existing_stored_fields_only():
    census = field_census()
    fields = {row["field"] for row in census}
    assert fields == {
        "tax_years_due_count",
        "years_due_count",
        "tax_years_due",
        "tax_years",
        "tax_distress.delinquent_years",
    }
    for row in census:
        assert "invent" not in row["writer"].lower()


def test_projects_tax_years_due_count_as_canonical_delinquent_years():
    fact = project_tax_years_facts({
        "apn": "0648080648",
        "tax_years_due_count": "4",
        "tax_years_due": "2022|2023|2024|2025",
        "state": "NC",
    })
    years = fact["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]
    assert fact["family"] == FAMILY_TAX_YEARS_THRESHOLD
    assert fact["gap_class"] == GAP_PROJECTION_MISSING
    assert fact["unlock_mode"] == UNLOCK_EXPOSE_EXISTING_PLUS_CONFIG
    assert fact["not_class"] == NOT_RAW_SOURCE_MISSING
    assert fact["live"] is False
    assert fact["invented_years"] == []
    assert years["state"] == "KNOWN"
    assert years["value"] == 4
    assert years["source_field"] == "tax_years_due_count"
    assert fact["year_list"] == [2022, 2023, 2024, 2025]


def test_t8_years_due_count_is_an_existing_writer_not_invented(tmp_path):
    extract = tmp_path / "REAL_ESTATE_DELQ853_09012026.txt"
    extract.write_text(
        _wake_line("0648080648", year="2024") + "\n"
        + _wake_line("0648080648", year="2025") + "\n",
        encoding="latin-1",
    )
    out = match_wake(
        [{"candidate_id": "c-hit", "fips": "37183", "apn": "0648080648"}],
        str(extract),
        run_id="run1",
        now_iso="2026-09-08T00:00:00Z",
        receipt_path="r.json",
    )
    sig = out["signals"][0]
    assert sig["years_due_count"] == 2
    fact = project_tax_years_facts(sig)
    assert fact["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]["value"] == 2
    assert fact["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]["source_field"] == "years_due_count"


def test_nc_dps_packet_delinquent_years_project_without_false_zero():
    schema = {
        "properties": {
            "governance": {
                "properties": {
                    "legal_heir_declared": {"const": False},
                    "title_clear_declared": {"const": False},
                    "seller_contact_authorized": {"const": False},
                    "skip_trace_authorized": {"const": False},
                    "offer_authorized": {"const": False},
                    "funds_authorized": {"const": False},
                    "filing_authorized": {"const": False},
                    "recording_authorized": {"const": False},
                }
            }
        }
    }
    packet, _crm = build_property(
        [
            {
                "signal_id": "a" * 64,
                "source_type": "taxdelinquent",
                "geography": "37119",
                "parcel_apn": "123",
                "owner_name": "DOE",
                "confidence": 0.8,
                "detail_json": {
                    "qualification_status": "credible_distress",
                    "tax_year": 2023,
                    "tax_balance": 100.0,
                    "assessed_value": 50000,
                },
            },
            {
                "signal_id": "b" * 64,
                "source_type": "taxdelinquent",
                "geography": "37119",
                "parcel_apn": "123",
                "owner_name": "DOE",
                "confidence": 0.8,
                "detail_json": {
                    "qualification_status": "credible_distress",
                    "tax_year": 2024,
                    "tax_balance": 120.0,
                    "assessed_value": 50000,
                },
            },
        ],
        schema,
    )
    assert packet["tax_distress"]["delinquent_years"] == [2023, 2024]
    fact = project_tax_years_facts(packet)
    assert fact["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]["value"] == 2
    assert fact["year_list"] == [2023, 2024]
    assert fact["year_list_field"] == "tax_distress.delinquent_years"


def test_raw_writer_tax_years_list_is_existing_fact():
    raw, _ = build_raw_candidates([
        {
            "signal_id": "c" * 64,
            "source_type": "taxdelinquent",
            "geography": "37183",
            "parcel_apn": "9",
            "owner_name": "ROE",
            "detail_json": {"tax_year": 2022, "tax_balance": 50},
            "confidence": 0.7,
        },
        {
            "signal_id": "d" * 64,
            "source_type": "taxdelinquent",
            "geography": "37183",
            "parcel_apn": "9",
            "owner_name": "ROE",
            "detail_json": {"tax_year": 2024, "tax_balance": 60},
            "confidence": 0.7,
        },
    ])
    emerging = raw[0]["emerging_signals"]["nc_dps"]
    assert emerging["tax_years"] == [2022, 2024]
    fact = project_tax_years_facts(raw[0])
    assert fact["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]["value"] == 2
    assert fact["invented_years"] == []


def test_missing_years_stay_unknown_never_fabricated_zero():
    fact = project_tax_years_facts({
        "apn": "X",
        "tax_balance": 4324.06,
        "state": "NC",
    })
    years = fact["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]
    assert years["state"] == "UNKNOWN"
    assert years["value"] is None
    assert years["reason"] == "STORED_COUNT_NOT_ON_QUERY"
    assert fact["threshold"]["relation"] is None
    assert fact["threshold"]["reason"] == "TAX_YEARS_COUNT_UNKNOWN"
    assert fact["invented_years"] == []


def test_explicit_zero_count_is_known_not_unknown():
    fact = project_tax_years_facts({"tax_years_due_count": 0, "state": "IN"})
    years = fact["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]
    assert years["state"] == "KNOWN"
    assert years["value"] == 0
    assert fact["threshold"]["relation"] == "BELOW"


def test_tax_balance_alone_does_not_invent_a_year_count():
    fact = project_tax_years_facts({"tax_delinquent_amount": "1500", "amount_owed": 1500})
    assert fact["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]["state"] == "UNKNOWN"


def test_fractional_count_stays_unknown():
    fact = project_tax_years_facts({"tax_years_due_count": "2.5"})
    assert fact["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]["state"] == "UNKNOWN"


def test_parse_year_list_drops_junk_never_guesses():
    assert parse_year_list("2023|2024") == [2023, 2024]
    assert parse_year_list("2024;2025") == [2024, 2025]
    assert parse_year_list(["2022", "nope", 2024]) == [2022, 2024]
    assert parse_year_list("paid in full") == []
    assert parse_year_list(None) == []


def test_nc_mi_in_threshold_table_is_config_not_live():
    table = TAX_YEAR_THRESHOLDS
    assert set(table) == {"NC", "MI", "IN"}
    assert table["NC"]["compare_years"] == 2
    assert table["NC"]["authority_kind"] == "ENGINE_EXISTING"
    assert "taxes_delinquent_2_4_years" in RAW_DPS_POINTS
    assert RAW_DPS_POINTS["taxes_delinquent_2_4_years"] == 15
    assert table["MI"]["compare_years"] == 3
    assert table["IN"]["compare_years"] == 1
    for row in table.values():
        assert row["live"] is False
        assert row["county_schedule_state"] == "NOT_NORMALIZED"
        assert row["clock_id"] == CLOCK_TAX_DELINQUENCY


def test_process_clock_registry_reuses_existing_clock_id():
    clock = PROCESS_CLOCK_REGISTRY[CLOCK_TAX_DELINQUENCY]
    assert clock["live"] is False
    assert clock["county_schedule_state"] == "NOT_NORMALIZED"
    assert clock["input_ingredient"] == INGREDIENT_TAX_DELINQUENT_YEARS
    assert RECIPE_TAX_THRESHOLD_WATCH in clock["recipes"]
    assert "published_tax_distress" in clock["recipes"]


def test_compare_nc_mi_in_without_live_claim():
    nc = compare_tax_years_to_threshold(2, "NC")
    assert nc["state"] == "KNOWN"
    assert nc["relation"] == "AT_OR_ABOVE"
    assert nc["live"] is False
    assert nc["product_path"] == "RUNNABLE_NOT_LIVE"
    assert nc["recipe_id"] == RECIPE_TAX_THRESHOLD_WATCH
    mi = compare_tax_years_to_threshold(2, "MI")
    assert mi["relation"] == "BELOW"
    assert mi["threshold_years"] == 3
    indiana = compare_tax_years_to_threshold(1, "IN")
    assert indiana["relation"] == "AT_OR_ABOVE"
    tx = compare_tax_years_to_threshold(4, "TX")
    assert tx["state"] == "UNKNOWN"
    assert tx["reason"] == "JURISDICTION_THRESHOLD_NOT_CONFIGURED"
    missing = compare_tax_years_to_threshold(None, "NC")
    assert missing["reason"] == "TAX_YEARS_COUNT_UNKNOWN"
    assert missing["years"] is None


def test_resolve_jurisdiction_from_fips_prefix_only_for_configured_states():
    assert resolve_jurisdiction({"fips": "37119"}) == "NC"
    assert resolve_jurisdiction({"county_fips": "26021"}) == "MI"
    assert resolve_jurisdiction({"fips": "18141"}) == "IN"
    assert resolve_jurisdiction({"fips": "48201"}) is None
    assert resolve_jurisdiction({"state": "nc"}) == "NC"


def test_estate_query_exposes_canonical_field_from_overlay():
    ingested = index_ingested_parcels([{
        "parcel_apn": "0648080648",
        "tax_years_due_count": 3,
        "tax_years_due": "2023|2024|2025",
        "state": "NC",
    }])
    hit = project_onto_estate_row({"apn": "0648080648", "owner_name": "DOE"}, ingested)
    miss = project_onto_estate_row({"apn": "NO-MATCH", "owner_name": "NONE", "state": "NC"}, ingested)
    assert hit["tax_delinquent_years"] == 3
    assert hit["tax_years_due_count"] == 3
    assert hit["tax_years_due"] == "2023|2024|2025"
    tax = hit["projected"]["tax"]
    assert tax["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]["value"] == 3
    assert tax["threshold"]["relation"] == "AT_OR_ABOVE"
    assert tax["threshold"]["jurisdiction"] == "NC"
    assert tax["live"] is False
    assert FAMILY_TAX_YEARS_THRESHOLD in hit["projected"]["families"]
    assert hit["projected"]["invented_years"] == []
    assert hit["projected"]["supabase_mutated"] is False
    assert hit["projected"]["scraper"] is False
    assert "tax_delinquent_years" not in miss
    assert miss["projected"]["tax"]["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]["state"] == "UNKNOWN"


def test_estate_query_does_not_clobber_existing_count():
    row = project_onto_estate_row(
        {"apn": "A", "tax_years_due_count": 1, "state": "NC"},
        {"A": {"tax_years_due_count": 9}},
    )
    assert row["tax_years_due_count"] == 1
    assert row["projected"]["tax"]["facts"][INGREDIENT_TAX_DELINQUENT_YEARS]["value"] == 9


def test_recipes_touched_stay_runnable_not_live():
    rows = {row["recipe_id"]: row for row in recipes_touched()}
    assert rows[RECIPE_TAX_THRESHOLD_WATCH]["live"] is False
    assert rows[RECIPE_TAX_THRESHOLD_WATCH]["board_status"] == "RUNNABLE_NOT_LIVE"
    assert rows[RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP]["live"] is False
    assert rows[RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP]["board_status"] == "RUNNABLE_NOT_LIVE"
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

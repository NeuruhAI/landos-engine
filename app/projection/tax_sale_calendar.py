"""TAX_SALE_CALENDAR unlock: persist TaxSaleInfoNormalizer outputs as clock rows.

Gap class: NORMALIZED_FACT_MISSING.
Unlock mode: PERSIST_FROM_EXISTING_NORMALIZER.

The MI tax-sale.info normalizer already parses minimum_bid and county onto
candidate raw_meta. Those facts were never process-clock rows on the estate
query. This module persists them in-process onto PROCESS_CLOCK_REGISTRY
(tax_sale_calendar_clock) — no new DB, no Supabase, no scraper.

sale_date / redemption_deadline stay UNKNOWN unless already present on the
normalized artifact. signal_date is Jan 1 of the catalog year and is NOT a
sale date. last_sale_date is DEED_TENURE and is refused.

No LIVE claim. Missing dates are never fabricated.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from app.freshness import _to_date
from app.normalizers.tax_sale_info import TaxSaleInfoNormalizer

from .classes import (
    FAMILY_TAX_SALE_CALENDAR,
    INGREDIENT_TAX_JURISDICTION,
    INGREDIENT_TAX_MIN_BID,
    INGREDIENT_TAX_REDEMPTION_DEADLINE,
    INGREDIENT_TAX_SALE_DATE,
    NOT_RAW_SOURCE_MISSING,
    STATE_KNOWN,
    STATE_UNKNOWN,
    unlock_classes,
)
from .numbers import first_number, first_present, first_text, row_apn
from .tax_years import (
    CLOCK_TAX_SALE_CALENDAR,
    PROCESS_CLOCK_REGISTRY,
    RECIPE_PUBLISHED_TAX_DISTRESS,
    RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP,
    resolve_jurisdiction,
)

# Existing TaxSaleInfoNormalizer / ingest spellings. Do not add a key unless
# a writer already emits it. signal_date and last_sale_date are excluded.
# sale_date / event_date are generic on estate/assessor rows — only accepted
# when the row is already a tax-sale.info / taxsale artifact.
TAX_SPECIFIC_SALE_DATE_KEYS = (
    "tax_sale_date",
    "auction_date",
)
GENERIC_SALE_DATE_KEYS = (
    "sale_date",
    "event_date",
)
SALE_DATE_KEYS = TAX_SPECIFIC_SALE_DATE_KEYS + GENERIC_SALE_DATE_KEYS
REDEMPTION_KEYS = (
    "redemption_deadline",
    "redemption_date",
    "redemption_by",
    "judgment_redemption_deadline",
)
MIN_BID_KEYS = (
    "minimum_bid",
    "min_bid",
)
COUNTY_KEYS = ("county", "local_unit")
NEST_KEYS = ("raw_meta", "raw_payload", "detail_json")
# Deed-tenure / year-floor aliases — never a tax-sale calendar date.
REFUSED_DATE_KEYS = frozenset({"signal_date", "last_sale_date", "deed_date"})

FIELD_CENSUS: tuple[dict[str, str], ...] = (
    {
        "field": "raw_meta.minimum_bid",
        "writer": "app.normalizers.tax_sale_info.TaxSaleInfoNormalizer",
        "when": "CSV 'Minimum Bid' column present",
        "path": "normalized candidate / landos.signals.raw_meta",
    },
    {
        "field": "raw_meta.county",
        "writer": "app.normalizers.tax_sale_info.TaxSaleInfoNormalizer",
        "when": "CSV 'County' column or constructor county=",
        "path": "normalized candidate / landos.signals.raw_meta",
    },
    {
        "field": "raw_meta.catalog_id",
        "writer": "app.normalizers.tax_sale_info.TaxSaleInfoNormalizer",
        "when": "catalog constructor (already on every candidate)",
        "path": "normalized candidate provenance",
    },
    {
        "field": "sale_date / event_date",
        "writer": "scripts/load_signals.py map_row (event_date) when present",
        "when": "already on the ingested artifact — TaxSaleInfoNormalizer does not emit",
        "path": "discovery_signals.signal_date / estate overlay",
    },
    {
        "field": "redemption_deadline",
        "writer": "none in TaxSaleInfoNormalizer (CSV has no redemption column)",
        "when": "already on the ingested artifact only",
        "path": "UNKNOWN unless the artifact already carries it",
    },
)


def field_census() -> list[dict[str, str]]:
    return [dict(row) for row in FIELD_CENSUS]


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _layers(*sources: Optional[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flatten already-nested normalizer / ingest envelopes. No invented keys."""
    layers: list[dict[str, Any]] = []
    for src in sources:
        if not src:
            continue
        top = dict(src)
        layers.append(top)
        for nest in NEST_KEYS:
            inner = _as_mapping(top.get(nest))
            if inner:
                layers.append(inner)
    return layers


def _first_across(layers: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> tuple[Any, Optional[str]]:
    for layer in layers:
        value = first_present(layer, keys)
        if value is not None:
            key = next(k for k in keys if first_present(layer, (k,)) is not None)
            return value, key
    return None, None


def _iso_date(value: Any) -> Optional[str]:
    parsed = _to_date(value)
    return parsed.isoformat() if parsed is not None else None


def _looks_like_tax_sale_info(layers: Iterable[dict[str, Any]]) -> bool:
    """True only when the existing MI tax-sale.info normalizer already stamped this row."""
    for layer in layers:
        if first_present(layer, ("catalog_id",)) is not None:
            return True
        if first_present(layer, ("minimum_bid", "min_bid")) is not None:
            source_type = str(first_present(layer, ("signal_type", "source_type")) or "")
            if source_type in {"tax_delinquent", "taxsale"}:
                return True
        addr = str(first_present(layer, ("address",)) or "")
        if addr.rstrip().upper().endswith(", MI") and first_present(layer, ("minimum_bid", "min_bid")):
            return True
    return False


def collect_tax_sale_calendar(
    estate_row: Optional[dict] = None,
    ingested: Optional[dict] = None,
) -> dict[str, Any]:
    """Read already-parsed tax-sale.info fields. Overlay wins (already ingested)."""
    layers = _layers(ingested, estate_row)
    sale_raw, sale_field = _first_across(layers, TAX_SPECIFIC_SALE_DATE_KEYS)
    if sale_raw is None and _looks_like_tax_sale_info(layers):
        sale_raw, sale_field = _first_across(layers, GENERIC_SALE_DATE_KEYS)
    sale_date = _iso_date(sale_raw)
    if sale_date is None:
        sale_field = None

    redemption_raw, redemption_field = _first_across(layers, REDEMPTION_KEYS)
    redemption = _iso_date(redemption_raw)
    if redemption is None:
        redemption_field = None

    min_bid = None
    min_bid_field = None
    for layer in layers:
        n = first_number(layer, MIN_BID_KEYS)
        if n is not None:
            min_bid = n
            min_bid_field = next(k for k in MIN_BID_KEYS if first_present(layer, (k,)) is not None)
            break

    catalog_id = None
    for layer in layers:
        if first_present(layer, ("catalog_id",)) is not None:
            catalog_id = first_present(layer, ("catalog_id",))
            break

    taxsale_shaped = _looks_like_tax_sale_info(layers)
    county = None
    county_field = None
    # County on a generic estate row is not a tax-sale jurisdiction.
    if taxsale_shaped:
        for layer in layers:
            text = first_text(layer, COUNTY_KEYS)
            if text:
                county = text
                county_field = next(
                    k for k in COUNTY_KEYS if first_present(layer, (k,)) is not None
                )
                break

    jurisdiction = None
    jurisdiction_field = None
    if taxsale_shaped:
        jurisdiction = resolve_jurisdiction(dict(ingested or {})) or resolve_jurisdiction(
            dict(estate_row or {})
        )
        jurisdiction_field = "state" if jurisdiction else None
        if jurisdiction is None:
            # tax-sale.info is the MI catalog. Address already stamped ", MI".
            jurisdiction = "MI"
            jurisdiction_field = "tax_sale_info.MI"
    return {
        "sale_date": sale_date,
        "sale_date_field": sale_field,
        "redemption_deadline": redemption,
        "redemption_field": redemption_field,
        "min_bid": min_bid,
        "min_bid_field": min_bid_field,
        "county": county,
        "county_field": county_field,
        "jurisdiction": jurisdiction,
        "jurisdiction_field": jurisdiction_field,
        "catalog_id": catalog_id,
        "apn": row_apn(dict(ingested or {})) or row_apn(dict(estate_row or {})),
    }


def _fact(
    ingredient: str,
    value: Any,
    *,
    source_field: Optional[str],
    unit: Optional[str],
    missing_reason: str,
    missing_why: str,
) -> dict[str, Any]:
    gap, mode = unlock_classes(FAMILY_TAX_SALE_CALENDAR)
    known = value is not None
    return {
        "ingredient_id": ingredient,
        "family": FAMILY_TAX_SALE_CALENDAR,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "kind": "PERSISTED_NORMALIZER_OUTPUT",
        "state": STATE_KNOWN if known else STATE_UNKNOWN,
        "value": value if known else None,
        "unit": unit,
        "source_field": source_field,
        "reason": (
            "PERSIST_EXISTING_NORMALIZER_OUTPUT" if known else missing_reason
        ),
        "why": (
            f"Persisted existing {ingredient} from {source_field}."
            if known
            else missing_why
        ),
    }


def project_tax_sale_calendar_facts(
    estate_row: Optional[dict] = None,
    ingested: Optional[dict] = None,
) -> dict[str, Any]:
    """Project tax.sale_date / redemption / min_bid / jurisdiction from custody."""
    stored = collect_tax_sale_calendar(estate_row, ingested)
    gap, mode = unlock_classes(FAMILY_TAX_SALE_CALENDAR)
    jurisdiction_value = None
    if stored["jurisdiction"] or stored["county"]:
        jurisdiction_value = {
            "state": stored["jurisdiction"],
            "county": stored["county"],
        }
    return {
        "family": FAMILY_TAX_SALE_CALENDAR,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "clock_id": CLOCK_TAX_SALE_CALENDAR,
        "jurisdiction": stored["jurisdiction"],
        "county": stored["county"],
        "catalog_id": stored["catalog_id"],
        "invented_sale_dates": [],
        "invented_redemption_deadlines": [],
        "live": False,
        "product_path": "RUNNABLE_NOT_LIVE",
        "storage": "IN_PROCESS_PROJECTION",
        "supabase_mutated": False,
        "facts": {
            INGREDIENT_TAX_SALE_DATE: _fact(
                INGREDIENT_TAX_SALE_DATE,
                stored["sale_date"],
                source_field=stored["sale_date_field"],
                unit="date",
                missing_reason="SALE_DATE_NOT_ON_NORMALIZER_OUTPUT",
                missing_why=(
                    "TaxSaleInfoNormalizer does not emit a sale date (CSV has none; "
                    "signal_date is Jan 1 of the catalog year and is refused). "
                    "UNKNOWN, never a fabricated auction day."
                ),
            ),
            INGREDIENT_TAX_REDEMPTION_DEADLINE: _fact(
                INGREDIENT_TAX_REDEMPTION_DEADLINE,
                stored["redemption_deadline"],
                source_field=stored["redemption_field"],
                unit="date",
                missing_reason="REDEMPTION_DEADLINE_NOT_ON_NORMALIZER_OUTPUT",
                missing_why=(
                    "TaxSaleInfoNormalizer does not parse a redemption deadline "
                    "(no CSV column). UNKNOWN, never a fabricated March 31."
                ),
            ),
            INGREDIENT_TAX_MIN_BID: _fact(
                INGREDIENT_TAX_MIN_BID,
                stored["min_bid"],
                source_field=stored["min_bid_field"],
                unit="usd",
                missing_reason="MIN_BID_NOT_ON_NORMALIZER_OUTPUT",
                missing_why=(
                    "minimum_bid / min_bid not on this TaxSaleInfoNormalizer "
                    "output or estate overlay. UNKNOWN, never a fabricated floor."
                ),
            ),
            INGREDIENT_TAX_JURISDICTION: _fact(
                INGREDIENT_TAX_JURISDICTION,
                jurisdiction_value,
                source_field=stored["jurisdiction_field"] or stored["county_field"],
                unit=None,
                missing_reason="JURISDICTION_NOT_ON_NORMALIZER_OUTPUT",
                missing_why=(
                    "No county / MI stamp from TaxSaleInfoNormalizer and no "
                    "configured state/FIPS on the estate row. UNKNOWN."
                ),
            ),
        },
    }


def persist_process_clock_rows(
    candidates: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Persist already-normalized tax_sale_info candidates as process-clock rows.

    In-process only. Inventing a DB/Supabase table would open a new seam.
    """
    rows: list[dict[str, Any]] = []
    gap, mode = unlock_classes(FAMILY_TAX_SALE_CALENDAR)
    clock = dict(PROCESS_CLOCK_REGISTRY[CLOCK_TAX_SALE_CALENDAR])
    for raw in candidates:
        candidate = dict(raw)
        projected = project_tax_sale_calendar_facts(candidate)
        facts = projected["facts"]
        rows.append({
            "clock_id": CLOCK_TAX_SALE_CALENDAR,
            "clock": clock,
            "apn": row_apn(candidate) or None,
            "catalog_id": projected["catalog_id"],
            "jurisdiction": projected["jurisdiction"],
            "county": projected["county"],
            "sale_date": facts[INGREDIENT_TAX_SALE_DATE]["value"],
            "redemption_deadline": facts[INGREDIENT_TAX_REDEMPTION_DEADLINE]["value"],
            "min_bid": facts[INGREDIENT_TAX_MIN_BID]["value"],
            "family": FAMILY_TAX_SALE_CALENDAR,
            "gap_class": gap,
            "unlock_mode": mode,
            "not_class": NOT_RAW_SOURCE_MISSING,
            "live": False,
            "persisted": "IN_PROCESS",
            "storage": "IN_PROCESS_PROJECTION",
            "supabase_mutated": False,
            "invented_sale_dates": [],
            "invented_redemption_deadlines": [],
            "facts": facts,
        })
    return rows


def persist_from_existing_normalizer(
    raw_csv: str,
    *,
    catalog_id: int,
    label: str,
    signal_year: int,
    county: str = "Berrien",
    tenant_id: str = "t",
) -> list[dict[str, Any]]:
    """Run the existing TaxSaleInfoNormalizer and persist its outputs as clock rows.

    Does not call the normalizer fetch path — no network, no new catalog buy.
    """
    candidates = TaxSaleInfoNormalizer(
        tenant_id, catalog_id, label, signal_year, county=county
    ).normalize(raw_csv)
    return persist_process_clock_rows(candidates)


def recipes_touched() -> list[dict[str, Any]]:
    """Child7 board recipes that can read this clock. None are marked LIVE."""
    return [
        {
            "recipe_id": RECIPE_PUBLISHED_TAX_DISTRESS,
            "board_status": "RUNNABLE_NOT_LIVE",
            "clock_id": CLOCK_TAX_SALE_CALENDAR,
            "uses": (INGREDIENT_TAX_SALE_DATE, INGREDIENT_TAX_MIN_BID),
            "live": False,
        },
        {
            "recipe_id": RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP,
            "board_status": "RUNNABLE_NOT_LIVE",
            "export_registry_status": "ACTIVE",
            "clock_id": CLOCK_TAX_SALE_CALENDAR,
            "uses": (INGREDIENT_TAX_REDEMPTION_DEADLINE, INGREDIENT_TAX_JURISDICTION),
            "live": False,
            "note": (
                "Export registry stays ACTIVE; this unlock does not promote "
                "the Child7 board row to LIVE."
            ),
        },
    ]

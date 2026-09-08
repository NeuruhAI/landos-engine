"""TAX_YEARS_THRESHOLD unlock: expose stored year counts + NC/MI/IN config.

Gap class: PROJECTION_MISSING.
Unlock mode: EXPOSE_EXISTING_PLUS_CONFIG.

tax_years_due_count already exists on the scrub/estate overlay (~20,734 rows)
and as years_due_count on T8 Wake signals. NC DPS packets already store
tax_distress.delinquent_years. This module projects those stored facts onto
the estate query as tax.delinquent_years and adds a minimal jurisdiction
threshold table so tax_threshold_watch can compare.

No scraper. No new raw source. No DeedSonar / Supabase / LIVE product path.
Missing years stay UNKNOWN — never a fabricated 0 (that would read as
"not delinquent").
"""
from __future__ import annotations

from typing import Any, Optional

from .classes import (
    FAMILY_TAX_YEARS_THRESHOLD,
    INGREDIENT_TAX_DELINQUENT_YEARS,
    NOT_RAW_SOURCE_MISSING,
    STATE_KNOWN,
    STATE_UNKNOWN,
    unlock_classes,
)
from .numbers import as_int, first_int, first_present

# Existing stored-count keys. Order is first-seen authority, not invention.
COUNT_KEYS = (
    "tax_years_due_count",
    "years_due_count",
    "tax_delinquent_years",
    INGREDIENT_TAX_DELINQUENT_YEARS,
)
# Existing year-list keys (pipe / semicolon / list). Count may be derived
# from the unique year set when no explicit count is stored.
YEAR_LIST_KEYS = (
    "tax_years_due",
    "tax_years",
    "delinquent_years",
)

# Closed FIPS-prefix → state map for the three configured jurisdictions only.
# Other prefixes stay UNKNOWN (no invented threshold).
FIPS_PREFIX_TO_STATE = {
    "37": "NC",
    "26": "MI",
    "18": "IN",
}

# Process-clock id already named in recipe-engine RECIPE_PROCESS_CLOCK_COVERAGE.
CLOCK_TAX_DELINQUENCY = "tax_delinquency_clock"

# Board alias + existing recipe-engine id. Neither is promoted LIVE here.
RECIPE_TAX_THRESHOLD_WATCH = "tax_threshold_watch"
RECIPE_EXPIRING_APPROACHING = "expiring_approaching_tax_thresholds"
RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP = "stalled_tax_foreclosure_heirship"
RECIPE_PUBLISHED_TAX_DISTRESS = "published_tax_distress"

PROCESS_CLOCK_REGISTRY: dict[str, dict[str, Any]] = {
    CLOCK_TAX_DELINQUENCY: {
        "clock_id": CLOCK_TAX_DELINQUENCY,
        "input_ingredient": INGREDIENT_TAX_DELINQUENT_YEARS,
        "threshold_table": "TAX_YEAR_THRESHOLDS",
        "jurisdictions": ("NC", "MI", "IN"),
        "recipes": (
            RECIPE_TAX_THRESHOLD_WATCH,
            RECIPE_EXPIRING_APPROACHING,
            RECIPE_PUBLISHED_TAX_DISTRESS,
        ),
        "related_recipes": (RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP,),
        "live": False,
        "county_schedule_state": "NOT_NORMALIZED",
        "source": (
            "recipe-engine RECIPE_PROCESS_CLOCK_COVERAGE.csv "
            "(tax_delinquency_clock); landos-engine projection"
        ),
    },
}

# State-level compare floors only. County schedules stay NOT_NORMALIZED —
# recipe-engine already records that blocker. live=False: RUNNABLE_NOT_LIVE.
TAX_YEAR_THRESHOLDS: dict[str, dict[str, Any]] = {
    "NC": {
        "state": "NC",
        "compare_years": 2,
        "authority_kind": "ENGINE_EXISTING",
        "authority": (
            "app/nc_dps_contract.py RAW_DPS_POINTS "
            "taxes_delinquent_2_4_years"
        ),
        "clock_id": CLOCK_TAX_DELINQUENCY,
        "county_schedule_state": "NOT_NORMALIZED",
        "live": False,
    },
    "MI": {
        "state": "MI",
        "compare_years": 3,
        "authority_kind": "CONFIG_STATUTE",
        "authority": "MCL 211.78 General Property Tax Act forfeiture/foreclosure clock",
        "clock_id": CLOCK_TAX_DELINQUENCY,
        "county_schedule_state": "NOT_NORMALIZED",
        "live": False,
    },
    "IN": {
        "state": "IN",
        "compare_years": 1,
        "authority_kind": "CONFIG_STATUTE",
        "authority": "IC 6-1.1-24 tax-sale eligibility clock",
        "clock_id": CLOCK_TAX_DELINQUENCY,
        "county_schedule_state": "NOT_NORMALIZED",
        "live": False,
    },
}

FIELD_CENSUS: tuple[dict[str, str], ...] = (
    {
        "field": "tax_years_due_count",
        "writer": "DeedSonar scrub_facts overlay / estate page RPC",
        "when": "scrub facts applied (~20,734 rows)",
        "path": "estate/query overlay (already stored)",
    },
    {
        "field": "years_due_count",
        "writer": "app/free_routes/t8_open_data.py:match_wake",
        "when": "Wake delinquent extract HIT",
        "path": "estate/T8 signal",
    },
    {
        "field": "tax_years_due",
        "writer": "scripts/export_opportunities.py map_row",
        "when": "src.tax_years pipe-joined",
        "path": "opportunity export",
    },
    {
        "field": "tax_years",
        "writer": "app/nc_dps_raw_writer.py emerging_signals.nc_dps",
        "when": "detail_json.tax_year present",
        "path": "estate/ingest (raw NC DPS)",
    },
    {
        "field": "tax_distress.delinquent_years",
        "writer": "app/nc_dps_wave_a_pipeline.py",
        "when": "tax_year ints on signal details",
        "path": "NC DPS property packet",
    },
)


def field_census() -> list[dict[str, str]]:
    """Exact existing-field census. Empty invented list is the contract."""
    return [dict(row) for row in FIELD_CENSUS]


def process_clock_registry() -> dict[str, dict[str, Any]]:
    return {key: dict(value) for key, value in PROCESS_CLOCK_REGISTRY.items()}


def tax_year_thresholds() -> dict[str, dict[str, Any]]:
    return {key: dict(value) for key, value in TAX_YEAR_THRESHOLDS.items()}


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _year_token(value: Any) -> Optional[int]:
    n = as_int(value)
    if n is None or n < 1900 or n > 2100:
        return None
    return n


def parse_year_list(value: Any) -> list[int]:
    """Parse stored year lists. Unknown tokens are dropped, never guessed."""
    if value is None:
        return []
    if isinstance(value, bool):
        return []
    if isinstance(value, (int, float)):
        year = _year_token(value)
        return [year] if year is not None else []
    if isinstance(value, (list, tuple, set, frozenset)):
        out: list[int] = []
        seen: set[int] = set()
        for item in value:
            for year in parse_year_list(item):
                if year not in seen:
                    seen.add(year)
                    out.append(year)
        return sorted(out)
    text = str(value).strip()
    if not text:
        return []
    for sep in ("|", ";", ",", " "):
        if sep in text:
            out: list[int] = []
            seen: set[int] = set()
            for part in text.split(sep):
                year = _year_token(part)
                if year is not None and year not in seen:
                    seen.add(year)
                    out.append(year)
            return sorted(out)
    year = _year_token(text)
    return [year] if year is not None else []


def _count_from_row(row: dict) -> tuple[Optional[int], Optional[str]]:
    n = first_int(row, COUNT_KEYS)
    if n is not None:
        key = next(k for k in COUNT_KEYS if first_present(row, (k,)) is not None)
        return n, key
    return None, None


def _years_from_row(row: dict) -> tuple[list[int], Optional[str]]:
    for key in YEAR_LIST_KEYS:
        if key not in row:
            continue
        years = parse_year_list(row.get(key))
        if years:
            return years, key
    distress = _as_mapping(row.get("tax_distress"))
    years = parse_year_list(distress.get("delinquent_years"))
    if years:
        return years, "tax_distress.delinquent_years"
    emerging = _as_mapping(row.get("emerging_signals"))
    nc = _as_mapping(emerging.get("nc_dps"))
    years = parse_year_list(nc.get("tax_years"))
    if years:
        return years, "emerging_signals.nc_dps.tax_years"
    raw_meta = _as_mapping(row.get("raw_meta"))
    nc_meta = _as_mapping(raw_meta.get("nc_dps"))
    years = parse_year_list(nc_meta.get("tax_years"))
    if years:
        return years, "raw_meta.nc_dps.tax_years"
    return [], None


def collect_stored_tax_years(
    estate_row: Optional[dict] = None,
    ingested: Optional[dict] = None,
) -> dict[str, Any]:
    """Read existing stored count / year-list. Overlay wins (already ingested)."""
    overlay = dict(ingested or {})
    row = dict(estate_row or {})

    count, count_field = _count_from_row(overlay)
    if count is None:
        count, count_field = _count_from_row(row)

    years, years_field = _years_from_row(overlay)
    if not years:
        years, years_field = _years_from_row(row)

    if count is None and years:
        count = len(years)
        count_field = f"{years_field}#len" if years_field else "year_list#len"

    return {
        "count": count,
        "count_field": count_field,
        "year_list": years,
        "year_list_field": years_field,
    }


def resolve_jurisdiction(
    row: Optional[dict] = None,
    *,
    state: Any = None,
    fips: Any = None,
) -> Optional[str]:
    """NC / MI / IN only. Anything else stays UNKNOWN — no invented state."""
    src = dict(row or {})
    explicit = (state or first_present(src, ("state", "property_state")) or "")
    token = str(explicit).strip().upper()
    if token in TAX_YEAR_THRESHOLDS:
        return token
    raw_fips = str(fips if fips is not None else first_present(src, ("fips", "county_fips")) or "").strip()
    digits = "".join(ch for ch in raw_fips if ch.isdigit())
    if len(digits) >= 2:
        mapped = FIPS_PREFIX_TO_STATE.get(digits[:2])
        if mapped in TAX_YEAR_THRESHOLDS:
            return mapped
    return None


def compare_tax_years_to_threshold(
    years: Any,
    jurisdiction: Any,
    *,
    recipe_id: str = RECIPE_TAX_THRESHOLD_WATCH,
) -> dict[str, Any]:
    """Compare a stored year-count to the NC/MI/IN config table.

    RUNNABLE_NOT_LIVE: returns a relation, never a product mint / LIVE claim.
    Missing years or an unconfigured jurisdiction stay UNKNOWN.
    """
    state = str(jurisdiction or "").strip().upper() or None
    cfg = dict(TAX_YEAR_THRESHOLDS[state]) if state in TAX_YEAR_THRESHOLDS else None
    count = as_int(years)
    gap, mode = unlock_classes(FAMILY_TAX_YEARS_THRESHOLD)
    base = {
        "ingredient_id": INGREDIENT_TAX_DELINQUENT_YEARS,
        "family": FAMILY_TAX_YEARS_THRESHOLD,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "clock_id": CLOCK_TAX_DELINQUENCY,
        "recipe_id": recipe_id,
        "jurisdiction": state if cfg else None,
        "threshold_years": cfg["compare_years"] if cfg else None,
        "county_schedule_state": (
            cfg["county_schedule_state"] if cfg else "NOT_NORMALIZED"
        ),
        "live": False,
        "product_path": "RUNNABLE_NOT_LIVE",
    }
    if cfg is None:
        return {
            **base,
            "state": STATE_UNKNOWN,
            "relation": None,
            "years": count,
            "reason": "JURISDICTION_THRESHOLD_NOT_CONFIGURED",
            "why": (
                "No NC/MI/IN threshold row for this jurisdiction. "
                "County schedules are NOT_NORMALIZED; do not invent one."
            ),
        }
    if count is None:
        return {
            **base,
            "state": STATE_UNKNOWN,
            "relation": None,
            "years": None,
            "reason": "TAX_YEARS_COUNT_UNKNOWN",
            "why": (
                "Stored tax year count is missing. UNKNOWN, never treated as 0 "
                "(that would fabricate 'not delinquent')."
            ),
        }
    threshold = int(cfg["compare_years"])
    if count >= threshold:
        relation = "AT_OR_ABOVE"
        reason = "YEARS_MEET_OR_EXCEED_THRESHOLD"
    else:
        relation = "BELOW"
        reason = "YEARS_BELOW_THRESHOLD"
    return {
        **base,
        "state": STATE_KNOWN,
        "relation": relation,
        "years": count,
        "reason": reason,
        "why": (
            f"{state} compare_years={threshold} ({cfg['authority_kind']}); "
            f"stored years={count} → {relation}. Not LIVE."
        ),
        "authority": cfg["authority"],
        "authority_kind": cfg["authority_kind"],
    }


def _fact(
    value: Optional[int],
    *,
    source_field: Optional[str],
) -> dict[str, Any]:
    gap, mode = unlock_classes(FAMILY_TAX_YEARS_THRESHOLD)
    known = value is not None
    return {
        "ingredient_id": INGREDIENT_TAX_DELINQUENT_YEARS,
        "family": FAMILY_TAX_YEARS_THRESHOLD,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "kind": "PROJECTION",
        "state": STATE_KNOWN if known else STATE_UNKNOWN,
        "value": value if known else None,
        "unit": "years",
        "source_field": source_field,
        "reason": (
            "EXPOSE_EXISTING_STORED_COUNT" if known else "STORED_COUNT_NOT_ON_QUERY"
        ),
        "why": (
            f"Projected stored {INGREDIENT_TAX_DELINQUENT_YEARS} from {source_field}."
            if known
            else (
                "tax_years_due_count / years_due_count / tax_years already exist "
                "on stored facts but were not on this estate query row. "
                "UNKNOWN, never a fabricated 0."
            )
        ),
    }


def project_tax_years_facts(
    estate_row: Optional[dict] = None,
    ingested: Optional[dict] = None,
) -> dict[str, Any]:
    """Expose tax.delinquent_years from stored facts + attach threshold config."""
    stored = collect_stored_tax_years(estate_row, ingested)
    row = dict(estate_row or {})
    overlay = dict(ingested or {})
    jurisdiction = resolve_jurisdiction(overlay) or resolve_jurisdiction(row)
    comparison = compare_tax_years_to_threshold(stored["count"], jurisdiction)
    gap, mode = unlock_classes(FAMILY_TAX_YEARS_THRESHOLD)
    return {
        "family": FAMILY_TAX_YEARS_THRESHOLD,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "jurisdiction": jurisdiction,
        "year_list": list(stored["year_list"]),
        "year_list_field": stored["year_list_field"],
        "invented_years": [],
        "live": False,
        "threshold": comparison,
        "facts": {
            INGREDIENT_TAX_DELINQUENT_YEARS: _fact(
                stored["count"],
                source_field=stored["count_field"],
            ),
        },
    }


def recipes_touched() -> list[dict[str, Any]]:
    """Child8 board recipes that can read this surface. None are marked LIVE."""
    return [
        {
            "recipe_id": RECIPE_TAX_THRESHOLD_WATCH,
            "alias_of": RECIPE_EXPIRING_APPROACHING,
            "board_status": "RUNNABLE_NOT_LIVE",
            "clock_id": CLOCK_TAX_DELINQUENCY,
            "uses": "compare_tax_years_to_threshold",
            "live": False,
        },
        {
            "recipe_id": RECIPE_STALLED_TAX_FORECLOSURE_HEIRSHIP,
            "board_status": "RUNNABLE_NOT_LIVE",
            "export_registry_status": "ACTIVE",
            "clock_id": "foreclosure_transition_clock",
            "uses": INGREDIENT_TAX_DELINQUENT_YEARS,
            "live": False,
            "note": (
                "Export registry stays ACTIVE; this unlock does not promote "
                "the Child8 board row to LIVE."
            ),
        },
    ]

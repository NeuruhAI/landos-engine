"""PARCEL unlock: expose already-ingested acreage / land-use on a query row.

Gap class: PROJECTION_MISSING.
Unlock mode: PROJECTION_OF_INGESTED.
The facts live on landos-ingest / owner_leads / OneMap enrichment. This module
only projects them. No county GIS scrape. No Kofile/Fidlar/GovOS.
"""
from __future__ import annotations

from typing import Any, Optional

from .classes import (
    FAMILY_PARCEL,
    INGREDIENT_ACREAGE,
    INGREDIENT_IMPROVEMENT_VALUE,
    INGREDIENT_LAND_USE,
    INGREDIENT_LAND_VALUE,
    NOT_RAW_SOURCE_MISSING,
    STATE_KNOWN,
    STATE_UNKNOWN,
    unlock_classes,
)
from .numbers import as_number, first_number, first_present, first_text

ACREAGE_KEYS = (
    "acreage",
    "acreage_calc",
    "gisacres",
    "roll_acres",
    "acres",
    "parcel_acres",
)
LAND_USE_KEYS = (
    "land_use",
    "land_use_class",
    "property_use",
    "land_use_code",
    "parusedesc",
    "class_code",
    "land_class",
)
LAND_VALUE_KEYS = ("land_value", "land_assessed", "landval", "av_land")
IMPROVEMENT_VALUE_KEYS = (
    "improvement_value",
    "improvval",
    "building_value",
    "bldg_value",
)

# Tenure / last-transfer stay in the DEED_TENURE ingest lane.
DEED_TENURE_KEYS = frozenset(
    {"years_owned", "last_sale_date", "deed_date", "sale_date", "grantor"}
)


def _acreage_from(row: dict) -> tuple[Optional[float], Optional[str]]:
    """Prefer a positive acreage; fall back to an explicit ingested zero."""
    for key in ACREAGE_KEYS:
        if key not in row:
            continue
        n = as_number(row.get(key))
        if n is None:
            continue
        if n > 0:
            return n, key
    for key in ACREAGE_KEYS:
        if key in row and as_number(row.get(key)) == 0.0:
            return 0.0, key
    meta = row.get("parcel_meta")
    if isinstance(meta, dict):
        n = as_number(meta.get("acreage"))
        if n is not None:
            return n, "parcel_meta.acreage"
    return None, None


def _fact(
    ingredient_id: str,
    value: Any,
    *,
    source_field: Optional[str],
    unit: Optional[str] = None,
    why_unknown: str,
) -> dict[str, Any]:
    gap, mode = unlock_classes(FAMILY_PARCEL)
    known = value is not None and value != ""
    return {
        "ingredient_id": ingredient_id,
        "family": FAMILY_PARCEL,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "kind": "PROJECTION",
        "state": STATE_KNOWN if known else STATE_UNKNOWN,
        "value": value if known else None,
        "unit": unit,
        "source_field": source_field,
        "reason": "PROJECTION_OF_INGESTED" if known else "INGESTED_FACT_NOT_ON_QUERY",
        "why": (
            f"Projected ingested {ingredient_id} from {source_field}."
            if known
            else why_unknown
        ),
    }


def project_parcel_facts(
    estate_row: Optional[dict] = None,
    ingested: Optional[dict] = None,
) -> dict[str, Any]:
    """Merge estate-row leftovers with an ingested parcel overlay.

    Overlay wins: it is the already-ingested landos/OneMap/GIS fact the estate
    query failed to expose.
    """
    raw_ingested = dict(ingested or {})
    overlay = {k: v for k, v in raw_ingested.items() if k not in DEED_TENURE_KEYS}
    row = dict(estate_row or {})

    acres, acres_field = _acreage_from(overlay)
    if acres is None:
        acres, acres_field = _acreage_from(row)

    use_field = None
    land_use = first_text(overlay, LAND_USE_KEYS)
    if land_use:
        use_field = next(
            key for key in LAND_USE_KEYS if first_present(overlay, (key,)) is not None
        )
    else:
        land_use = first_text(row, LAND_USE_KEYS)
        if land_use:
            use_field = next(
                key for key in LAND_USE_KEYS if first_present(row, (key,)) is not None
            )

    land_value = first_number(overlay, LAND_VALUE_KEYS)
    land_value_field = None
    if land_value is not None:
        land_value_field = next(
            key for key in LAND_VALUE_KEYS if first_present(overlay, (key,)) is not None
        )
    else:
        land_value = first_number(row, LAND_VALUE_KEYS)
        if land_value is not None:
            land_value_field = next(
                key for key in LAND_VALUE_KEYS if first_present(row, (key,)) is not None
            )

    improv = first_number(overlay, IMPROVEMENT_VALUE_KEYS)
    improv_field = None
    if improv is not None:
        improv_field = next(
            key
            for key in IMPROVEMENT_VALUE_KEYS
            if first_present(overlay, (key,)) is not None
        )
    else:
        improv = first_number(row, IMPROVEMENT_VALUE_KEYS)
        if improv is not None:
            improv_field = next(
                key
                for key in IMPROVEMENT_VALUE_KEYS
                if first_present(row, (key,)) is not None
            )

    facts = {
        INGREDIENT_ACREAGE: _fact(
            INGREDIENT_ACREAGE,
            acres,
            source_field=acres_field,
            unit="acres",
            why_unknown=(
                "Acreage is ingested on the parcel roll but was not on this "
                "estate query row. Pass ingested_parcels to project it."
            ),
        ),
        INGREDIENT_LAND_USE: _fact(
            INGREDIENT_LAND_USE,
            land_use,
            source_field=use_field,
            why_unknown=(
                "Land-use is ingested on the parcel roll but was not on this "
                "estate query row. Pass ingested_parcels to project it."
            ),
        ),
        INGREDIENT_LAND_VALUE: _fact(
            INGREDIENT_LAND_VALUE,
            land_value,
            source_field=land_value_field,
            unit="usd",
            why_unknown="Land value was not on the ingested overlay or estate row.",
        ),
        INGREDIENT_IMPROVEMENT_VALUE: _fact(
            INGREDIENT_IMPROVEMENT_VALUE,
            improv,
            source_field=improv_field,
            unit="usd",
            why_unknown="Improvement value was not on the ingested overlay or estate row.",
        ),
    }
    gap, mode = unlock_classes(FAMILY_PARCEL)
    return {
        "family": FAMILY_PARCEL,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "facts": facts,
        "merged_aliases_ignored": sorted(DEED_TENURE_KEYS & set(raw_ingested)),
        "deed_tenure_projected": False,
    }

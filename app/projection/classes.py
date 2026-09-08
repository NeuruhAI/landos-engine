"""Fable unlock classifications. Closed set — new families fail loudly."""
from __future__ import annotations

from typing import NoReturn

FAMILY_VALUATION = "VALUATION"
FAMILY_PARCEL = "PARCEL"
FAMILY_CODE_VACANCY = "CODE_VACANCY"
FAMILY_TAX_YEARS_THRESHOLD = "TAX_YEARS_THRESHOLD"

GAP_NORMALIZED_FACT_MISSING = "NORMALIZED_FACT_MISSING"
GAP_PROJECTION_MISSING = "PROJECTION_MISSING"
GAP_RECIPE_BINDING_MISSING = "RECIPE_BINDING_MISSING"
# This lane is never a raw-source hole. A scraper would be the wrong unlock.
NOT_RAW_SOURCE_MISSING = "RAW_SOURCE_MISSING"

UNLOCK_DERIVED_FROM_EXISTING = "DERIVED_FROM_EXISTING"
UNLOCK_PROJECTION_OF_INGESTED = "PROJECTION_OF_INGESTED"
UNLOCK_PROMOTION_THEN_ACQUISITION_FREE = "PROMOTION_THEN_ACQUISITION_FREE"
UNLOCK_EXPOSE_EXISTING_PLUS_CONFIG = "EXPOSE_EXISTING_PLUS_CONFIG"

STATE_KNOWN = "KNOWN"
STATE_UNKNOWN = "UNKNOWN"

INGREDIENT_EQUITY_PCT = "equity.estimated_pct"
INGREDIENT_ACREAGE = "acreage.acres"
INGREDIENT_LAND_USE = "land.use"
INGREDIENT_LAND_VALUE = "parcel.land_value"
INGREDIENT_IMPROVEMENT_VALUE = "parcel.improvement_value"
INGREDIENT_CODE_VIOLATION = "code_violation"
INGREDIENT_VACANCY = "vacancy"
INGREDIENT_TAX_DELINQUENT_YEARS = "tax.delinquent_years"


def _unhandled(family: str) -> NoReturn:
    raise AssertionError(f"unhandled unlock family: {family!r}")


def unlock_classes(family: str) -> tuple[str, str]:
    """(gap_class, unlock_mode) for a Fable source family."""
    if family == FAMILY_VALUATION:
        return GAP_NORMALIZED_FACT_MISSING, UNLOCK_DERIVED_FROM_EXISTING
    if family == FAMILY_PARCEL:
        return GAP_PROJECTION_MISSING, UNLOCK_PROJECTION_OF_INGESTED
    if family == FAMILY_CODE_VACANCY:
        return GAP_RECIPE_BINDING_MISSING, UNLOCK_PROMOTION_THEN_ACQUISITION_FREE
    if family == FAMILY_TAX_YEARS_THRESHOLD:
        return GAP_PROJECTION_MISSING, UNLOCK_EXPOSE_EXISTING_PLUS_CONFIG
    _unhandled(family)

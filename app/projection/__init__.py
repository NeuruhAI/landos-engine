"""Engine-side Fable source unlocks: derive and project, never scrape.

VALUATION (NORMALIZED_FACT_MISSING / DERIVED_FROM_EXISTING)
    equity.estimated_pct from assessed_value + the tax-balance debt proxy.

PARCEL (PROJECTION_MISSING / PROJECTION_OF_INGESTED)
    acreage / land-use / land-vs-improvement already ingested, exposed on
    the estate query.

CODE_VACANCY (RECIPE_BINDING_MISSING / PROMOTION_THEN_ACQUISITION_FREE)
    Existing NC DPS tags (nc_dps:vacancy / nc_dps:code_violation /
    nc_dps:vacancy_or_code) bound so recipes can query them.

DEED_TENURE is a separate ingest lane and is not projected here.
"""
from .code_vacancy import (
    EXISTING_CODE_VACANCY_TAGS,
    expand_recipe_tokens,
    project_code_vacancy_facts,
    tag_census,
)
from .equity import derive_equity_estimated_pct
from .estate_query import (
    index_ingested_parcels,
    load_ingested_parcels,
    project_estate_query,
    project_onto_estate_row,
)
from .parcel import project_parcel_facts

__all__ = [
    "EXISTING_CODE_VACANCY_TAGS",
    "derive_equity_estimated_pct",
    "expand_recipe_tokens",
    "index_ingested_parcels",
    "load_ingested_parcels",
    "project_code_vacancy_facts",
    "project_estate_query",
    "project_onto_estate_row",
    "project_parcel_facts",
    "tag_census",
]

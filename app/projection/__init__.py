"""Engine-side Fable source unlocks: derive and project, never scrape.

VALUATION (NORMALIZED_FACT_MISSING / DERIVED_FROM_EXISTING)
    equity.estimated_pct from assessed_value + the tax-balance debt proxy.

PARCEL (PROJECTION_MISSING / PROJECTION_OF_INGESTED)
    acreage / land-use / land-vs-improvement already ingested, exposed on
    the estate query.

DEED_TENURE is a separate ingest lane and is not projected here.
"""
from .equity import derive_equity_estimated_pct
from .estate_query import (
    index_ingested_parcels,
    load_ingested_parcels,
    project_estate_query,
    project_onto_estate_row,
)
from .parcel import project_parcel_facts

__all__ = [
    "derive_equity_estimated_pct",
    "index_ingested_parcels",
    "load_ingested_parcels",
    "project_estate_query",
    "project_onto_estate_row",
    "project_parcel_facts",
]

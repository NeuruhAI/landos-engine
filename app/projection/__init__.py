"""Engine-side Fable source unlocks: derive and project, never scrape.

VALUATION (NORMALIZED_FACT_MISSING / DERIVED_FROM_EXISTING)
    equity.estimated_pct from assessed_value + the tax-balance debt proxy.

PARCEL (PROJECTION_MISSING / PROJECTION_OF_INGESTED)
    acreage / land-use / land-vs-improvement already ingested, exposed on
    the estate query.

CODE_VACANCY (RECIPE_BINDING_MISSING / PROMOTION_THEN_ACQUISITION_FREE)
    Existing NC DPS tags (nc_dps:vacancy / nc_dps:code_violation /
    nc_dps:vacancy_or_code) bound so recipes can query them.

TAX_YEARS_THRESHOLD (PROJECTION_MISSING / EXPOSE_EXISTING_PLUS_CONFIG)
    tax.delinquent_years from stored tax_years_due_count / years_due_count /
    tax_years lists, plus NC/MI/IN threshold config for tax_threshold_watch.
    Not LIVE.

PARCEL_GRAPH (NORMALIZED_FACT_MISSING / DERIVED_FROM_GEOMETRY)
    parcel.adjacent_apns / parcel.shared_boundary / parcel.same_owner_adjacent
    from ST_Touches on polygons already in custody (the fn_parcel_plat /
    v_mine_same_owner_adjacent relation). Corpus-scoped. Not LIVE.

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
from .parcel_graph import (
    classify_touch,
    field_census as parcel_graph_field_census,
    project_parcel_graph_facts,
)
from .tax_years import (
    PROCESS_CLOCK_REGISTRY,
    TAX_YEAR_THRESHOLDS,
    compare_tax_years_to_threshold,
    project_tax_years_facts,
)

__all__ = [
    "EXISTING_CODE_VACANCY_TAGS",
    "derive_equity_estimated_pct",
    "expand_recipe_tokens",
    "index_ingested_parcels",
    "load_ingested_parcels",
    "project_code_vacancy_facts",
    "project_estate_query",
    "project_onto_estate_row",
    "PROCESS_CLOCK_REGISTRY",
    "TAX_YEAR_THRESHOLDS",
    "compare_tax_years_to_threshold",
    "classify_touch",
    "parcel_graph_field_census",
    "project_parcel_facts",
    "project_parcel_graph_facts",
    "project_tax_years_facts",
    "tag_census",
]

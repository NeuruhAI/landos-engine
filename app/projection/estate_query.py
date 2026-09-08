"""Project VALUATION + PARCEL + CODE + TAX_YEARS + GRAPH + TAX_SALE_CALENDAR.

The CURRENT estate export is the query surface free-routes and opportunity
evaluation read. assessed_value is already on ~18k NC rows; acreage/land-use
are ingested on the parcel roll and were not exposed here. NC CODE/VACANCY
tags already land on the DPS ingest writers and were not bound for recipes.
tax_years_due_count already exists on the scrub overlay (~20,734 rows) and
was not projected as tax.delinquent_years. Parcel adjacency already exists
as ST_Touches on landos.parcel_geom (fn_parcel_plat / same-owner mine) and
was not a normalized query ingredient. TaxSaleInfoNormalizer already parses
MI min-bid / county and those were not process-clock rows. This is a
projection / persist-from-existing-normalizer job, not a source acquisition.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .classes import (
    FAMILY_CODE_VACANCY,
    FAMILY_PARCEL,
    FAMILY_PARCEL_GRAPH,
    FAMILY_TAX_SALE_CALENDAR,
    FAMILY_TAX_YEARS_THRESHOLD,
    FAMILY_VALUATION,
    INGREDIENT_ACREAGE,
    INGREDIENT_ADJACENT_APNS,
    INGREDIENT_CODE_VIOLATION,
    INGREDIENT_IMPROVEMENT_VALUE,
    INGREDIENT_LAND_USE,
    INGREDIENT_LAND_VALUE,
    INGREDIENT_SAME_OWNER_ADJACENT,
    INGREDIENT_SHARED_BOUNDARY,
    INGREDIENT_TAX_DELINQUENT_YEARS,
    INGREDIENT_TAX_JURISDICTION,
    INGREDIENT_TAX_MIN_BID,
    INGREDIENT_TAX_REDEMPTION_DEADLINE,
    INGREDIENT_TAX_SALE_DATE,
    INGREDIENT_VACANCY,
    NOT_RAW_SOURCE_MISSING,
)
from .code_vacancy import project_code_vacancy_facts
from .equity import derive_equity_estimated_pct
from .numbers import apn_keys, row_apn
from .parcel import project_parcel_facts
from .parcel_graph import build_geom_corpus, project_parcel_graph_facts
from .tax_sale_calendar import persist_process_clock_rows, project_tax_sale_calendar_facts
from .tax_years import project_tax_years_facts

PROJECTED_KEY = "projected"


def index_ingested_parcels(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Key already-ingested parcel rows by every APN spelling we can match."""
    index: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        apn = row_apn(row)
        for key in apn_keys(apn):
            index.setdefault(key, row)
    return index


def load_ingested_parcels(path: Path) -> dict[str, dict[str, Any]]:
    """Read a derive.py / owner_leads / OneMap enrichment CSV. No network."""
    with Path(path).open(newline="", encoding="utf-8") as fh:
        return index_ingested_parcels(csv.DictReader(fh))


def _lookup_ingested(
    row: Mapping[str, Any],
    ingested_parcels: Optional[Mapping[str, Mapping[str, Any]]],
) -> Optional[dict[str, Any]]:
    if not ingested_parcels:
        return None
    for key in apn_keys(row_apn(dict(row))):
        hit = ingested_parcels.get(key)
        if hit:
            return dict(hit)
    return None


def project_onto_estate_row(
    row: Mapping[str, Any],
    ingested_parcels: Optional[Mapping[str, Mapping[str, Any]]] = None,
    geom_corpus: Optional[Iterable[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Copy an estate row and attach projected VALUATION + PARCEL + CODE + TAX + GRAPH + CALENDAR."""
    out = dict(row)
    ingested = _lookup_ingested(out, ingested_parcels)
    equity = derive_equity_estimated_pct(out)
    parcel = project_parcel_facts(out, ingested)
    code_vacancy = project_code_vacancy_facts(out, ingested)
    tax_years = project_tax_years_facts(out, ingested)
    tax_sale_calendar = project_tax_sale_calendar_facts(out, ingested)
    calendar_known = any(
        fact["state"] == "KNOWN"
        for fact in tax_sale_calendar["facts"].values()
    )
    clock_rows = (
        persist_process_clock_rows([ingested or out])
        if calendar_known
        else []
    )
    corpus = (
        list(geom_corpus)
        if geom_corpus is not None
        else build_geom_corpus([out], ingested_parcels)
    )
    parcel_graph = project_parcel_graph_facts(out, ingested, corpus)
    parcel_facts = parcel["facts"]
    code_facts = code_vacancy["facts"]
    tax_facts = tax_years["facts"]
    graph_facts = parcel_graph["facts"]
    calendar_facts = tax_sale_calendar["facts"]

    out[PROJECTED_KEY] = {
        "valuation": equity,
        "parcel": parcel,
        "code_vacancy": code_vacancy,
        "tax": tax_years,
        "parcel_graph": parcel_graph,
        "tax_sale_calendar": tax_sale_calendar,
        "process_clock_rows": clock_rows,
        "supabase_mutated": False,
        "scraper": False,
        "deed_tenure_projected": False,
        "invented_tags": [],
        "invented_years": [],
        "invented_neighbors": [],
        "invented_sale_dates": [],
        "invented_redemption_deadlines": [],
        "not_class": NOT_RAW_SOURCE_MISSING,
        "families": [
            FAMILY_VALUATION,
            FAMILY_PARCEL,
            FAMILY_CODE_VACANCY,
            FAMILY_TAX_YEARS_THRESHOLD,
            FAMILY_PARCEL_GRAPH,
            FAMILY_TAX_SALE_CALENDAR,
        ],
    }

    # Flat convenience keys for query consumers. Never overwrite a populated
    # estate column — projection fills the hole, it does not clobber custody.
    if equity.get("state") == "KNOWN" and not str(out.get("equity_estimated_pct") or "").strip():
        out["equity_estimated_pct"] = equity["value"]
    acres = parcel_facts[INGREDIENT_ACREAGE]
    if acres["state"] == "KNOWN" and not str(out.get("acreage") or "").strip():
        out["acreage"] = acres["value"]
    land_use = parcel_facts[INGREDIENT_LAND_USE]
    if land_use["state"] == "KNOWN":
        if not str(out.get("land_use") or "").strip():
            out["land_use"] = land_use["value"]
        if not str(out.get("land_use_class") or "").strip():
            out["land_use_class"] = land_use["value"]
    land_value = parcel_facts[INGREDIENT_LAND_VALUE]
    if land_value["state"] == "KNOWN" and not str(out.get("land_value") or "").strip():
        out["land_value"] = land_value["value"]
    improv = parcel_facts[INGREDIENT_IMPROVEMENT_VALUE]
    if improv["state"] == "KNOWN" and not str(out.get("improvement_value") or "").strip():
        out["improvement_value"] = improv["value"]
    # CODE/VACANCY: never fabricate false. Only fill when a tagged fact is known
    # and the estate column is empty.
    code = code_facts[INGREDIENT_CODE_VIOLATION]
    if code["state"] == "KNOWN" and not str(out.get("code_violation") or "").strip():
        out["code_violation"] = True
    vacant = code_facts[INGREDIENT_VACANCY]
    if vacant["state"] == "KNOWN" and not str(out.get("vacancy") or "").strip():
        out["vacancy"] = True
    if code_vacancy["recipe_tokens"] and not out.get("code_vacancy_tokens"):
        out["code_vacancy_tokens"] = list(code_vacancy["recipe_tokens"])
    # TAX_YEARS: never fabricate 0. Only fill when a stored count is known
    # and the estate column is empty.
    delinquent_years = tax_facts[INGREDIENT_TAX_DELINQUENT_YEARS]
    if delinquent_years["state"] == "KNOWN":
        if not str(out.get("tax_delinquent_years") or "").strip():
            out["tax_delinquent_years"] = delinquent_years["value"]
        if not str(out.get("tax_years_due_count") or "").strip():
            out["tax_years_due_count"] = delinquent_years["value"]
    year_list = tax_years.get("year_list") or []
    if year_list and not str(out.get("tax_years_due") or "").strip():
        out["tax_years_due"] = "|".join(str(year) for year in year_list)
    # PARCEL_GRAPH: never fabricate false adjacency. Only fill when the
    # corpus-scoped ST_Touches relation is KNOWN and the estate column is empty.
    adjacent = graph_facts[INGREDIENT_ADJACENT_APNS]
    if adjacent["state"] == "KNOWN" and not str(out.get("adjacent_apns") or "").strip():
        out["adjacent_apns"] = list(adjacent["value"] or [])
    shared = graph_facts[INGREDIENT_SHARED_BOUNDARY]
    if shared["state"] == "KNOWN" and not str(out.get("shared_boundary") or "").strip():
        out["shared_boundary"] = bool(shared["value"])
    same_owner = graph_facts[INGREDIENT_SAME_OWNER_ADJACENT]
    if same_owner["state"] == "KNOWN" and not str(out.get("same_owner_adjacent") or "").strip():
        out["same_owner_adjacent"] = list(same_owner["value"] or [])
    # TAX_SALE_CALENDAR: never fabricate a sale date or redemption deadline.
    # Only fill when the existing normalizer/artifact already carried the fact
    # and the estate column is empty.
    sale = calendar_facts[INGREDIENT_TAX_SALE_DATE]
    if sale["state"] == "KNOWN" and not str(out.get("tax_sale_date") or "").strip():
        out["tax_sale_date"] = sale["value"]
    redemption = calendar_facts[INGREDIENT_TAX_REDEMPTION_DEADLINE]
    if redemption["state"] == "KNOWN" and not str(out.get("tax_redemption_deadline") or "").strip():
        out["tax_redemption_deadline"] = redemption["value"]
    min_bid = calendar_facts[INGREDIENT_TAX_MIN_BID]
    if min_bid["state"] == "KNOWN" and not str(out.get("min_bid") or "").strip():
        out["min_bid"] = min_bid["value"]
    jurisdiction = calendar_facts[INGREDIENT_TAX_JURISDICTION]
    if jurisdiction["state"] == "KNOWN" and not str(out.get("tax_jurisdiction") or "").strip():
        out["tax_jurisdiction"] = jurisdiction["value"]
    return out


def project_estate_query(
    rows: Iterable[Mapping[str, Any]],
    ingested_parcels: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Project every estate-query row. Pure: no DB, no network."""
    materialized = [dict(row) for row in rows]
    corpus = build_geom_corpus(materialized, ingested_parcels)
    return [
        project_onto_estate_row(row, ingested_parcels, geom_corpus=corpus)
        for row in materialized
    ]

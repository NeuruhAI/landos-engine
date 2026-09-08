"""Project VALUATION + PARCEL + CODE_VACANCY facts onto estate-query rows.

The CURRENT estate export is the query surface free-routes and opportunity
evaluation read. assessed_value is already on ~18k NC rows; acreage/land-use
are ingested on the parcel roll and were not exposed here. NC CODE/VACANCY
tags already land on the DPS ingest writers and were not bound for recipes.
This is a projection / promotion job, not a source acquisition.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .classes import (
    FAMILY_CODE_VACANCY,
    FAMILY_PARCEL,
    FAMILY_VALUATION,
    INGREDIENT_ACREAGE,
    INGREDIENT_CODE_VIOLATION,
    INGREDIENT_IMPROVEMENT_VALUE,
    INGREDIENT_LAND_USE,
    INGREDIENT_LAND_VALUE,
    INGREDIENT_VACANCY,
    NOT_RAW_SOURCE_MISSING,
)
from .code_vacancy import project_code_vacancy_facts
from .equity import derive_equity_estimated_pct
from .numbers import apn_keys, row_apn
from .parcel import project_parcel_facts

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
) -> dict[str, Any]:
    """Copy an estate row and attach projected VALUATION + PARCEL + CODE_VACANCY."""
    out = dict(row)
    ingested = _lookup_ingested(out, ingested_parcels)
    equity = derive_equity_estimated_pct(out)
    parcel = project_parcel_facts(out, ingested)
    code_vacancy = project_code_vacancy_facts(out, ingested)
    parcel_facts = parcel["facts"]
    code_facts = code_vacancy["facts"]

    out[PROJECTED_KEY] = {
        "valuation": equity,
        "parcel": parcel,
        "code_vacancy": code_vacancy,
        "supabase_mutated": False,
        "scraper": False,
        "deed_tenure_projected": False,
        "invented_tags": [],
        "not_class": NOT_RAW_SOURCE_MISSING,
        "families": [FAMILY_VALUATION, FAMILY_PARCEL, FAMILY_CODE_VACANCY],
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
    return out


def project_estate_query(
    rows: Iterable[Mapping[str, Any]],
    ingested_parcels: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Project every estate-query row. Pure: no DB, no network."""
    return [project_onto_estate_row(row, ingested_parcels) for row in rows]

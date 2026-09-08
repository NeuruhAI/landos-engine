"""Project VALUATION + PARCEL facts onto estate-query rows.

The CURRENT estate export is the query surface free-routes and opportunity
evaluation read. assessed_value is already on ~18k NC rows; acreage/land-use
are ingested on the parcel roll and were not exposed here. This is a
projection job, not a source acquisition.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .classes import (
    FAMILY_PARCEL,
    FAMILY_VALUATION,
    INGREDIENT_ACREAGE,
    INGREDIENT_IMPROVEMENT_VALUE,
    INGREDIENT_LAND_USE,
    INGREDIENT_LAND_VALUE,
    NOT_RAW_SOURCE_MISSING,
)
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
    """Copy an estate row and attach projected VALUATION + PARCEL facts."""
    out = dict(row)
    ingested = _lookup_ingested(out, ingested_parcels)
    equity = derive_equity_estimated_pct(out)
    parcel = project_parcel_facts(out, ingested)
    parcel_facts = parcel["facts"]

    out[PROJECTED_KEY] = {
        "valuation": equity,
        "parcel": parcel,
        "supabase_mutated": False,
        "scraper": False,
        "deed_tenure_projected": False,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "families": [FAMILY_VALUATION, FAMILY_PARCEL],
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
    return out


def project_estate_query(
    rows: Iterable[Mapping[str, Any]],
    ingested_parcels: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Project every estate-query row. Pure: no DB, no network."""
    return [project_onto_estate_row(row, ingested_parcels) for row in rows]

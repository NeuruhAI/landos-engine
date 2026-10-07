"""National Hunt territory expansion.

The LandOS Census authority already contains the national county universe.
This module makes that existing authority executable by turning the reserved
NATIONAL Hunt scope into the exact county list used by the existing Hunt worker.
No source, ranking, or contactability policy is introduced here.
"""
from __future__ import annotations

import json
from pathlib import Path

NATIONAL_HUNT_FIPS = "NATIONAL"
_NATIONAL_FIPS_PATH = Path(__file__).resolve().parent.parent / "data" / "national_county_fips.json"


def national_counties() -> list[dict[str, str]]:
    """Return the complete Census county universe in deterministic FIPS order."""
    payload = json.loads(_NATIONAL_FIPS_PATH.read_text(encoding="utf-8"))
    counties = payload.get("counties")
    if not isinstance(counties, dict):
        raise RuntimeError("national county census is missing its counties object")
    rows: list[dict[str, str]] = []
    for fips, meta in counties.items():
        if not isinstance(fips, str) or len(fips) != 5 or not fips.isdigit():
            raise RuntimeError(f"invalid national county FIPS: {fips!r}")
        if not isinstance(meta, dict):
            raise RuntimeError(f"invalid national county metadata for {fips}")
        state = meta.get("state")
        county = meta.get("name")
        if not isinstance(state, str) or not isinstance(county, str):
            raise RuntimeError(f"national county metadata incomplete for {fips}")
        rows.append({"fips": fips, "state": state, "county": county})
    rows.sort(key=lambda row: row["fips"])
    return rows


def expand_territory(entries: list[object]) -> list[object]:
    """Expand one reserved NATIONAL marker into the complete county territory."""
    for entry in entries:
        fips = getattr(entry, "fips", None)
        if isinstance(fips, str) and fips.strip().upper() == NATIONAL_HUNT_FIPS:
            if len(entries) != 1:
                raise ValueError("NATIONAL Hunt scope cannot be combined with explicit counties")
            return national_counties()
    return entries

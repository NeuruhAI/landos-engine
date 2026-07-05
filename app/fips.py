"""
fips.py — territory resolution against the US Census national county table.

Data: data/national_county_fips.json (census.gov national_county2020.txt,
fetched 2026-07-05; 3,235 counties; fips -> {state, name}). Resolution is
DATA-driven — no fuzzy guessing: a county name must match (case/punct-immune,
'County' suffix optional) within its state, or the caller must pass the fips
outright. Anything unresolvable raises ValueError — a hunt job never launches
against a guessed territory.
"""
import json
import os
import re
from functools import lru_cache

_DATA = os.path.join(os.path.dirname(__file__), "..", "data", "national_county_fips.json")


def _norm(s):
    """'St. Joseph County' / 'st joseph' -> 'ST JOSEPH' (case/punct-immune)."""
    s = re.sub(r"[^A-Za-z0-9 ]", " ", (s or "")).upper()
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r" (COUNTY|PARISH|BOROUGH|CENSUS AREA|MUNICIPALITY|CITY)$", "", s)


@lru_cache(maxsize=1)
def _table():
    with open(_DATA) as f:
        counties = json.load(f)["counties"]
    by_fips = {}
    by_state_name = {}
    for fips, row in counties.items():
        by_fips[fips] = {"fips": fips, "state": row["state"], "county": row["name"]}
        by_state_name[(row["state"], _norm(row["name"]))] = fips
    return by_fips, by_state_name


def resolve_county(*, fips=None, state=None, county=None):
    """One canonical {fips, state, county} or ValueError. fips wins when given
    (and must exist); else (state, county) must match the census table exactly
    (normalized). Never guessed."""
    by_fips, by_state_name = _table()
    if fips:
        f = str(fips).strip().zfill(5)
        if f not in by_fips:
            raise ValueError(f"unknown county fips {fips!r}")
        return dict(by_fips[f])
    st = (state or "").strip().upper()
    if not st or not county:
        raise ValueError("territory entry needs fips OR (state, county)")
    key = (st, _norm(county))
    if key not in by_state_name:
        raise ValueError(f"no census match for {county!r}, {st!r} — pass fips explicitly")
    return dict(by_fips[by_state_name[key]])


def parse_territory_string(s):
    """'Travis County, TX' -> resolved entry (the CLI convenience form)."""
    parts = [p.strip() for p in (s or "").split(",")]
    if len(parts) != 2:
        raise ValueError(f"expected 'County Name, ST', got {s!r}")
    return resolve_county(state=parts[1], county=parts[0])

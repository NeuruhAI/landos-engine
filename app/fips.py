"""
fips.py — territory resolution against the US Census national county table
and ZIP/ZCTA → county FIPS crosswalk.

Data:
  data/national_county_fips.json (census.gov national_county2020.txt,
    fetched 2026-07-05; 3,235 counties; fips -> {state, name})
  data/zip_county_fips.json (census.gov 2020 ZCTA5→county relationship;
    ZIP/ZCTA -> primary_county_fips + county_fips)

Resolution is DATA-driven — no fuzzy guessing, no live geo service, and
territory-engine is not this resolver. A county name must match
(case/punct-immune, 'County' suffix optional) within its state, or the
caller must pass the fips outright. A ZIP must exist in the crosswalk.
Anything unresolvable raises ValueError — a hunt job never launches against
a guessed territory. Rows like 49022/Berrien are data, not product defaults.
"""
import json
import os
import re
from functools import lru_cache

_DATA = os.path.join(os.path.dirname(__file__), "..", "data", "national_county_fips.json")
_ZIP_DATA = os.path.join(os.path.dirname(__file__), "..", "data", "zip_county_fips.json")
_GEOGRAPHY_KINDS = ("zip", "county", "state", "counties")


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


def state_county_fips(state):
    """Every county fips in ``state`` (census table), e.g. the 100 NC counties."""
    st = (state or "").strip().upper()
    return frozenset(f for f, row in _table()[0].items() if row["state"] == st)


@lru_cache(maxsize=1)
def _zip_table():
    with open(_ZIP_DATA) as f:
        return json.load(f)["zips"]


def _norm_zip(zip_code):
    digits = re.sub(r"[^0-9]", "", str(zip_code or ""))
    if len(digits) >= 5:
        digits = digits[:5]
    if len(digits) != 5:
        raise ValueError(f"invalid zip {zip_code!r}")
    return digits


def _as_dict(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    return {}


def _dedupe(entries):
    out, seen = [], set()
    for e in entries:
        f = e["fips"]
        if f not in seen:
            seen.add(f)
            out.append(e)
    return out


def resolve_zip(zip_code):
    """ZIP/ZCTA → list of canonical {fips, state, county} via zip_county_fips.json
    then resolve_county. Never guessed; unknown ZIP is ValueError."""
    z = _norm_zip(zip_code)
    row = _zip_table().get(z)
    if not row:
        raise ValueError(f"unknown zip {zip_code!r}")
    primary = str(row["primary_county_fips"]).zfill(5)
    ordered = [primary]
    for fips in row.get("county_fips") or []:
        f = str(fips).zfill(5)
        if f not in ordered:
            ordered.append(f)
    return [resolve_county(fips=f) for f in ordered]


def _counties_entries(counties):
    if not counties:
        raise ValueError("kind=counties needs a counties list")
    out = []
    for c in counties:
        if isinstance(c, (str, int)):
            out.append(resolve_county(fips=str(c)))
            continue
        row = _as_dict(c)
        out.append(resolve_county(fips=row.get("fips"), state=row.get("state"),
                                  county=row.get("county")))
    return out


def _infer_kind(*, kind, zip_code, fips, state, county, counties, value):
    if kind:
        k = str(kind).strip().lower()
        if k not in _GEOGRAPHY_KINDS:
            raise ValueError(f"unknown geography kind {kind!r}")
        return k
    if zip_code:
        return "zip"
    if counties:
        return "counties"
    if fips or county:
        return "county"
    if state:
        return "state"
    v = (value or "").strip()
    if not v:
        raise ValueError(
            "geography needs kind zip|county|state|counties "
            "(or zip/county/state/counties fields)"
        )
    if "," in v:
        return "county"
    if len(v) == 2 and v.isalpha():
        return "state"
    digits = re.sub(r"[^0-9]", "", v)
    if len(digits) >= 5:
        z = digits[:5]
        if z in _zip_table():
            return "zip"
        if z in _table()[0]:
            return "county"
    raise ValueError(f"cannot infer geography kind from {value!r}")


def resolve_geography(*, kind=None, value=None, zip_code=None, zip=None,
                      fips=None, state=None, county=None, counties=None,
                      geography=None):
    """ZIP | county (state+name or fips) | state | list of counties → territory[]
    in the same {fips, state, county} shape hunt_create already stores.
    ``geography`` may be a dict, a model, or a list of those. Never guessed."""
    if geography is not None:
        if isinstance(geography, list):
            nested = []
            for g in geography:
                nested.extend(resolve_geography(geography=g))
            return _dedupe(nested)
        g = _as_dict(geography)
        return resolve_geography(
            kind=g.get("kind"),
            value=g.get("value"),
            zip_code=g.get("zip_code") or g.get("zip"),
            fips=g.get("fips"),
            state=g.get("state"),
            county=g.get("county"),
            counties=g.get("counties"),
        )
    zip_code = zip_code or zip
    k = _infer_kind(kind=kind, zip_code=zip_code, fips=fips, state=state,
                    county=county, counties=counties, value=value)
    if k == "zip":
        return resolve_zip(zip_code or value)
    if k == "county":
        if value and not (fips or (state and county)):
            v = str(value).strip()
            if "," in v:
                return [parse_territory_string(v)]
            digits = re.sub(r"[^0-9]", "", v)
            if len(digits) >= 5:
                return [resolve_county(fips=digits[:5])]
        return [resolve_county(fips=fips, state=state, county=county)]
    if k == "state":
        st = (state or value or "").strip().upper()
        fipses = sorted(state_county_fips(st))
        if not fipses:
            raise ValueError(f"unknown state {st!r}")
        return [resolve_county(fips=f) for f in fipses]
    if k == "counties":
        return _dedupe(_counties_entries(counties))
    raise ValueError(f"unknown geography kind {kind!r}")


def resolve_hunt_inputs(territory=None, geography=None):
    """Union of explicit territory[] and geography inputs → canonical territory[].
    Existing hunt_create territory path is unchanged; geography is additive.
    Empty result is ValueError — never falls back to a fixture county."""
    out = []
    for e in territory or []:
        row = _as_dict(e)
        out.append(resolve_county(fips=row.get("fips"), state=row.get("state"),
                                  county=row.get("county")))
    if geography is not None:
        out.extend(resolve_geography(geography=geography))
    out = _dedupe(out)
    if not out:
        raise ValueError("territory or geography must name >=1 county")
    return out

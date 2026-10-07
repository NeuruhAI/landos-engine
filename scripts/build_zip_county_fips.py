"""Build data/zip_county_fips.json from the Census 2020 ZCTA→county relationship file.

Static data only — same pattern as data/national_county_fips.json. Runtime Hunt
resolution reads the JSON; this script is a rebuild tool, not a geo service.

Source:
  https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt

county_fips lists the primary county (largest AREALAND_PART) plus any other
county holding >= 5% of the ZCTA's land. Tiny slivers are kept out of Hunt
WHERE so a Benton Harbor ZIP does not drag in Van Buren as a default territory.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from collections import defaultdict

CENSUS_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
    "tab20_zcta520_county20_natl.txt"
)
LAND_SHARE_MIN = 0.05
REPO = os.path.join(os.path.dirname(__file__), "..")
OUT = os.path.join(REPO, "data", "zip_county_fips.json")
COUNTY_TABLE = os.path.join(REPO, "data", "national_county_fips.json")

# Optional USPS place names from the Hunt WHERE seed. Fixtures, not product defaults.
SEED_PLACE = {
    "49022": "Benton Harbor",
}


def _int(s):
    s = (s or "").strip()
    return int(s) if s else 0


def load_counties():
    with open(COUNTY_TABLE) as f:
        return json.load(f)["counties"]


def parse_relationship(text):
    groups = defaultdict(list)
    lines = text.splitlines()
    if not lines:
        raise SystemExit("empty census relationship file")
    header = lines[0].split("|")
    idx = {name: i for i, name in enumerate(header)}
    z_i = idx["GEOID_ZCTA5_20"]
    c_i = idx["GEOID_COUNTY_20"]
    land_i = idx["AREALAND_PART"]
    water_i = idx["AREAWATER_PART"]
    for line in lines[1:]:
        cols = line.split("|")
        if len(cols) <= max(z_i, c_i, land_i, water_i):
            continue
        zcta = cols[z_i].strip()
        county = cols[c_i].strip().zfill(5) if cols[c_i].strip() else ""
        if len(zcta) != 5 or not zcta.isdigit() or len(county) != 5:
            continue
        groups[zcta].append((_int(cols[land_i]), _int(cols[water_i]), county))
    return groups


def row_for(zcta, parts, counties):
    total_land = sum(p[0] for p in parts)
    total_water = sum(p[1] for p in parts)
    parts_sorted = sorted(parts, key=lambda p: (p[0], p[1]), reverse=True)
    primary = parts_sorted[0][2]
    if total_land > 0:
        keep = [p[2] for p in parts_sorted
                if p[2] == primary or (p[0] / total_land) >= LAND_SHARE_MIN]
    elif total_water > 0:
        keep = [p[2] for p in parts_sorted
                if p[2] == primary or (p[1] / total_water) >= LAND_SHARE_MIN]
    else:
        keep = [primary]
    # unique, primary first
    county_fips = []
    for fips in [primary] + keep:
        if fips not in county_fips:
            county_fips.append(fips)
    meta = counties.get(primary, {})
    row = {
        "primary_county_fips": primary,
        "county_fips": county_fips,
        "state": meta.get("state", ""),
    }
    place = SEED_PLACE.get(zcta)
    if place:
        row["place"] = place
    return row


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-file", help="local tab20_zcta520_county20_natl.txt")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)

    if a.from_file:
        with open(a.from_file) as f:
            text = f.read()
        source = os.path.abspath(a.from_file)
    else:
        with urllib.request.urlopen(CENSUS_URL) as resp:
            text = resp.read().decode("utf-8")
        source = CENSUS_URL

    counties = load_counties()
    groups = parse_relationship(text)
    zips = {z: row_for(z, parts, counties) for z, parts in sorted(groups.items())}
    payload = {
        "_doc": (
            "US Census 2020 ZCTA5→county relationship "
            "(www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
            "tab20_zcta520_county20_natl.txt), rebuilt 2026-09-16. "
            "ZIP/ZCTA -> primary_county_fips + county_fips (primary plus any "
            f"county with >= {int(LAND_SHARE_MIN * 100)}% of ZCTA land). "
            "Data file for app/fips.py resolve_zip / resolve_geography. "
            "Berrien/49022 is a row in this table, not a product default."
        ),
        "schema": "landos.zip_county_fips.v1",
        "source": source,
        "note": (
            "Full national ZCTA-county relationship; same data-driven pattern "
            "as national_county_fips.json. Rebuild: "
            "python scripts/build_zip_county_fips.py"
        ),
        "zips": zips,
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
        f.write("\n")
    print(f"wrote {a.out}  zips={len(zips)}  49022={zips.get('49022')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

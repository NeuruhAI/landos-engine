"""
resolve_estate_address_geocode.py — county-recorded address -> real coordinates
for the DeedSonar current estate's unmapped rows.

    python -m scripts.resolve_estate_address_geocode [--apply] [--fips 26107 ...]

WHY THIS EXISTS. The E2/E3 courts recorded these rows as having "no city, no
ZIP" and concluded address geocoding had nothing to anchor on. That was wrong,
and the error was mine: I measured `normalized.propertyCity`, a key the estate
rarely fills, and never looked at `normalized.propertyAddress`, which carries the
county's own combined record:

    7125 20 MILE RD, EVART
    288 ANGELL AVE, BARRYTON
    13930 TROUT ST, MECOSTA

263 unmapped MI rows carry that STREET, CITY shape. They were never blocked on
county parcel endpoints at all.

WHAT IT REUSES (nothing new is invented):
  * the Census batch geocoder proven in
    landos-ingest/strategy/stage_atlas_w2.py::census_batch_geocode — free, public
    domain, POST a CSV of (id, street, city, state, zip) to
    geocoding.geo.census.gov. This uses the `geographies` variant of the same
    endpoint so each match also returns STATEFP/COUNTYFP.
  * public.data_estate_coords_apply_v1 — the courted, upgrade-only apply path.
    ADDRESS_GEOCODE is one of its two MAP_READY classes; PARCEL_GIS still
    outranks it, so nothing here can downgrade a parcel-exact coordinate and a
    replay RETAINs rather than rewrites.

IDENTITY COURT, enforced per row and not by convention:
  * a Census "Match" is not enough. The returned STATEFP+COUNTYFP must equal the
    row's own county_fips. A hit that lands in the wrong county is a wrong
    parcel, and is rejected as COUNTY_MISMATCH rather than mapped.
  * "Non_Exact" matches are kept but recorded, because Census reports them
    honestly and the county check still has to pass.
  * a row whose address has no city half is not guessed at — the county is not
    substituted for the city.
  * nothing here ever writes COARSE. The 141 ZIP-centroid rows held at
    confidence 0.25 are untouched.

Default is a dry run: it geocodes, courts, and writes a receipt, mutating
nothing. --apply is the explicit second step.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

CENSUS_BATCH = "https://geocoding.geo.census.gov/geocoder/geographies/addressbatch"
BENCHMARK = "Public_AR_Current"
VINTAGE = "Current_Current"
UA = "landos-engine/1.0 (+estate-address-geocode; contact jeramieshicks@gmail.com)"
CHUNK = 500                     # well inside the 10k cap; keeps one failure small
OUT_DIR = REPO / "data" / "estate_geocode_runs"

# Census reports its own confidence only as Match/Non_Exact. 0.65 is the value
# the existing stage_atlas_w2 geocoder already assigns to a Census locations
# match; keeping it identical means one number means one thing estate-wide.
EXACT_CONFIDENCE = 0.75
NON_EXACT_CONFIDENCE = 0.65

CURRENT_BATCH_SQL = """
select b.batch_id
  from public.lead_import_batches b
 where b.import_kind = 'data_estate'
   and b.is_test is false and b.is_synthetic is false
   and (b.source_context ->> 'dataset') = 'CURRENT_CANONICAL'
   and (b.source_context ->> 'current') = 'true'
   and (b.source_context ->> 'authoritative') = 'true'
 limit 1
"""

UNRESOLVED_SQL = """
select r.normalized ->> 'property_key'    as property_key,
       r.normalized ->> 'county_fips'     as county_fips,
       r.normalized ->> 'state'           as state,
       r.normalized ->> 'apn'             as apn,
       r.normalized ->> 'propertyAddress' as property_address
  from public.lead_import_rows r
 where r.batch_id = %s
   and (r.latitude is null or r.longitude is null)
   and nullif(btrim(coalesce(r.normalized ->> 'propertyAddress', '')), '') is not null
 order by r.normalized ->> 'county_fips', r.normalized ->> 'apn'
"""


def split_address(combined: str):
    """'7125 20 MILE RD, EVART' -> ('7125 20 MILE RD', 'EVART').

    Returns (None, None) when there is no city half. The county name is NOT
    substituted for a missing city — that would geocode a guess.
    """
    if not combined or "," not in combined:
        return None, None
    street, _, city = combined.rpartition(",")
    street, city = street.strip(), city.strip()
    if not street or not city:
        return None, None
    return street, city


def census_batch(rows):
    """POST one chunk. Returns {index: parsed record}."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for index, row in enumerate(rows):
        writer.writerow([index, row["street"], row["city"], row["state"], ""])
    body = buf.getvalue().encode()

    boundary = "----landosestategeocode"
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"benchmark\"\r\n\r\n{BENCHMARK}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"vintage\"\r\n\r\n{VINTAGE}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"addressFile\"; "
        f"filename=\"a.csv\"\r\nContent-Type: text/csv\r\n\r\n"
    ).encode()
    payload = head + body + f"\r\n--{boundary}--\r\n".encode()

    request = urllib.request.Request(
        CENSUS_BATCH, data=payload,
        headers={"User-Agent": UA,
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(request, timeout=300) as response:
        raw = response.read().decode("utf-8", "replace")

    out = {}
    for record in csv.reader(io.StringIO(raw)):
        # id, input, match, exact, matched_address, coords, tigerid, side,
        # STATEFP, COUNTYFP, TRACT, BLOCK
        if len(record) < 3:
            continue
        try:
            index = int(record[0])
        except ValueError:
            continue
        entry = {"match": record[2], "exact": record[3] if len(record) > 3 else ""}
        if entry["match"] == "Match" and len(record) >= 10:
            entry["matched_address"] = record[4]
            entry["coords"] = record[5]
            entry["statefp"] = record[8]
            entry["countyfp"] = record[9]
        out[index] = entry
    return out


def court(row, entry):
    """One geocode result -> one classified outcome. Never returns a coordinate
    that failed the county check."""
    base = {
        "property_key": row["property_key"],
        "county_fips": row["county_fips"],
        "apn": row["apn"],
        "state": row["state"],
        "queried_address": f"{row['street']}, {row['city']}, {row['state']}",
        "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if not entry or entry.get("match") != "Match":
        return {**base, "outcome": "NO_MATCH",
                "detail": f"census returned {entry.get('match') if entry else 'no row'}"}

    try:
        lng, lat = (float(v) for v in entry["coords"].split(","))
    except (KeyError, ValueError):
        return {**base, "outcome": "NO_COORDS", "detail": "match without usable coordinates"}

    returned_fips = f"{entry.get('statefp','')}{entry.get('countyfp','')}"
    if returned_fips != row["county_fips"]:
        # A hit in the wrong county is a wrong parcel, not a weak one.
        return {**base, "outcome": "COUNTY_MISMATCH",
                "detail": f"census placed it in {returned_fips}, row says {row['county_fips']}"}

    exact = entry.get("exact") == "Exact"
    return {
        **base,
        "outcome": "RESOLVED",
        "latitude": lat,
        "longitude": lng,
        "coordinate_class": "ADDRESS_GEOCODE",
        "method": "census_geocoder_addressbatch",
        "precision": "street_address" if exact else "interpolated",
        "confidence": EXACT_CONFIDENCE if exact else NON_EXACT_CONFIDENCE,
        "source": f"census:{BENCHMARK}/{VINTAGE}",
        "matched_address": entry.get("matched_address"),
        "county_verified": returned_fips,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="county address -> coordinates for unmapped estate rows")
    ap.add_argument("--apply", action="store_true",
                    help="call data_estate_coords_apply_v1 (default: dry run)")
    ap.add_argument("--fips", nargs="*", default=None, help="limit to these county FIPS")
    ap.add_argument("--limit", type=int, default=None, help="cap rows attempted")
    args = ap.parse_args()

    run_id = f"estate-geocode-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        cur = conn.cursor()
        cur.execute(CURRENT_BATCH_SQL)
        batch = cur.fetchone()
        if not batch:
            print("FATAL: no current canonical data estate batch", file=sys.stderr)
            return 1
        cur.execute(UNRESOLVED_SQL, (batch["batch_id"],))
        raw_rows = cur.fetchall()

    prepared, unusable = [], []
    for row in raw_rows:
        if args.fips and row["county_fips"] not in set(args.fips):
            continue
        street, city = split_address(row["property_address"])
        if not street:
            unusable.append({**row, "outcome": "NO_CITY_IN_ADDRESS",
                             "detail": f"address has no city half: {row['property_address']!r}"})
            continue
        prepared.append({**row, "street": street, "city": city})

    if args.limit:
        prepared = prepared[: args.limit]

    print(f"run_id={run_id}")
    print(f"rows with an address: {len(raw_rows)}")
    print(f"  geocodable (street + city): {len(prepared)}")
    print(f"  no city half: {len(unusable)}")

    results = []
    for start in range(0, len(prepared), CHUNK):
        chunk = prepared[start : start + CHUNK]
        print(f"  geocoding {start + 1}-{start + len(chunk)} …")
        try:
            answers = census_batch(chunk)
        except Exception as error:                     # noqa: BLE001 - reported, not swallowed
            for row in chunk:
                results.append({"property_key": row["property_key"], "outcome": "SOURCE_ACCESS_FAILURE",
                                "detail": str(error)[:300]})
            continue
        for index, row in enumerate(chunk):
            results.append(court(row, answers.get(index)))
        time.sleep(1.0)                                # polite between batches

    resolved = [r for r in results if r["outcome"] == "RESOLVED"]
    tally = Counter(r["outcome"] for r in results)
    by_county = defaultdict(Counter)
    for r in results:
        by_county[r.get("county_fips", "?")][r["outcome"]] += 1

    receipt = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "applied": bool(args.apply),
        "rows_with_address": len(raw_rows),
        "geocodable": len(prepared),
        "no_city_half": len(unusable),
        "attempted": len(results),
        "outcomes": dict(tally),
        "by_county": {k: dict(v) for k, v in by_county.items()},
        "results": results,
    }

    if args.apply and resolved:
        payload = [{
            "property_key": r["property_key"],
            "coordinate_class": r["coordinate_class"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
        } for r in resolved]
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cur = conn.cursor()
            cur.execute(
                "select * from public.data_estate_coords_apply_v1(%s, %s, %s::jsonb)",
                (run_id, "census_geocoder_addressbatch", json.dumps(payload)),
            )
            applied = cur.fetchone()
            conn.commit()
        applied["batch_id"] = str(applied["batch_id"])
        receipt["apply_result"] = applied

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{run_id}.json"
    path.write_text(json.dumps(receipt, indent=2, default=str), encoding="utf-8")

    print()
    print(json.dumps({"attempted": len(results), "outcomes": dict(tally),
                      "apply_result": receipt.get("apply_result"),
                      "receipt": str(path)}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

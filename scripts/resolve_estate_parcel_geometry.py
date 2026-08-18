"""
resolve_estate_parcel_geometry.py — APN -> real parcel geometry for the
DeedSonar current estate's unmapped rows.

    python -m scripts.resolve_estate_parcel_geometry [--apply] [--fips 26077 ...]

WHY: 953 rows of the CURRENT_CANONICAL estate are not map-ready. 812 of them
carry no coordinates at all and hold only {apn, state, county_fips} — no city,
no ZIP, and for 308 of them no street line either. Address geocoding has nothing
to anchor on. The only identity they have is the county APN, so the resolution
path is the county's own parcel layer.

WHAT IT REUSES (nothing new is built):
  * app.providers.county_rest.CountyRESTProvider — already queries each county's
    ArcGIS layer with returnGeometry=true & outSR=4326, already computes the
    WGS84 outer-ring centroid, already strips the layer's apn_prefix back to the
    canonical county APN, and already REFUSES an endpoint an operator has not
    marked verified=True.
  * app.providers.config.COUNTY_REST_ENDPOINTS — the verified county registry.
  * public.data_estate_coords_apply_v1 — the courted, upgrade-only apply path.
    PARCEL_GIS outranks COARSE, so nothing here can downgrade a coordinate, and
    replaying the run RETAINs rather than rewrites.

NO-FABRICATION RULES, enforced here and not by convention:
  * a county with no verified endpoint is REPORTED, never guessed at
  * an APN the layer does not return is REPORTED, never approximated
  * a parcel whose geometry yields no centroid is REPORTED, never centroid-filled
  * more than one parcel for one APN is AMBIGUOUS and skipped — picking one
    would be inventing a fact the county does not assert
  * the ZIP-centroid rows already held at confidence 0.25 are never touched;
    this path only ever emits PARCEL_GIS

Default is a dry run: it resolves, classifies and writes a receipt, and mutates
nothing. --apply is the explicit second step.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from app.providers.config import COUNTY_REST_ENDPOINTS
from app.providers.county_rest import CountyRESTProvider
from app.providers.base import ProviderUnavailable

# The estate's own tenant is irrelevant to a read-only parcel lookup, but the
# provider's constructor wants one.
TENANT = os.environ.get("TENANT_ID", "1840ac90-54bd-4b3d-8baf-227c8d84ab5a")

SPACING_SECONDS = 1.0          # polite: county GIS hosts are small
OUT_DIR = REPO / "data" / "estate_geometry_runs"

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
select r.row_id,
       r.normalized ->> 'property_key' as property_key,
       r.normalized ->> 'county_fips'  as county_fips,
       r.normalized ->> 'apn'          as apn,
       r.normalized ->> 'state'        as state,
       r.coordinate_class, r.map_status
  from public.lead_import_rows r
 where r.batch_id = %s
   and (r.latitude is null or r.longitude is null)
 order by r.normalized ->> 'county_fips', r.normalized ->> 'apn'
"""


def classify_county(fips: str) -> str:
    """Why a county can or cannot be served, before a single request is made."""
    cfg = COUNTY_REST_ENDPOINTS.get(fips)
    if not cfg:
        return "COUNTY_ENDPOINT_MISSING"
    if not cfg.get("verified"):
        return "COUNTY_ENDPOINT_UNVERIFIED"
    return "PARCEL_GEOMETRY_AVAILABLE"


def resolve_row(provider: CountyRESTProvider, row: dict) -> dict:
    """One APN -> one classified outcome. Never raises past ProviderUnavailable."""
    fips, apn = row["county_fips"], row["apn"]
    result = {
        "property_key": row["property_key"],
        "county_fips": fips,
        "apn": apn,
        "state": row["state"],
        "retrieved_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    if not apn:
        return {**result, "outcome": "INSUFFICIENT_IDENTITY",
                "detail": "row carries no APN"}
    # Some source rows carry prose where the APN should be ("Parcel ID is TBD
    # Jackson"). That is a missing identity, not an APN the county failed to
    # match, and must not be reported as a county-side miss.
    if not any(ch.isdigit() for ch in apn) or "TBD" in apn.upper():
        return {**result, "outcome": "INSUFFICIENT_IDENTITY",
                "detail": f"APN field holds placeholder text: {apn!r}"}
    try:
        parcels = provider.lookup_by_apn(fips, apn)
    except ProviderUnavailable as error:
        return {**result, "outcome": "SOURCE_ACCESS_FAILURE", "detail": str(error)[:300]}

    if not parcels:
        # The layer answered and had nothing for this APN. Verified by hand for
        # Kalamazoo 2026-08-18: the estate's APNs carry the layer's own
        # NN-NN-NNN-NNN shape (06-10-314-002) and the layer holds 72 parcels
        # under 06-10-*, so this is the published layer not containing the
        # parcel — a filtered/partial view or a retired number — NOT a format
        # error on our side. Reported as such; never approximated.
        return {**result, "outcome": "APN_NOT_IN_LAYER",
                "detail": "county layer answered with no parcel for this APN"}
    if len(parcels) > 1:
        return {**result, "outcome": "AMBIGUOUS_APN",
                "detail": f"{len(parcels)} parcels returned; refusing to choose"}

    parcel = parcels[0]
    lat, lng = parcel.get("situs_lat"), parcel.get("situs_lng")
    if lat is None or lng is None:
        return {**result, "outcome": "NO_GEOMETRY",
                "detail": "parcel matched but carries no usable geometry"}

    return {
        **result,
        "outcome": "RESOLVED",
        "latitude": lat,
        "longitude": lng,
        "coordinate_class": "PARCEL_GIS",
        "method": "county_parcel_polygon_centroid_wgs84",
        "precision": "parcel",
        "confidence": 0.95,
        "source": parcel.get("provenance"),
        "matched_apn": parcel.get("parcel_apn"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="APN -> parcel geometry for unmapped estate rows")
    ap.add_argument("--apply", action="store_true",
                    help="call data_estate_coords_apply_v1 (default: dry run)")
    ap.add_argument("--fips", nargs="*", default=None,
                    help="limit to these county FIPS")
    ap.add_argument("--limit", type=int, default=None, help="cap rows attempted")
    args = ap.parse_args()

    run_id = f"estate-geometry-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    provider = CountyRESTProvider(TENANT)

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        cur = conn.cursor()
        cur.execute(CURRENT_BATCH_SQL)
        batch = cur.fetchone()
        if not batch:
            print("FATAL: no current canonical data estate batch", file=sys.stderr)
            return 1
        cur.execute(UNRESOLVED_SQL, (batch["batch_id"],))
        rows = cur.fetchall()

    by_county = defaultdict(list)
    for row in rows:
        by_county[row["county_fips"]].append(row)

    county_status = {fips: classify_county(fips) for fips in by_county}
    servable = [f for f, s in county_status.items() if s == "PARCEL_GEOMETRY_AVAILABLE"]
    if args.fips:
        servable = [f for f in servable if f in set(args.fips)]

    print(f"run_id={run_id}")
    print(f"unresolved rows: {len(rows)} across {len(by_county)} counties")
    print(f"counties with a verified parcel endpoint: {len(servable)}")
    for fips in sorted(by_county, key=lambda f: -len(by_county[f])):
        print(f"  {fips}  n={len(by_county[fips]):4d}  {county_status[fips]}")

    attempted, results = 0, []
    for fips in servable:
        for row in by_county[fips]:
            if args.limit and attempted >= args.limit:
                break
            if attempted:
                time.sleep(SPACING_SECONDS)
            outcome = resolve_row(provider, row)
            attempted += 1
            results.append(outcome)
            print(f"  {fips} {row['apn']} -> {outcome['outcome']}")

    tally = Counter(r["outcome"] for r in results)
    resolved = [r for r in results if r["outcome"] == "RESOLVED"]

    # Every county that was never attempted is reported with its reason, so the
    # remainder is never mistaken for "tried and failed".
    unattempted = {f: {"rows": len(by_county[f]), "reason": county_status[f]}
                   for f in by_county if f not in servable}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    receipt = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "applied": bool(args.apply),
        "unresolved_total": len(rows),
        "counties_total": len(by_county),
        "counties_servable": sorted(servable),
        "counties_unattempted": unattempted,
        "attempted": attempted,
        "outcomes": dict(tally),
        "results": results,
    }

    if args.apply and resolved:
        payload = [{
            "property_key": r["property_key"],
            "coordinate_class": r["coordinate_class"],
            "latitude": r["latitude"],
            "longitude": r["longitude"],
        } for r in resolved]
        source = "county_rest_parcel_centroid_wgs84"
        with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
            cur = conn.cursor()
            cur.execute(
                "select * from public.data_estate_coords_apply_v1(%s, %s, %s::jsonb)",
                (run_id, source, json.dumps(payload)),
            )
            receipt["apply_result"] = cur.fetchone()
            conn.commit()
        receipt["apply_result"]["batch_id"] = str(receipt["apply_result"]["batch_id"])

    path = OUT_DIR / f"{run_id}.json"
    path.write_text(json.dumps(receipt, indent=2, default=str), encoding="utf-8")

    print()
    print(json.dumps({"attempted": attempted, "outcomes": dict(tally),
                      "apply_result": receipt.get("apply_result"),
                      "receipt": str(path)}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

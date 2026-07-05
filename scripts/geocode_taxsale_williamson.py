"""
geocode_taxsale_williamson.py — BY-APN parcel-centroid geocode for the Williamson TX wave.

    python -m scripts.geocode_taxsale_williamson [--dry-run]

The MI discovered_taxsale leads came from county ArcGIS layers, so they carry
raw_meta->>'source_layer' and geocode_taxsale_multi.py fetch-ALLs each small county
layer. The Williamson TX wave came from the MVBA suit PDF instead — no source_layer,
and Williamson CAD is a large county (fetch-all is wrong). So this adapter geocodes
BY-APN: it asks the Williamson CAD parcels layer for exactly the R-accounts we hold,
in batched `<apn_field> IN (...)` queries, and writes each parcel's boundary centroid.

Same doctrine as the MI geocoders (all shared):
  * GEOMETRY backfill, not address geocoding — an addressless parcel's authoritative
    point is its own boundary centroid.
  * $0, stdlib urllib, honest UA, drop-not-fight on an auth/token wall.
  * Idempotent — the `situs_lat IS NULL` guard means a second run writes nothing.
  * Never fabricate a coord: an R-account absent from the CAD parcels layer (e.g. a
    manufactured-home-ONLY chattel account with no land polygon) stays NULL, honest.
  * Writes ONLY landos.owner_leads.situs_lat/lng. Projection to the board is
    sync_to_crm.py's existing situs_lat/lng path (single writer) — run it after.

Registry-driven so other by-APN CAD counties (a large-county TX/other pattern) can be
added without new code.
"""
import argparse
import json
import time
import urllib.parse
import urllib.request

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

PROVENANCE = "discovered_taxsale"
UA = "landos-engine/1.0 (+geocode-taxsale-williamson; contact jeramieshicks@gmail.com)"
CHUNK = 200          # APNs per IN() query (well under maxRecordCount 2000)
SPACING_S = 2.0      # >=2s between requests (drop-not-fight courtesy)

# By-APN CAD parcel layers, keyed by county_fips. apn_fields are the layer attributes
# that carry our parcel_apn verbatim (Williamson CAD stores the R-account in BOTH
# QuickRefID and PARCELID). Verified live: gis.wilco.org layer 0 = WCAD Parcels (polygon).
COUNTY_CAD = {
    "48491": {
        "name": "Williamson TX",
        "layer": "https://gis.wilco.org/arcgis/rest/services/public/county_wcad_parcels/MapServer/0",
        "apn_fields": ["QuickRefID", "PARCELID"],
    },
}


def _centroid(geom):
    """WGS84 boundary centroid (lat, lng) of a polygon's exterior ring. ArcGIS rings are
    [x=lng, y=lat] -> situs_lng=x-mean, situs_lat=y-mean. DO NOT FLIP."""
    rings = (geom or {}).get("rings") or []
    if not rings:
        return None
    ring = rings[0]
    if len(ring) > 1 and ring[0] == ring[-1]:      # drop the closing duplicate vertex
        ring = ring[:-1]
    if not ring:
        return None
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (sum(ys) / len(ys), sum(xs) / len(xs))  # (lat, lng)


def _q(s):
    """Single-quote-escape an APN for an ArcGIS SQL string literal (APNs are [A-Z0-9]
    in practice; this is belt-and-suspenders)."""
    return "'" + str(s).replace("'", "''") + "'"


def _fetch_centroids(layer, apn_fields, apns):
    """Return {apn: (lat, lng)} for the given APNs, querying the CAD layer by-APN in
    batches. Indexes every apn_field value on each returned feature -> its centroid,
    so a lead matches whichever id field the layer stores it in. Raises SystemExit on
    an auth/token wall (drop-not-fight — those leads stay NULL)."""
    base = layer.rstrip("/")
    if not base.endswith("/query"):
        base = base + "/query"
    out = {}
    first = True
    for i in range(0, len(apns), CHUNK):
        chunk = apns[i:i + CHUNK]
        vals = ",".join(_q(a) for a in chunk)
        where = " OR ".join(f"{fld} IN ({vals})" for fld in apn_fields)
        params = {"where": where, "outFields": ",".join(apn_fields),
                  "returnGeometry": "true", "outSR": "4326",
                  "resultRecordCount": 2000, "f": "json"}
        if not first:
            time.sleep(SPACING_S)
        first = False
        url = base + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.load(r)
        if "error" in d:
            raise SystemExit(f"ArcGIS error (drop-not-fight) on {layer}: {d['error']}")
        for f in d.get("features", []):
            c = _centroid(f.get("geometry"))
            if not c:
                continue
            attrs = f.get("attributes") or {}
            for fld in apn_fields:
                v = attrs.get(fld)
                if isinstance(v, str) and v.strip() and v.strip() not in out:
                    out[v.strip()] = c
    return out


def main(dry_run=False):
    updated_total = stayed_null_total = 0
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        for fips, cfg in COUNTY_CAD.items():
            cur.execute(
                "select lead_id, parcel_apn, raw_meta->>'property_flag' as flag "
                "from landos.owner_leads "
                "where provenance=%s and county_fips=%s and situs_lat is null "
                "and parcel_apn is not null",
                (PROVENANCE, fips))
            targets = cur.fetchall()
            if not targets:
                print(f"  [{cfg['name']} {fips}] 0 leads need geocode — skip")
                continue
            print(f"  [{cfg['name']} {fips}] {len(targets)} leads need geocode")
            apns = [t["parcel_apn"].strip() for t in targets]
            try:
                idx = _fetch_centroids(cfg["layer"], cfg["apn_fields"], apns)
            except SystemExit as e:
                print(f"    DROP (left NULL): {e}")
                stayed_null_total += len(targets)
                continue
            print(f"    CAD parcels matched: {len(idx)} distinct APN(s)")

            up = sn = 0
            misses = []
            for t in targets:
                c = idx.get((t["parcel_apn"] or "").strip())
                if not c:
                    sn += 1
                    misses.append((t["parcel_apn"], t["flag"]))
                    continue
                lat, lng = c
                if dry_run:
                    up += 1
                    continue
                cur.execute(
                    "update landos.owner_leads set situs_lat=%s, situs_lng=%s, updated_at=now() "
                    "where lead_id=%s and situs_lat is null",
                    (lat, lng, t["lead_id"]))
                up += cur.rowcount
            updated_total += up
            stayed_null_total += sn
            print(f"    {'would update' if dry_run else 'updated'}={up}  stayed_null={sn}")
            if misses:
                print("    stayed NULL (honest — no CAD land polygon; never fabricated):")
                for apn, flag in sorted(misses):
                    reason = ("manufactured-home-only chattel — no land parcel"
                              if flag == "mh_only" else "not in published WCAD parcels layer")
                    print(f"      {apn}  [{flag or '?'}] — {reason}")

        if dry_run:
            conn.rollback()
            print(f"\n=== WILLIAMSON GEOCODE DRY-RUN — would update {updated_total}, "
                  f"{stayed_null_total} stay NULL. NO writes. ===")
            return
        conn.commit()
    print(f"\ngeocode summary: updated={updated_total} stayed_null={stayed_null_total} "
          f"(by-APN CAD parcel-centroid). Next: python -m scripts.sync_to_crm")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="by-APN CAD parcel-centroid geocode (Williamson TX)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + match + preview; write nothing")
    main(dry_run=ap.parse_args().dry_run)

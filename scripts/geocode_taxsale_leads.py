"""
geocode_taxsale_leads.py — CC2 step 4b: GEOCODE BACKFILL (AXON Emergence Loop).

    python -m scripts.geocode_taxsale_leads [--dry-run]

Drops real coordinates on the 87 discovered tax-sale leads so /map can pin them. They have
situs_lat/lng NULL because the county tax-sale layer's situs ADDRESS was blank — but the SAME
layer carries the parcel POLYGON. This re-queries that layer WITH geometry (CC3 fetched it
attributes-only), computes each parcel's boundary centroid, and writes situs_lat/lng.

This is GEOMETRY backfill, not address geocoding: an addressless parcel's authoritative point
is its own boundary centroid. $0 — free public ArcGIS REST (the CC3-validated endpoint, no
token). NO ParcelProviderChain, NO paid geocoder.

Touches ONLY the 87 (provenance='discovered_taxsale' gate). Idempotent: the `situs_lat IS NULL`
guard means a second run writes nothing. A parcel with no geometry in the layer stays NULL —
honest, can't pin what has no boundary, never a fabricated placeholder coord. Projection to
public.acquisition_leads is sync_to_crm.py's existing situs_lat/lng path (no sync edit).
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
UA = "landos-engine/1.0 (+geocode-backfill; contact jeramieshicks@gmail.com)"
PAGE = 2000        # layer maxRecordCount; 88 features -> one page
SPACING_S = 2.0    # >=2s between pages (drop-not-fight courtesy)


def _layer_url(cur):
    """The one cert-sale FeatureServer URL the 87 share (read from raw_meta, never hardcoded)."""
    cur.execute("select distinct raw_meta->>'source_layer' as layer from landos.owner_leads "
                "where provenance=%s and raw_meta->>'source_layer' is not null", (PROVENANCE,))
    urls = [r["layer"] for r in cur.fetchall()]
    if len(urls) != 1:
        raise SystemExit(f"expected exactly ONE source_layer URL, got {urls!r}")
    return urls[0]


def _fetch_features(layer):
    """All features (PARCELSTAT + geometry) from the layer in WGS84. Paginates with >=2s
    spacing; drops (raises) on a token/auth wall instead of fighting it."""
    feats, offset = [], 0
    while True:
        params = {"where": "1=1", "outFields": "PARCELSTAT,PARCELID",
                  "returnGeometry": "true", "outSR": "4326",
                  "resultOffset": offset, "resultRecordCount": PAGE, "f": "json"}
        url = f"{layer}/query?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        if "error" in d:                       # token/auth wall -> drop, don't fight
            raise SystemExit(f"ArcGIS error (drop-not-fight): {d['error']}")
        batch = d.get("features", [])
        feats.extend(batch)
        if not d.get("exceededTransferLimit") or not batch:
            break
        offset += len(batch)
        time.sleep(SPACING_S)
    return feats


def _centroid(geom):
    """Boundary centroid (label point) of the exterior ring: mean of its DISTINCT vertices.
    ArcGIS rings are [x=lng, y=lat] -> situs_lng=x-mean, situs_lat=y-mean. DO NOT FLIP."""
    rings = (geom or {}).get("rings") or []
    if not rings:
        return None
    ring = rings[0]                            # exterior ring
    if len(ring) > 1 and ring[0] == ring[-1]:  # drop the closing duplicate vertex
        ring = ring[:-1]
    if not ring:
        return None
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (sum(ys) / len(ys), sum(xs) / len(xs))   # (lat, lng)


def main(dry_run=False):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        layer = _layer_url(cur)
        print(f"  layer: {layer}")
        feats = _fetch_features(layer)
        coords = {}
        for f in feats:
            ps = (f.get("attributes") or {}).get("PARCELSTAT")
            c = _centroid(f.get("geometry"))
            if ps and c:
                coords[ps] = c
        print(f"  features_fetched={len(feats)} centroids_computed={len(coords)}")

        # only the 87, only still-NULL (idempotent)
        cur.execute("select lead_id, parcel_apn from landos.owner_leads "
                    "where provenance=%s and situs_lat is null", (PROVENANCE,))
        targets = cur.fetchall()

        updated = stayed_null = 0
        preview = []
        for t in targets:
            c = coords.get(t["parcel_apn"])
            if not c:
                stayed_null += 1
                continue
            lat, lng = c
            preview.append((t["parcel_apn"], lat, lng))
            if dry_run:
                continue
            cur.execute(
                "update landos.owner_leads set situs_lat=%s, situs_lng=%s "
                "where parcel_apn=%s and provenance=%s and situs_lat is null",
                (lat, lng, t["parcel_apn"], PROVENANCE))
            updated += cur.rowcount

        if dry_run:
            conn.rollback()
            print(f"\n=== GEOCODE DRY-RUN — {len(preview)} would update, {stayed_null} stay NULL. "
                  f"NO writes. ===")
            for apn, lat, lng in preview[:8]:
                print(f"  {apn}  lat={lat:.6f} lng={lng:.6f}")
            if len(preview) > 8:
                print(f"  … +{len(preview) - 8} more")
            return
        conn.commit()

    print(f"\ngeocode summary: targets={len(targets)} updated={updated} stayed_null={stayed_null} "
          f"(provenance={PROVENANCE!r}). Next: python -m scripts.sync_to_crm")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="CC2 step 4b — geometry-centroid geocode backfill (taxsale -> owner_leads)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + compute + preview; write nothing")
    main(dry_run=ap.parse_args().dry_run)

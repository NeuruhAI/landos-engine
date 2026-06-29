"""
geocode_taxsale_multi.py — MULTI-LAYER geometry-centroid geocode (AXON cycle, additive).

    python -m scripts.geocode_taxsale_multi [--dry-run]

Generalizes scripts/geocode_taxsale_leads.py (which is St-Joseph-only: it asserts EXACTLY ONE
source_layer and crashes the moment a second county's discovered_taxsale leads exist). This
version groups the discovered_taxsale leads by raw_meta->>'source_layer' and geocodes EACH
county's layer independently, so St. Joseph + Delaware + Vanderburgh (+ any future county)
coexist. It does NOT modify or call the shipped single-layer script — it supersedes it for the
cycle while leaving it untouched (additive-only rail).

Per layer: query WITH geometry in WGS84 (outSR=4326), outFields=* so we can match the layer's
parcel-id field WITHOUT hardcoding its name (the id appears verbatim as some attribute value;
we index every string attribute value -> centroid and look the lead's parcel_apn up in it).
Handles BOTH geometry kinds: polygon (rings -> boundary centroid, same math as the shipped
script) and point (esriGeometryPoint {x:lng, y:lat}).

$0 — free public ArcGIS REST, stdlib urllib, honest UA, >=2s page spacing, drop-not-fight on an
auth/token wall (that layer's leads stay NULL; never a fabricated coord). Idempotent: the
`situs_lat IS NULL` guard means a second run writes nothing. A parcel whose id is not found in
its layer stays NULL — honest.
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
UA = "landos-engine/1.0 (+geocode-backfill-multi; contact jeramieshicks@gmail.com)"
PAGE = 2000
SPACING_S = 2.0


def _centroid(geom):
    """WGS84 centroid (lat, lng) from an ArcGIS geometry — polygon ring OR point. DO NOT FLIP."""
    if not geom:
        return None
    rings = geom.get("rings")
    if rings:
        ring = rings[0]
        if len(ring) > 1 and ring[0] == ring[-1]:
            ring = ring[:-1]
        if not ring:
            return None
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        return (sum(ys) / len(ys), sum(xs) / len(xs))
    if "x" in geom and "y" in geom and geom["x"] is not None and geom["y"] is not None:
        return (geom["y"], geom["x"])           # point: x=lng, y=lat
    return None


def _fetch_layer(layer):
    """All features (geometry + every attribute) from a layer in WGS84, paginated.
    Returns list of features. Raises SystemExit on an auth/token wall (drop-not-fight)."""
    base = layer.rstrip("/")
    if not base.endswith("/query"):
        base = base + "/query"
    feats, offset = [], 0
    while True:
        params = {"where": "1=1", "outFields": "*", "returnGeometry": "true",
                  "outSR": "4326", "resultOffset": offset, "resultRecordCount": PAGE, "f": "json"}
        url = base + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.load(r)
        if "error" in d:
            raise SystemExit(f"ArcGIS error (drop-not-fight) on {layer}: {d['error']}")
        batch = d.get("features", [])
        feats.extend(batch)
        if not d.get("exceededTransferLimit") or not batch:
            break
        offset += len(batch)
        time.sleep(SPACING_S)
    return feats


def _index_by_attr_value(feats):
    """Map every string attribute value -> centroid, so a lead's parcel_apn matches whatever
    field the layer stores it in (no per-county field config). APN values are distinctive
    enough that a non-id field colliding with a real APN is effectively impossible."""
    idx = {}
    for f in feats:
        c = _centroid(f.get("geometry"))
        if not c:
            continue
        for v in (f.get("attributes") or {}).values():
            if isinstance(v, str):
                s = v.strip()
                if s and s not in idx:
                    idx[s] = c
    return idx


def main(dry_run=False):
    updated_total = stayed_null_total = 0
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "select raw_meta->>'source_layer' as layer, count(*) n "
            "from landos.owner_leads where provenance=%s and situs_lat is null "
            "and raw_meta->>'source_layer' is not null group by layer", (PROVENANCE,))
        layers = [(r["layer"], r["n"]) for r in cur.fetchall()]
        print(f"  layers needing geocode: {len(layers)}")
        for layer, n in layers:
            print(f"  [{n:>5} leads] {layer}")
            try:
                feats = _fetch_layer(layer)
            except SystemExit as e:
                print(f"    DROP (left NULL): {e}")
                continue
            idx = _index_by_attr_value(feats)
            cur.execute("select lead_id, parcel_apn from landos.owner_leads "
                        "where provenance=%s and situs_lat is null and raw_meta->>'source_layer'=%s",
                        (PROVENANCE, layer))
            targets = cur.fetchall()
            up = sn = 0
            for t in targets:
                c = idx.get((t["parcel_apn"] or "").strip())
                if not c:
                    sn += 1
                    continue
                if dry_run:
                    up += 1
                    continue
                cur.execute("update landos.owner_leads set situs_lat=%s, situs_lng=%s "
                            "where lead_id=%s and situs_lat is null", (c[0], c[1], t["lead_id"]))
                up += cur.rowcount
            print(f"    features={len(feats)} matched/updated={up} stayed_null={sn}")
            updated_total += up
            stayed_null_total += sn
        if dry_run:
            conn.rollback()
            print(f"\n=== MULTI-GEOCODE DRY-RUN — would update {updated_total}, "
                  f"{stayed_null_total} stay NULL. NO writes. ===")
            return
        conn.commit()
    print(f"\nmulti-geocode summary: updated={updated_total} stayed_null={stayed_null_total} "
          f"(provenance={PROVENANCE!r}, per-layer parcel-centroid). Next: sync_to_crm")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="multi-layer parcel-centroid geocode (additive)")
    ap.add_argument("--dry-run", action="store_true", help="fetch + match + preview; write nothing")
    main(dry_run=ap.parse_args().dry_run)

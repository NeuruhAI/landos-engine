"""
geocode_taxsale_mi.py — MI STATEWIDE by-APN parcel-centroid geocode (registry-driven).

    python -m scripts.geocode_taxsale_mi [--dry-run] [--county 26009 --county 26077 ...]

The ~1,146 minted MI discovered_taxsale candidates came from tax-sale.info (no
raw_meta.source_layer), so geocode_taxsale_multi.py can't touch them. This geocodes
them BY-APN against each county's own free public ArcGIS parcel layer — the Williamson
TX pattern generalized to 60 MI counties via a per-county registry
(scripts/mi_parcel_endpoints.json), each entry VERIFIED against a real tax-sale APN.

Doctrine (shared with every geocoder):
  * geometry backfill (parcel boundary centroid), $0 free county endpoints only, stdlib
    urllib, honest UA, polite throttle, drop-not-fight on an auth/token wall.
  * idempotent (`situs_lat IS NULL` guard) — a re-run writes nothing new.
  * NEVER a fabricated coord: an APN absent from its county layer stays NULL with a
    reason. A county with no registry entry is UNCOVERED (listed, not guessed).
  * writes ONLY landos.owner_leads.situs_lat/lng — projection to the board is
    sync_to_crm.py's existing path (single writer).

APN matching is SEPARATOR-INSENSITIVE: our stored APNs and the county layers disagree on
dashes/spaces/padding, so we query a set of format VARIANTS and match on a CANONICAL key
(strip non-alphanumerics, uppercase). Raw ArcGIS responses are cached to
~/models/provenance/geocode_cache/mi/<fips>.json (the run is reproducible + re-inspectable).
"""
import argparse
import json
import os
import re
import time
import urllib.parse
import urllib.request

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

PROVENANCE = "discovered_taxsale"
UA = "landos-engine/1.0 (+geocode-taxsale-mi; contact jeramieshicks@gmail.com)"
REGISTRY = os.path.join(os.path.dirname(__file__), "mi_parcel_endpoints.json")
CACHE_DIR = os.path.expanduser("~/models/provenance/geocode_cache/mi")
CHUNK = 100            # APN VARIANTS per IN() query (each APN expands to several variants)
SPACING_S = 1.5       # between requests, per county (polite)


def _canon(s):
    """Canonical APN key — strip everything but alphanumerics, uppercase. Makes matching
    immune to dash/space/padding differences between our data and a county layer."""
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def apn_variants(apn):
    """Plausible on-layer spellings of one of our APNs (so the IN() query hits whatever
    separator style the county uses). Matching is still canonical; these just make the
    server-side WHERE find the row."""
    a = (apn or "").strip()
    if not a:
        return []
    out = {a, a.replace("-", " "), a.replace(" ", "-"),
           a.replace("-", ""), a.replace(" ", ""),
           re.sub(r"\s+", " ", a), re.sub(r"[^A-Za-z0-9]", "", a)}
    return [v for v in out if v]


def _centroid(geom):
    """WGS84 boundary centroid (lat, lng) of a polygon exterior ring. Rings are [lng,lat].
    DO NOT FLIP. Handles point geometry too (x=lng, y=lat)."""
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
    if geom.get("x") is not None and geom.get("y") is not None:
        return (geom["y"], geom["x"])
    return None


def _q(s):
    return "'" + str(s).replace("'", "''") + "'"


def _fetch_centroids(layer, apn_field, apns, cache_path):
    """Return {canon(apn_value): (lat,lng)} for the given APNs. Queries the layer by-APN in
    batches of format variants; caches every raw feature response. Raises SystemExit on an
    auth/token wall (drop-not-fight)."""
    base = layer.rstrip("/")
    if not base.endswith("/query"):
        base = base + "/query"
    variants = sorted({v for a in apns for v in apn_variants(a)})
    out = {}
    raw_dump = []
    first = True
    for i in range(0, len(variants), CHUNK):
        chunk = variants[i:i + CHUNK]
        where = f"{apn_field} IN ({','.join(_q(v) for v in chunk)})"
        params = {"where": where, "outFields": apn_field, "returnGeometry": "true",
                  "outSR": "4326", "resultRecordCount": 2000, "f": "json"}
        if not first:
            time.sleep(SPACING_S)
        first = False
        # POST the query: a 100-value IN() clause blows past the GET URL-length limit on
        # hosted feature services (returns 404) — POST has no such ceiling.
        body = urllib.parse.urlencode(params).encode("utf-8")
        req = urllib.request.Request(base, data=body, headers={
            "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                d = json.load(r)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            raise SystemExit(f"HTTP failure (drop-not-fight) on {layer}: {e}")
        if "error" in d:
            raise SystemExit(f"ArcGIS error (drop-not-fight) on {layer}: {d['error']}")
        for f in d.get("features", []):
            raw_dump.append(f)
            c = _centroid(f.get("geometry"))
            if not c:
                continue
            v = (f.get("attributes") or {}).get(apn_field)
            k = _canon(v)
            if k and k not in out:
                out[k] = c
    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(cache_path, "w") as fh:
            json.dump({"layer": layer, "apn_field": apn_field,
                       "features": raw_dump}, fh)
    except OSError:
        pass  # cache is best-effort, never fatal
    return out


def main(dry_run=False, only=None):
    with open(REGISTRY) as f:
        reg = json.load(f)["counties"]

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        # every MI county that still has null-coord candidates
        cur.execute(
            "select county_fips, max(raw_meta->>'county_name') county_name, count(*) n "
            "from landos.owner_leads "
            "where provenance=%s and county_fips like '26%%' and situs_lat is null "
            "group by county_fips order by n desc", (PROVENANCE,))
        pending = cur.fetchall()

        covered, uncovered, updated_total, absent_total = [], [], 0, 0
        for row in pending:
            fips = row["county_fips"]
            if only and fips not in only:
                continue
            cfg = reg.get(fips)
            if not cfg:
                uncovered.append((fips, row["county_name"], row["n"]))
                continue
            cur.execute(
                "select lead_id, parcel_apn from landos.owner_leads "
                "where provenance=%s and county_fips=%s and situs_lat is null "
                "and parcel_apn is not null", (PROVENANCE, fips))
            targets = cur.fetchall()
            apns = [t["parcel_apn"] for t in targets]
            cache_path = os.path.join(CACHE_DIR, f"{fips}.json")
            try:
                idx = _fetch_centroids(cfg["layer"], cfg["apn_field"], apns, cache_path)
            except SystemExit as e:
                print(f"  [{cfg['county']} {fips}] DROP (left NULL): {e}")
                uncovered.append((fips, cfg["county"], row["n"]))
                continue

            up = absent = 0
            misses = []
            for t in targets:
                c = idx.get(_canon(t["parcel_apn"]))
                if not c:
                    absent += 1
                    misses.append(t["parcel_apn"])
                    continue
                if dry_run:
                    up += 1
                    continue
                cur.execute(
                    "update landos.owner_leads set situs_lat=%s, situs_lng=%s, updated_at=now() "
                    "where lead_id=%s and situs_lat is null", (c[0], c[1], t["lead_id"]))
                up += cur.rowcount
            updated_total += up
            absent_total += absent
            covered.append((fips, cfg["county"], up, absent, len(targets)))
            tag = "would-geocode" if dry_run else "geocoded"
            print(f"  [{cfg['county']} {fips}] {tag}={up} absent={absent} total={len(targets)}"
                  + (f"  (absent APNs: {', '.join(misses[:6])}{'…' if len(misses) > 6 else ''})"
                     if misses else ""))

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

    print("\n=== MI GEOCODE " + ("DRY-RUN (no writes)" if dry_run else "COMMITTED") + " ===")
    print(f"covered counties: {len(covered)}  geocoded={updated_total}  absent(stay NULL)={absent_total}")
    if uncovered:
        tot = sum(n for _, _, n in uncovered)
        print(f"UNCOVERED counties: {len(uncovered)}  ({tot} candidates, no registry endpoint)")
        for fips, name, n in sorted(uncovered, key=lambda x: -x[2])[:15]:
            print(f"    {fips} {name or '?':<14} {n}")
        if len(uncovered) > 15:
            print(f"    …and {len(uncovered)-15} more")
    if not dry_run:
        print("Next: python -m scripts.sync_to_crm  (project situs_lat/lng to the board)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="MI statewide by-APN parcel-centroid geocode")
    ap.add_argument("--dry-run", action="store_true", help="fetch + match + preview; write nothing")
    ap.add_argument("--county", action="append", default=None,
                    help="limit to these county_fips (repeatable); default = all registered")
    a = ap.parse_args()
    main(dry_run=a.dry_run, only=set(a.county) if a.county else None)

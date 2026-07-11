"""
load_parcel_geom.py — JOB 7 GEOMETRY: county parcel polygons -> landos.parcel_geom.

    python -m scripts.load_parcel_geom [--county stjoseph] [--dry-run]

Implements the docs/geometry_infra_spec.md Part-2 handoff contract: the $0 county
ArcGIS REST parcel layer, paginated, stored as geometry(MultiPolygon, 4326) keyed on
(county_fips, parcel_apn). St. Joseph's Parcel25 layer carries the APN in PARCELSTAT
ALREADY in the canonical 71-form owner_leads stores — no transformation, so the join
lines up with zero fuzzy matching (contract term 1).

$0 — free public ArcGIS REST, no token. stdlib urllib, honest UA, >=1.2s spacing
between pages, drop-not-fight on an auth wall. Idempotent: upsert on
(county_fips, parcel_apn) — re-runs converge, never duplicate (contract term 3).
Honest coverage: skipped features (blank APN / missing geometry) are counted and
printed, never silently dropped (contract term 4). Existing rows are only ever
UPSERTED by this loader — nothing is deleted.

Geometry hygiene happens in SQL at insert: ST_MakeValid heals ring self-intersections,
ST_CollectionExtract(..., 3) keeps only the polygonal part, ST_Multi normalizes
Polygon -> MultiPolygon. centroid = ST_PointOnSurface (guaranteed inside, unlike
ST_Centroid) as the cheap PIP prefilter.
"""
import argparse
import json
import time
import urllib.parse
import urllib.request

import psycopg

from app.config import DATABASE_URL

UA = "landos-engine/1.0 (+parcel-geom-load; contact jeramieshicks@gmail.com)"
PAGE_SIZE = 2000          # Parcel25 maxRecordCount
SPACING_S = 1.2

# County registry — one entry per free parcel FeatureServer. Berrien MI stays a
# named gap (BS&A bans automation; no free polygon layer) per the spec.
COUNTIES = {
    "stjoseph": {
        "county_fips": "18141",
        "layer": ("https://services.arcgis.com/OjftlhRHkAABcyiF/arcgis/rest/services/"
                  "Parcel25/FeatureServer/0/query"),
        "apn_field": "PARCELSTAT",   # canonical 71-form, same string owner_leads stores
        "oid_field": "FID",
        "source": "arcgis:stjoseph_parcel25",
    },
}

UPSERT_SQL = """
    insert into landos.parcel_geom
        (county_fips, parcel_apn, geom, centroid, source, source_ref)
    select %(county_fips)s, %(parcel_apn)s, g.geom, ST_PointOnSurface(g.geom),
           %(source)s, %(source_ref)s
    from (select ST_Multi(ST_CollectionExtract(ST_MakeValid(
                     ST_SetSRID(ST_GeomFromGeoJSON(%(geom_json)s), 4326)), 3)) as geom) g
    where not ST_IsEmpty(g.geom)
    on conflict (county_fips, parcel_apn) do update
        set geom = excluded.geom, centroid = excluded.centroid,
            source = excluded.source, source_ref = excluded.source_ref,
            ingested_at = now()
"""


def page_params(oid_field, apn_field, offset, page_size=PAGE_SIZE):
    """Query params for one page — 4326 GeoJSON, stable OID ordering for pagination."""
    return {
        "where": "1=1",
        "outFields": f"{oid_field},{apn_field}",
        "outSR": "4326",
        "f": "geojson",
        "orderByFields": oid_field,
        "resultOffset": str(offset),
        "resultRecordCount": str(page_size),
    }


def feature_to_row(feature, cfg):
    """One GeoJSON feature -> upsert params, or (None, reason) for an honest skip."""
    props = feature.get("properties") or {}
    apn = (props.get(cfg["apn_field"]) or "").strip()
    if not apn:
        return None, "no_apn"
    geom = feature.get("geometry")
    if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
        return None, "bad_geom"
    oid = props.get(cfg["oid_field"])
    return {
        "county_fips": cfg["county_fips"],
        "parcel_apn": apn,
        "geom_json": json.dumps(geom),
        "source": cfg["source"],
        "source_ref": str(oid) if oid is not None else None,
    }, None


def _fetch_page(cfg, offset):
    url = cfg["layer"] + "?" + urllib.parse.urlencode(
        page_params(cfg["oid_field"], cfg["apn_field"], offset))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.load(r)
    if "error" in d:                          # token/auth wall -> drop, don't fight
        raise SystemExit(f"ArcGIS error (drop-not-fight): {d['error']}")
    return d.get("features", [])


def _upsert_page(cur, rows):
    """Batch upsert; on a batch failure, retry per-row so ONE bad geometry is
    isolated and reported instead of sinking the whole page."""
    try:
        cur.executemany(UPSERT_SQL, rows)
        return len(rows), []
    except psycopg.Error:
        cur.connection.rollback()
        ok, bad = 0, []
        for row in rows:
            try:
                cur.execute(UPSERT_SQL, row)
                cur.connection.commit()
                ok += 1
            except psycopg.Error as e:
                cur.connection.rollback()
                bad.append((row["parcel_apn"], str(e).splitlines()[0]))
        return ok, bad


def main(county="stjoseph", dry_run=False):
    cfg = COUNTIES[county]
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from landos.parcel_geom where county_fips=%s",
                    (cfg["county_fips"],))
        before = cur.fetchone()[0]
        print(f"[census] before: parcel_geom {cfg['county_fips']} = {before}")

        seen = upserted = 0
        skips = {"no_apn": 0, "bad_geom": 0}
        bad_rows = []
        offset = 0
        while True:
            if offset:
                time.sleep(SPACING_S)
            feats = _fetch_page(cfg, offset)
            if not feats:
                break
            seen += len(feats)
            rows = []
            for f in feats:
                row, skip = feature_to_row(f, cfg)
                if row is None:
                    skips[skip] += 1
                else:
                    rows.append(row)
            if dry_run:
                print(f"  [dry-run] page@{offset}: {len(feats)} features, "
                      f"{len(rows)} loadable — stopping after first page")
                return
            ok, bad = _upsert_page(cur, rows)
            conn.commit()
            upserted += ok
            bad_rows.extend(bad)
            offset += PAGE_SIZE
            if offset % 20000 == 0:
                print(f"  ...{seen} fetched / {upserted} upserted")
            if len(feats) < PAGE_SIZE:
                break

        cur.execute("select count(*), count(distinct parcel_apn) "
                    "from landos.parcel_geom where county_fips=%s",
                    (cfg["county_fips"],))
        after, distinct = cur.fetchone()
        print(f"[census] fetched={seen} upserted={upserted} "
              f"skipped_no_apn={skips['no_apn']} skipped_bad_geom={skips['bad_geom']} "
              f"insert_failed={len(bad_rows)}")
        for apn, err in bad_rows[:10]:
            print(f"  [insert_failed] {apn}: {err}")
        print(f"[census] after: parcel_geom {cfg['county_fips']} = {after} "
              f"(distinct APN {distinct})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", default="stjoseph", choices=sorted(COUNTIES))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    main(county=a.county, dry_run=a.dry_run)

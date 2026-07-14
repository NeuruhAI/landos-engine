"""
load_nfhl_sfha.py — FEMA NFHL Special Flood Hazard Area polygons -> overlay_polygons
(geometry_infra_spec Part 4, load half).

    python -m scripts.load_nfhl_sfha [--county 18141] [--dry-run]

Pulls S_Fld_Haz_Ar (NFHL MapServer layer 28, $0 federal open data, no key) for
each geometry-covered county, SFHA zones only (SFHA_TF='T'), and upserts them
into landos.overlay_polygons matching the existing table idiom exactly
(ingest_overlays precedent): GeoJSON geometry as jsonb + bbox columns, natural
key (tenant_id, overlay_type, name).

Posture — UNARMED AREA-HEAT (brownfield precedent): rows load authoritative
(FEMA is the authority on its own zones) with multiplier_weight=0.0, so overlay
hits are LOGGED and applied as x1.0. Arming a nonzero weight is a scoring-policy
ruling = operator move (a column update, no redeploy). Re-pulls NEVER clobber an
armed weight: multiplier_weight is deliberately absent from the conflict update.

tenant_id = NULL: SFHA zones are geography facts, not tenant data (same
tenant-agnostic canon as discovery_signals). Additive: a future FIRM revision
adds new FLD_AR_ID rows; superseded rows persist (AI never deletes).
"""
import argparse
import json

import psycopg
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from scripts.ingest_overlays import _bbox, _get_geojson

NFHL_LAYER_28 = ("https://hazards.fema.gov/arcgis/rest/services/public/"
                 "NFHL/MapServer/28/query")
OVERLAY_TYPE = "flood_sfha"
PAGE_SIZE = 1000

# Geometry-covered counties only (landos.parcel_geom has their boundaries).
# Berrien 26021 stays a named gap: no parcel polygons -> no cross to run,
# so its zones are not pulled (honest scope, same gate as the PIP resolver).
COUNTIES = {
    "18141": {"dfirm": "18141C", "label": "St. Joseph County, IN"},
    "18091": {"dfirm": "18091C", "label": "LaPorte County, IN"},
}

UPSERT_SQL = """
    insert into landos.overlay_polygons
      (tenant_id, overlay_type, name, geometry, source, provenance_status,
       effective_date, multiplier_weight, buffer_m, minx, miny, maxx, maxy)
    values (null, %(overlay_type)s, %(name)s, %(geometry)s, %(source)s,
            'authoritative', null, 0.0, null,
            %(minx)s, %(miny)s, %(maxx)s, %(maxy)s)
    on conflict (tenant_id, overlay_type, name) do update set
      geometry = excluded.geometry,
      source = excluded.source,
      minx = excluded.minx, miny = excluded.miny,
      maxx = excluded.maxx, maxy = excluded.maxy,
      updated_at = now()
"""


def page_params(dfirm: str, offset: int) -> dict:
    """ArcGIS REST params: SFHA-only for one DFIRM, 4326 GeoJSON, stable paging."""
    return {
        "where": f"DFIRM_ID='{dfirm}' AND SFHA_TF='T'",
        "outFields": "DFIRM_ID,FLD_AR_ID,FLD_ZONE,ZONE_SUBTY,STATIC_BFE",
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
        "orderByFields": "FLD_AR_ID",
        "resultOffset": str(offset),
        "resultRecordCount": str(PAGE_SIZE),
    }


def zone_name(dfirm: str, props: dict) -> str:
    """Stable natural-key name: NFHL <DFIRM> <FLD_AR_ID> zone <FLD_ZONE>.
    cross_flood_sfha parses the county back out of the DFIRM prefix."""
    return (f"NFHL {dfirm} {str(props.get('FLD_AR_ID') or '').strip()} "
            f"zone {str(props.get('FLD_ZONE') or '?').strip()}")


def feature_to_row(feat: dict, dfirm: str):
    """GeoJSON feature -> overlay row dict, or (None, reason) honest skip."""
    props = feat.get("properties") or {}
    geom = feat.get("geometry")
    if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
        return None, "bad_geom"
    if not str(props.get("FLD_AR_ID") or "").strip():
        return None, "no_fld_ar_id"
    minx, miny, maxx, maxy = _bbox(geom)
    return {
        "overlay_type": OVERLAY_TYPE,
        "name": zone_name(dfirm, props),
        "geometry": Jsonb(geom),
        "source": (f"FEMA NFHL MapServer/28 S_Fld_Haz_Ar; DFIRM_ID={dfirm}; "
                   f"SFHA_TF=T; zone_subty={props.get('ZONE_SUBTY') or ''}; "
                   f"static_bfe={props.get('STATIC_BFE')}"),
        "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy,
    }, None


def main(counties=None, dry_run: bool = False) -> dict:
    selected = sorted(counties or COUNTIES)
    totals = {}
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        for fips in selected:
            cfg = COUNTIES[fips]
            fetched = upserted = 0
            skips = {"bad_geom": 0, "no_fld_ar_id": 0}
            offset = 0
            while True:
                fc = _get_geojson(NFHL_LAYER_28, page_params(cfg["dfirm"], offset))
                feats = fc.get("features") or []
                if not feats:
                    break
                fetched += len(feats)
                for f in feats:
                    row, skip = feature_to_row(f, cfg["dfirm"])
                    if row is None:
                        skips[skip] += 1
                        continue
                    if dry_run:
                        upserted += 1
                        continue
                    cur.execute(UPSERT_SQL, row)
                    upserted += 1
                offset += PAGE_SIZE
                if len(feats) < PAGE_SIZE:
                    break
            totals[fips] = {"fetched": fetched, "upserted": upserted, **skips}
            print(f"[{cfg['label']}] fetched={fetched} upserted={upserted} "
                  f"skipped_bad_geom={skips['bad_geom']} "
                  f"skipped_no_fld_ar_id={skips['no_fld_ar_id']}"
                  + ("  [dry-run: no writes]" if dry_run else ""))
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
            cur.execute("select count(*) from landos.overlay_polygons "
                        "where overlay_type = %s", (OVERLAY_TYPE,))
            print(f"[census] overlay_polygons {OVERLAY_TYPE} = {cur.fetchone()[0]} "
                  f"(authoritative, weight 0.0 = UNARMED area-heat)")
    return totals


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", action="append", choices=sorted(COUNTIES),
                    help="county fips; repeatable (default: all covered)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    main(a.county, dry_run=a.dry_run)

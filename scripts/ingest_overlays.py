"""
ingest_overlays.py — populate landos.overlay_polygons with provenance-gated
policy overlays for the Benton Harbor, MI geography.

Provenance gate (enforced later by app/multiplier.py):
  * Opportunity Zone  -> 'authoritative'        (multiplier APPLIED)
  * Brownfield / Ox Creek proxy / Housing TIF -> 'sourced_unconfirmed' / 'stub'
                                                 (LOGGED, applied as x1.0)

The table is geography-agnostic: new geographies are new rows, not new code.

Sources (authoritative OZ pipeline):
  - Designated-QOZ tract list: CDFI Fund (Notice 2018-48)
    https://www.cdfifund.gov/system/files/documents/designated-qozs.12.14.18.xlsx
  - Tract polygon geometry: Census TIGERweb 2020 tracts (layer 10)
    https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/Tracts_Blocks/MapServer/10
Local overlays (best-effort, unconfirmed):
  - EGLE RRD Open Data (brownfields layer 3): https://gisagoegle.state.mi.us/arcgis/rest/services/EGLE/RRDOpenData/MapServer/3

Usage:
    python -m scripts.ingest_overlays
"""
import json
import os
import sys
import urllib.parse
import urllib.request

import psycopg
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

# --- The "macro / policy layer": OZ tailwind weight + sunset are DATA, not code.
#     Override without redeploy via env. effective_date = §1400Z-2 deferral
#     recognition date (IRS FAQ); the OZ tailwind expires after it.
OZ_MULTIPLIER_WEIGHT = float(os.environ.get("OZ_MULTIPLIER_WEIGHT", "0.30"))   # -> x1.30
OZ_EFFECTIVE_DATE = os.environ.get("OZ_EFFECTIVE_DATE", "2026-12-31")

# Berrien County (FIPS 26021) designated QOZ tracts — from the CDFI list,
# cross-checked against HUD's Opportunity_Zones service during planning.
BERRIEN_QOZ_GEOIDS = [
    "26021000400",  # Tract 4  — Benton Harbor (downtown / 133 E Main)
    "26021002500",  # Tract 25 — Benton Harbor
    "26021002100",  # Tract 21 — Benton Charter Twp (1850 Pipestone Rd)
    "26021010300",  # Tract 103
    "26021020500",  # Tract 205
]
CDFI_SOURCE = "CDFI designated-QOZ list (Notice 2018-48) + Census TIGERweb L10 geometry"

TIGERWEB = ("https://tigerweb.geo.census.gov/arcgis/rest/services/"
            "TIGERweb/Tracts_Blocks/MapServer/10/query")
EGLE_BROWNFIELDS = ("https://gisagoegle.state.mi.us/arcgis/rest/services/"
                    "EGLE/RRDOpenData/MapServer/3/query")
BROWNFIELD_BUFFER_M = 150.0      # hit radius around an EGLE brownfield point
OX_CREEK_PROXY_BUFFER_M = 400.0  # wider radius for the Ox Creek Pathway proxy


def _get_geojson(url: str, params: dict) -> dict:
    """GET an ArcGIS REST query as GeoJSON (FeatureCollection)."""
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"User-Agent": "landos-engine/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _bbox(geom: dict):
    """(minx, miny, maxx, maxy) over any GeoJSON Polygon/MultiPolygon/Point."""
    xs, ys = [], []

    def walk(c):
        if isinstance(c, (int, float)):
            return
        if c and isinstance(c[0], (int, float)):
            xs.append(c[0]); ys.append(c[1]); return
        for sub in c:
            walk(sub)

    if not geom:
        return (None, None, None, None)
    walk(geom.get("coordinates", []))
    if not xs:
        return (None, None, None, None)
    return (min(xs), min(ys), max(xs), max(ys))


def _upsert(cur, *, overlay_type, name, geometry, source, provenance, effective_date,
            multiplier_weight, buffer_m):
    minx, miny, maxx, maxy = _bbox(geometry) if geometry else (None, None, None, None)
    cur.execute(
        """
        insert into landos.overlay_polygons
          (tenant_id, overlay_type, name, geometry, source, provenance_status,
           effective_date, multiplier_weight, buffer_m, minx, miny, maxx, maxy)
        values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        on conflict (tenant_id, overlay_type, name) do update set
          geometry=excluded.geometry, source=excluded.source,
          provenance_status=excluded.provenance_status,
          effective_date=excluded.effective_date,
          multiplier_weight=excluded.multiplier_weight, buffer_m=excluded.buffer_m,
          minx=excluded.minx, miny=excluded.miny, maxx=excluded.maxx, maxy=excluded.maxy,
          updated_at=now()
        """,
        (TENANT, overlay_type, name, Jsonb(geometry) if geometry else None, source,
         provenance, effective_date or None, multiplier_weight, buffer_m,
         minx, miny, maxx, maxy),
    )


def ingest_opportunity_zones(cur) -> int:
    n = 0
    for geoid in BERRIEN_QOZ_GEOIDS:
        try:
            fc = _get_geojson(TIGERWEB, {
                "where": f"GEOID='{geoid}'", "outFields": "GEOID,NAME",
                "returnGeometry": "true", "outSR": "4326", "f": "geojson",
            })
        except Exception as e:  # noqa: BLE001
            print(f"  OZ {geoid}: fetch failed ({e.__class__.__name__}) — skipped")
            continue
        feats = fc.get("features") or []
        if not feats:
            print(f"  OZ {geoid}: no geometry returned — skipped")
            continue
        geom = feats[0]["geometry"]
        _upsert(cur, overlay_type="opportunity_zone",
                name=f"QOZ Tract {geoid}", geometry=geom, source=CDFI_SOURCE,
                provenance="authoritative", effective_date=OZ_EFFECTIVE_DATE,
                multiplier_weight=OZ_MULTIPLIER_WEIGHT, buffer_m=None)
        n += 1
        print(f"  OZ {geoid}: authoritative, weight {OZ_MULTIPLIER_WEIGHT} "
              f"(eff {OZ_EFFECTIVE_DATE})")
    return n


def ingest_brownfields_and_oxcreek(cur) -> int:
    try:
        fc = _get_geojson(EGLE_BROWNFIELDS, {
            "where": "City LIKE '%BENTON HARBOR%'",
            "outFields": "ProjectName,City,SiteAddress,ParcelTaxID",
            "returnGeometry": "true", "outSR": "4326", "f": "geojson",
        })
    except Exception as e:  # noqa: BLE001
        print(f"  EGLE brownfields: fetch failed ({e.__class__.__name__}) — skipped")
        return 0
    n = 0
    for f in fc.get("features", []):
        props = f.get("properties", {}) or {}
        geom = f.get("geometry")
        if not geom:
            continue
        pname = (props.get("ProjectName") or "Brownfield site").strip()
        addr = (props.get("SiteAddress") or "").strip()
        label = f"{pname}" + (f" ({addr})" if addr else "")
        _upsert(cur, overlay_type="brownfield", name=f"EGLE: {label}", geometry=geom,
                source=f"EGLE RRDOpenData L3 brownfields; ParcelTaxID={props.get('ParcelTaxID')}",
                provenance="sourced_unconfirmed", effective_date=None,
                multiplier_weight=0.0, buffer_m=BROWNFIELD_BUFFER_M)
        n += 1
        # Ox Creek corridor PROXY: the EGLE "Ox Creek Pathway" project point, used
        # as a stand-in until the adopted 2025 corridor polygon is digitized.
        if "ox creek" in pname.lower():
            _upsert(cur, overlay_type="ox_creek_corridor",
                    name=f"Ox Creek Pathway (EGLE proxy: {label})", geometry=geom,
                    source="PROXY: EGLE Ox Creek Pathway brownfield point — NOT the adopted "
                           "corridor polygon; digitize/confirm the 2025 BH Ox Creek "
                           "Revitalization Plan boundary before promoting.",
                    provenance="sourced_unconfirmed", effective_date=None,
                    multiplier_weight=0.0, buffer_m=OX_CREEK_PROXY_BUFFER_M)
            print(f"  Ox Creek proxy: {label}")
    print(f"  EGLE brownfields: {n} Benton Harbor sites (sourced_unconfirmed)")
    return n


def ingest_housing_tif_stub(cur) -> int:
    _upsert(cur, overlay_type="bh_housing_tif",
            name="Benton Harbor Housing TIF / DDA district (boundary pending)",
            geometry=None,
            source="City of Benton Harbor Brownfield Redevelopment Authority — no published "
                   "GIS polygon; obtain legal description / map and digitize before promoting.",
            provenance="stub", effective_date=None, multiplier_weight=0.0, buffer_m=None)
    print("  Housing TIF: stub row (no geometry; tracked only)")
    return 1


def main() -> None:
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            print("Ingesting Opportunity Zones (authoritative)...")
            oz = ingest_opportunity_zones(cur)
            print("Ingesting Brownfields + Ox Creek proxy (sourced_unconfirmed)...")
            bf = ingest_brownfields_and_oxcreek(cur)
            print("Ingesting Housing TIF stub...")
            tif = ingest_housing_tif_stub(cur)
        conn.commit()
    print(f"\nDone. OZ authoritative={oz}, brownfield={bf}, tif_stub={tif}.")
    if oz == 0:
        print("WARNING: no authoritative OZ overlays ingested — the calibration "
              "multiplier will be x1.0. Check network access to TIGERweb.", file=sys.stderr)


if __name__ == "__main__":
    main()

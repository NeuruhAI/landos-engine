"""
cross_flood_sfha.py — the flood_remap community-join resolve
(geometry_infra_spec Part 4, resolve half).

    python -m scripts.cross_flood_sfha --dry-run

The flood_remap adapter stages COMMUNITY-grain rows (NFIP Community Status
Book; match_method=community_macro) and defers the parcel cross to the engine.
This is that cross: SFHA zone polygons (landos.overlay_polygons, loaded by
load_nfhl_sfha) x parcel boundaries (landos.parcel_geom, ST_Intersects with a
bbox/GIST prefilter) -> the set of book-relevant parcels under a Special Flood
Hazard Area per county, stamped onto each flood_remap signal as an additive
raw_payload.sfha_cross provenance block.

Grain honesty: the cross runs at county/DFIRM grain — the FIRM adoption date a
community signal carries applies to the DFIRM panel set covering that county.
A finer CID-polygon join (NFHL layer 22 Political Jurisdictions) is the named
arm-time refinement; do not fake it from this stamp.

What this does NOT do (spec Part 4): no minted parcels, no new distress axis,
no matched_lead_id (a community touches thousands of parcels — never one lead),
no score movement (SFHA overlays are UNARMED at weight 0.0). Signals stay
status='new'; counties without geometry (Berrien 26021) are a named gate.
"""
import argparse
import json
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

OVERLAY_TYPE = "flood_sfha"

GEOM_COVERAGE = """
    select county_fips, count(*) as polys
    from landos.parcel_geom
    group by county_fips
"""

ZONES = """
    select id, name, geometry::text as geojson, minx, miny, maxx, maxy
    from landos.overlay_polygons
    where overlay_type = %(overlay_type)s
    order by name
"""

# One zone at a time. A county-spanning floodplain polygon is too heavy for a
# single ST_Intersects pass (statement-timeout territory), so the zone is
# subdivided server-side first: each piece carries a tight bbox, so g.geom &&
# rides the parcel GIST index and the confirm intersect stays small. Piece-wise
# intersection is equivalent (a parcel intersects the zone iff it intersects
# some piece); distinct collapses parcels spanning pieces.
ZONE_PARCELS = """
    with zone as (
        select (ST_Dump(ST_Subdivide(ST_MakeValid(
                   ST_SetSRID(ST_GeomFromGeoJSON(%(geojson)s), 4326)), 128))).geom as piece
    )
    select distinct g.parcel_apn
    from zone z
    join landos.parcel_geom g
      on g.county_fips = %(fips)s
     and g.geom && z.piece
     and ST_Intersects(g.geom, z.piece)
"""

SIGNALS = """
    select signal_id, source_ref, county_fips,
           raw_payload->>'community_name' as community_name,
           raw_payload->>'effective_map_iso' as effective_map_iso
    from landos.discovery_signals
    where source_type = 'flood_remap'
      and status = 'new'
    order by county_fips, source_ref
"""

STAMP = """
    update landos.discovery_signals
       set raw_payload = coalesce(raw_payload, '{}'::jsonb)
                         || jsonb_build_object('sfha_cross', %(cross)s::jsonb)
     where signal_id = %(signal_id)s
       and status = 'new'
"""


def county_of(zone_name: str) -> str:
    """'NFHL 18141C 18141C_123 zone AE' -> '18141' (DFIRM prefix contract
    pinned by load_nfhl_sfha.zone_name)."""
    parts = zone_name.split()
    if len(parts) < 2 or parts[0] != "NFHL" or len(parts[1]) < 6:
        return ""
    return parts[1][:5]


def main(dry_run: bool = False) -> dict:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(GEOM_COVERAGE)
        geom_counties = {r["county_fips"]: r["polys"] for r in cur.fetchall()}

        cur.execute(ZONES, {"overlay_type": OVERLAY_TYPE})
        zones = cur.fetchall()

        by_county: dict[str, set] = {}
        zone_counts: dict[str, int] = {}
        for i, z in enumerate(zones, 1):
            fips = county_of(z["name"])
            if not fips or not geom_counties.get(fips):
                continue
            cur.execute(ZONE_PARCELS, {"fips": fips, "geojson": z["geojson"]})
            apns = {r["parcel_apn"] for r in cur.fetchall()}
            by_county.setdefault(fips, set()).update(apns)
            zone_counts[fips] = zone_counts.get(fips, 0) + 1
            if i % 100 == 0:
                print(f"  ...{i}/{len(zones)} zones crossed")

        cur.execute(SIGNALS)
        signals = cur.fetchall()

        stamped = 0
        gated: dict[str, int] = {}
        resolved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for s in signals:
            fips = s["county_fips"]
            if fips not in by_county:
                gated[fips] = gated.get(fips, 0) + 1
                continue
            cross = json.dumps({
                "parcels_in_sfha": len(by_county[fips]),
                "sfha_zones": zone_counts.get(fips, 0),
                "grain": "county_dfirm",
                "method": "NFHL S_Fld_Haz_Ar x landos.parcel_geom ST_Intersects",
                "spec": "geometry_infra_spec Part 4 (area-heat; overlays UNARMED at weight 0.0)",
                "resolved_at": resolved_at,
            })
            stamped += 1
            print(f"  STAMP [{fips}] {s['community_name']!r} "
                  f"(FIRM {s['effective_map_iso'] or '?'}) -> "
                  f"parcels_in_sfha={len(by_county[fips])}")
            if dry_run:
                continue
            cur.execute(STAMP, {"signal_id": s["signal_id"], "cross": cross})
            if cur.rowcount != 1:
                raise RuntimeError(f"expected to stamp 1 row for signal {s['signal_id']}, got {cur.rowcount}")

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"\n=== SFHA CROSS {mode} ===")
    for fips in sorted(by_county):
        print(f"  [{fips}] parcels_in_sfha={len(by_county[fips])} "
              f"of {geom_counties.get(fips, 0)} "
              f"(zones={zone_counts.get(fips, 0)})")
    print(f"  signals_stamped={stamped}")
    for fips, n in sorted(gated.items()):
        print(f"  gated [{fips}]: {n} held — named gate, no parcel geometry/zones loaded")
    if dry_run:
        print("\nNO writes. Re-run without --dry-run to apply.")

    return {"dry_run": dry_run, "stamped": stamped, "gated": gated,
            "parcels_in_sfha": {k: len(v) for k, v in by_county.items()}}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="flood_remap community-join resolve")
    ap.add_argument("--dry-run", action="store_true", help="preview; write nothing")
    a = ap.parse_args()
    main(dry_run=a.dry_run)

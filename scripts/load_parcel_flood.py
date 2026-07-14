"""
load_parcel_flood.py — persist SFHA-crossed parcels into landos.parcel_flood (W0 JOB 4).

    python -m scripts.load_parcel_flood [--dry-run]

Crosses landos.parcel_geom against the loaded flood_sfha overlays (the same
ST_Subdivide-per-zone method cross_flood_sfha uses to survive the statement
timeout) and records each covered parcel + the FEMA zone(s) it intersects. This
is the FLOOD-lens candidate surface — reversing the earlier "attribute" call.
ON CONFLICT DO NOTHING; census before/after.
"""
import argparse

import psycopg

from app.config import DATABASE_URL

# Per-zone subdivide keeps each intersect small; zone name -> FLD_ZONE via the
# load_nfhl_sfha naming contract ("NFHL <dfirm> <fld_ar_id> zone <FLD_ZONE>").
POPULATE = """
    with zone as (
      select substr(o.name, 6, 5) as county_fips,
             nullif(split_part(o.name, 'zone ', 2), '') as fld_zone,
             (st_dump(st_subdivide(st_makevalid(
                st_setsrid(st_geomfromgeojson(o.geometry::text), 4326)), 128))).geom as piece
      from landos.overlay_polygons o
      where o.overlay_type = 'flood_sfha'
    ),
    hit as (
      select g.county_fips, g.parcel_apn,
             string_agg(distinct z.fld_zone, ',') as flood_zone
      from zone z
      join landos.parcel_geom g
        on g.county_fips = z.county_fips
       and g.geom && z.piece
       and st_intersects(g.geom, z.piece)
      group by g.county_fips, g.parcel_apn
    )
    insert into landos.parcel_flood (county_fips, parcel_apn, in_sfha, flood_zone, source)
    select county_fips, parcel_apn, true, flood_zone,
           'FEMA NFHL S_Fld_Haz_Ar x landos.parcel_geom (ST_Intersects)'
    from hit
    on conflict (county_fips, parcel_apn) do nothing
"""


def main(dry_run: bool = False):
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("set statement_timeout = '600s'")
        cur.execute("select count(*) from landos.parcel_flood")
        before = cur.fetchone()[0]
        print(f"[census] before: parcel_flood = {before}")
        if dry_run:
            # count what WOULD land without inserting
            cur.execute(f"with src as ({POPULATE.split('insert into')[0]} "
                        f"select county_fips, parcel_apn from hit) "
                        f"select count(*) from src")
            print(f"[dry-run] would upsert {cur.fetchone()[0]} SFHA parcels — no writes")
            conn.rollback()
            return
        cur.execute(POPULATE)
        conn.commit()
        cur.execute("select county_fips, count(*), count(flood_zone) from landos.parcel_flood group by 1 order by 1")
        rows = cur.fetchall()
        cur.execute("select count(*) from landos.parcel_flood")
        after = cur.fetchone()[0]
    for fips, n, nz in rows:
        print(f"[census] {fips}: {n} SFHA parcels ({nz} with a zone)")
    print(f"[census] after: parcel_flood = {after} (+{after - before})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    main(dry_run=a.dry_run)

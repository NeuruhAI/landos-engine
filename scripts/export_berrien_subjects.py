"""
export_berrien_subjects.py — pre-stage the 124 Berrien (FIPS 26021) subjects for CC3's
BatchData comps puller. $0 / NO API contact — a pure DB->CSV export.

Reads landos.owner_leads (the 124 qualified Berrien subjects, all with lat/lng + acreage +
land_class) and writes CC3's byte-exact template CSV to the GITIGNORED data-input path in the
landos-ingest repo. Writing that file is a data export, NOT a git op — it never touches CC3's
tracked tree. The radius search runs off situs_lat/lng (100% coverage); situs_city/zip stay
blank (no such columns on owner_leads) and are non-critical.

    python -m scripts.export_berrien_subjects
"""
import csv
import hashlib
import os

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
OUT = os.path.expanduser(
    "~/repos/landos-ingest/data/strategy/comps/subjects/berrien_subjects.csv")
HEADER = ["subject_parcel_number", "situs_address", "situs_city", "situs_zip",
          "situs_lat", "situs_lng", "acreage", "land_class"]

QUERY = """
    select parcel_apn                 as subject_parcel_number,
           coalesce(situs_address,'') as situs_address,
           ''                         as situs_city,   -- no situs_city column; blank, non-critical
           ''                         as situs_zip,    -- no situs_zip column; blank, non-critical
           situs_lat, situs_lng,
           acreage,
           land_use_class             as land_class
    from landos.owner_leads
    where tenant_id = %s
      and county_fips = '26021'
      and coalesce(discovery_seed, false) = false
      and acreage >= 0.01
    order by parcel_apn
"""


def main() -> None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = 30000")
            cur.execute(QUERY, (TENANT,))
            rows = cur.fetchall()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        for r in rows:
            w.writerow([r[c] for c in HEADER])

    geo = sum(1 for r in rows if r["situs_lat"] is not None and r["situs_lng"] is not None)
    sha = hashlib.sha256(open(OUT, "rb").read()).hexdigest()
    print(f"rows: {len(rows)} (expect 124)")
    print(f"lat/lng coverage: {geo}/{len(rows)}")
    print(f"output: {OUT}")
    print(f"sha256: {sha}")


if __name__ == "__main__":
    main()

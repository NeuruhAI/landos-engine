"""
export_entity_pierce_targets.py — stage the 81 entities for CC3's BatchData entity-pierce
puller. $0 / NO API — pure DB->CSV.

CC3's pierce puller reads data/strategy/pierce/targets/entity_pierce_targets.csv (already
.gitignore-registered on their side). This pre-stages it so the puller is armed the moment
BatchData is greenlit. ONE row per ENTITY LEAD — all 81, NOT collapsed (the pierce resolves
per parcel-owner, so each parcel is its own pierce attempt). mailing_* is the lookup anchor
(81/81 coverage).

    python -m scripts.export_entity_pierce_targets
"""
import csv
import hashlib
import os

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
OUT = os.path.expanduser(
    "~/repos/landos-ingest/data/strategy/pierce/targets/entity_pierce_targets.csv")
HEADER = ["entity_name", "mailing_address", "mailing_city", "mailing_state", "mailing_zip",
          "parcel_apn", "county_fips", "lead_id"]

QUERY = """
    select owner_name, owner_mailing_address, owner_mailing_city, owner_mailing_state,
           owner_mailing_zip, parcel_apn, county_fips, lead_id
    from landos.owner_leads
    where tenant_id = %s and owner_type = 'entity' and not coalesce(discovery_seed, false)
    order by owner_name, parcel_apn
"""


def main() -> None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = 60000")
            cur.execute(QUERY, (TENANT,))
            rows = cur.fetchall()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        for r in rows:
            w.writerow([r["owner_name"], r["owner_mailing_address"], r["owner_mailing_city"],
                        r["owner_mailing_state"], r["owner_mailing_zip"], r["parcel_apn"],
                        r["county_fips"], r["lead_id"]])

    mail_fill = sum(1 for r in rows if (r["owner_mailing_address"] or "").strip())
    sha = hashlib.sha256(open(OUT, "rb").read()).hexdigest()
    print(f"rows (entity leads): {len(rows)}")
    print(f"mailing_address fill: {mail_fill}/{len(rows)} "
          f"(city {sum(1 for r in rows if (r['owner_mailing_city'] or '').strip())}, "
          f"state {sum(1 for r in rows if (r['owner_mailing_state'] or '').strip())}, "
          f"zip {sum(1 for r in rows if (r['owner_mailing_zip'] or '').strip())})")
    print(f"unique entity names: {len({r['owner_name'] for r in rows})}")
    print(f"output: {OUT}")
    print(f"sha256: {sha}")


if __name__ == "__main__":
    main()

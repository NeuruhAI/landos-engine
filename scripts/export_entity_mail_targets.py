"""
export_entity_mail_targets.py — stage the 81 entities as a direct-mail merge file for CC3.
$0 / NO API — pure DB->CSV. Path A: the entity channel.

The 81 entities are 0-pierced (no resolved controlling human) but 100% mailable, so direct
mail to the entity at its mailing address is the ready-today outreach. ONE row per entity
LEAD (entities are ~1 parcel each; each parcel is its own mail piece) — NOT hub-collapsed.
Only TRUE duplicates collapse: parcels whose owner_name + full mailing block are byte-identical
become one mail piece carrying parcel_count (no sense mailing the same name+address twice).

    python -m scripts.export_entity_mail_targets
"""
import csv
import hashlib
import os

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
OUT = os.path.expanduser(
    "~/repos/landos-ingest/data/strategy/mail/targets/entity_mail_targets.csv")
HEADER = ["entity_name", "mailing_address", "mailing_city", "mailing_state", "mailing_zip",
          "situs_address", "county_fips", "parcel_apn", "parcel_count", "lead_id"]

# Collapse ONLY byte-identical (owner_name + full mailing) — same entity, same address,
# one mail piece. Distinct mailing => distinct row even if the name repeats.
QUERY = """
    with base as (
        select lead_id, owner_name, owner_mailing_address, owner_mailing_city,
               owner_mailing_state, owner_mailing_zip, situs_address, county_fips, parcel_apn
        from landos.owner_leads
        where tenant_id = %s and owner_type = 'entity' and not coalesce(discovery_seed, false)
    ),
    grp as (
        select *,
               row_number() over (partition by owner_name, owner_mailing_address, owner_mailing_city,
                                  owner_mailing_state, owner_mailing_zip order by parcel_apn) rn,
               count(*)     over (partition by owner_name, owner_mailing_address, owner_mailing_city,
                                  owner_mailing_state, owner_mailing_zip) parcel_count
        from base
    )
    select owner_name, owner_mailing_address, owner_mailing_city, owner_mailing_state,
           owner_mailing_zip, situs_address, county_fips, parcel_apn, parcel_count, lead_id
    from grp where rn = 1
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
                        r["owner_mailing_state"], r["owner_mailing_zip"], r["situs_address"],
                        r["county_fips"], r["parcel_apn"], r["parcel_count"], r["lead_id"]])

    mail_fill = sum(1 for r in rows if (r["owner_mailing_address"] or "").strip())
    sha = hashlib.sha256(open(OUT, "rb").read()).hexdigest()
    print(f"rows (mail pieces): {len(rows)}")
    print(f"parcels covered: {sum(r['parcel_count'] for r in rows)} (entities)")
    print(f"mailing_address fill: {mail_fill}/{len(rows)}")
    print(f"unique entity names: {len({r['owner_name'] for r in rows})}")
    print(f"output: {OUT}")
    print(f"sha256: {sha}")


if __name__ == "__main__":
    main()

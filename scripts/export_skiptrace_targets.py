"""
export_skiptrace_targets.py — pre-stage the unique INDIVIDUAL skip-trace targets for CC3's
Tracerfy fetcher. $0 / NO API contact — a pure DB->CSV export.

STEP 0 (verified live) settled the universe: the entity->human pierce is NOT materialized
(contact_relations=1 row, 0/81 entities resolved; the MI-registry pierce returned
no_registration_found / trust-no-registry). So the skip-trace file is the REAL PEOPLE only:
the resolved individual owners. Entities are mail-primary (100% mailable) and a future
BatchData re-pierce — they do NOT go here.

Universe: owner_type='individual', deduped to unique humans by normalized name (hub-collapse:
one person owning multiple parcels = ONE row, carry parcel_count). Two non-persons sitting in
the individual bucket are dropped (BENTON HARBOR HOPE VI, LAKELAND HOSPITALS) -> ~32 persons.
mailing_* is the absentee anchor (situs as fallback). target_key is a stable md5 of the
normalized name so re-runs are idempotent.

    python -m scripts.export_skiptrace_targets
"""
import csv
import hashlib
import os

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
OUT = os.path.expanduser(
    "~/repos/landos-ingest/data/strategy/skiptrace/targets/skiptrace_targets.csv")
HEADER = ["target_key", "full_name", "mailing_address", "mailing_city", "mailing_state",
          "mailing_zip", "situs_address", "county_fips", "source_kind", "parcel_count"]

# Non-persons misclassified into owner_type='individual' — dropped (not skip-traceable people).
DROP_PATTERNS = ["BENTON HARBOR HOPE%", "LAKELAND HOSPITAL%"]

QUERY = """
    with base as (
        select upper(btrim(owner_name))      as nkey,
               owner_name, owner_mailing_address, owner_mailing_city,
               owner_mailing_state, owner_mailing_zip, situs_address, county_fips, parcel_apn
        from landos.owner_leads
        where tenant_id = %s and owner_type = 'individual'
          and not coalesce(discovery_seed, false)
          and owner_name not ilike all(%s)
    ),
    ranked as (
        select *, row_number() over (partition by nkey order by parcel_apn) rn,
                  count(*)     over (partition by nkey) parcel_count
        from base
    )
    select nkey, owner_name, owner_mailing_address, owner_mailing_city, owner_mailing_state,
           owner_mailing_zip, situs_address, county_fips, parcel_count
    from ranked where rn = 1
    order by owner_name
"""


def target_key(nkey: str) -> str:
    return "IND_" + hashlib.md5(nkey.encode("utf-8")).hexdigest()[:12]


def main() -> None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = 60000")
            cur.execute(QUERY, (TENANT, DROP_PATTERNS))
            rows = cur.fetchall()

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(HEADER)
        for r in rows:
            w.writerow([target_key(r["nkey"]), r["owner_name"], r["owner_mailing_address"],
                        r["owner_mailing_city"], r["owner_mailing_state"], r["owner_mailing_zip"],
                        r["situs_address"], r["county_fips"], "individual", r["parcel_count"]])

    def fill(col):
        return sum(1 for r in rows if (r[col] is not None and str(r[col]).strip() != ""))
    sha = hashlib.sha256(open(OUT, "rb").read()).hexdigest()
    keys = {target_key(r["nkey"]) for r in rows}
    print(f"rows: {len(rows)} unique individuals (target_keys: {len(keys)} — {'unique' if len(keys)==len(rows) else 'COLLISION'})")
    print(f"full_name fill: {fill('owner_name')}/{len(rows)}")
    print(f"mailing_address fill: {fill('owner_mailing_address')}/{len(rows)} "
          f"(city {fill('owner_mailing_city')}, state {fill('owner_mailing_state')}, zip {fill('owner_mailing_zip')})")
    print(f"parcels covered: {sum(r['parcel_count'] for r in rows)} (hub-collapse from individual parcels)")
    print(f"output: {OUT}")
    print(f"sha256: {sha}")


if __name__ == "__main__":
    main()

"""
project_comps.py — project landos.comps -> public.lead_comps (engine single-writer).

Firewall-safe: reads landos (the engine's matched comps), writes ONLY public.lead_comps —
additive, same ownership as the score projection. For each matched comp it resolves the CRM
lead via subject_apn -> owner_leads.parcel_apn -> owner_leads.lead_id = acquisition_leads
.source_lead_id -> acquisition_leads.lead_id (the lead_comps FK target), and projects the
per-comp fields CC1's Comps panel reads. Idempotent: clears prior lead_comps rows for the
touched lead_ids first. SDF has no street address / DOM -> address = the comp parcel number,
dom/terrain_type/url = null.

    python -m scripts.project_comps            # dry-run
    python -m scripts.project_comps --apply     # write public.lead_comps

Scope: only the lit subjects (those with landos.comps rows). HARD INVARIANT (additive):
acquisition_leads.score_owner/score_distress/final_lead_score on the 136 are NOT touched.
"""
import argparse
import sys

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

PROJECT_SQL = """
    select al.lead_id,
           c.comp_apn      as address,        -- SDF has no street address; the parcel no. is the locator
           c.acreage       as acres,
           c.sale_price,
           case when c.acreage > 0 then round(c.sale_price / c.acreage, 2) end as price_per_acre,
           c.sale_date,
           c.source
    from landos.comps c
    join landos.owner_leads ol
      on ol.tenant_id = %s and ol.parcel_apn = c.subject_apn
         and coalesce(ol.discovery_seed, false) = false
    join public.acquisition_leads al
      on al.tenant_id = %s and al.source_lead_id = ol.lead_id
    order by al.lead_id
"""

INVARIANT_SQL = """
    with q as (select al.source_lead_id sid, al.score_owner so, al.score_distress sd,
                      al.final_lead_score fls
               from public.acquisition_leads al
               join landos.owner_leads ol on ol.lead_id = al.source_lead_id
               where ol.tenant_id = %s and not ol.discovery_seed)
    select md5(string_agg(concat_ws('|', sid::text, coalesce(so::text,'∅'), coalesce(sd::text,'∅'),
               coalesce(fls::text,'∅')), ';' order by sid)) h from q
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        if not args.apply:
            conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = 120000")
            cur.execute(PROJECT_SQL, (TENANT, TENANT))
            rows = cur.fetchall()
            lead_ids = sorted({r["lead_id"] for r in rows})
            print(f"projecting {len(rows)} comps across {len(lead_ids)} lit lead_id(s)")
            for r in rows[:3]:
                print(f"  sample: lead={str(r['lead_id'])[:8]} apn={r['address']} acres={r['acres']} "
                      f"price={r['sale_price']} ppa={r['price_per_acre']} date={r['sale_date']} src={r['source']}")
            if not args.apply:
                print("(dry-run — pass --apply to write public.lead_comps)")
                return

            cur.execute(INVARIANT_SQL, (TENANT,)); before = cur.fetchone()["h"]
            # idempotent: clear prior rows for the touched leads, then insert
            cur.execute("delete from public.lead_comps where tenant_id=%s and lead_id = any(%s)",
                        (TENANT, lead_ids))
            for r in rows:
                cur.execute("""insert into public.lead_comps
                    (tenant_id, lead_id, address, acres, sale_price, price_per_acre,
                     dom, sale_date, source, terrain_type, url)
                    values (%s,%s,%s,%s,%s,%s,null,%s,%s,null,null)""",
                    (TENANT, r["lead_id"], r["address"], r["acres"], r["sale_price"],
                     r["price_per_acre"], r["sale_date"], r["source"]))
            conn.commit()
            cur.execute(INVARIANT_SQL, (TENANT,)); after = cur.fetchone()["h"]
            cur.execute("""select count(*) n, count(distinct lead_id) leads from public.lead_comps
                           where tenant_id=%s""", (TENANT,))
            v = cur.fetchone()
            print(f"\nWROTE public.lead_comps: total={v['n']}  distinct lead_id={v['leads']}")
            print(f"HARD INVARIANT score_owner/distress/final_lead_score (136) unchanged: "
                  f"{before == after}  ({before} -> {after})")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"project_comps FAILED: {e.__class__.__name__}: {e}", file=sys.stderr)
        raise

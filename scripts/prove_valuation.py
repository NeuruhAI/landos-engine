"""
prove_valuation.py — JOB 5: the acceptance test that matters. Value 10 known parcels and
PRINT the comps + the math, so the operator can judge whether he'd put his own money behind
each number. No writes.

    python -m scripts.prove_valuation                 # 10 land valuations, full math
    python -m scripts.prove_valuation --federal        # + live NWI/3DEP buildable overlay
    python -m scripts.prove_valuation --mode house     # house MAO (honest-null until resid comps)

Selection: prefer leads that have BOTH comparable sales AND a loaded parcel polygon (a
grounded value), then fill with no-comp leads (to show the honest-null path). Every number
is traceable to its comps and its itemized adjustments — a valuation you cannot audit is a
valuation nobody can defend to a seller.
"""
import argparse

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
import app.valuation as V

TEN = V.TENANT

PICK_GROUNDED = """
    select a.lead_id, a.owner_name, a.parcel_apn, a.county_fips
      from public.acquisition_leads a
     where a.tenant_id = %s
       and exists (select 1 from landos.comps k where k.subject_apn = a.parcel_apn)
       and exists (select 1 from landos.parcel_geom g
                    where g.county_fips = a.county_fips and g.parcel_apn = a.parcel_apn)
     order by a.parcel_apn
     limit %s
"""
PICK_UNGROUNDED = """
    select a.lead_id, a.owner_name, a.parcel_apn, a.county_fips
      from public.acquisition_leads a
     where a.tenant_id = %s and a.parcel_apn is not null
       and not exists (select 1 from landos.comps k where k.subject_apn = a.parcel_apn)
     order by a.score_distress desc nulls last
     limit %s
"""


def _money(v):
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "—"


def render(out):
    print(f"\n{'─'*78}")
    print(f"lead {out['lead_id']}   {out.get('parcel_apn')}  ({out.get('county_fips')})   mode={out['mode']}")
    print(f"  VALUE: {_money(out['value'])}   RANGE: "
          f"[{_money(out['range'][0]) if out['range'] else '—'}, "
          f"{_money(out['range'][1]) if out['range'] else '—'}]   confidence={out['confidence']}")
    if out.get("reason"):
        print(f"  reason: {out['reason']}")
    if out['mode'] == "land":
        print(f"  base $/acre: {_money(out.get('base_ppa'))}   net factor: {out.get('net_factor')}")
    comps = out.get("comps", [])
    print(f"  COMPS ({len(comps)} used){' — grounded' if out.get('comps_grounded') else ' — NOT grounded'}:")
    for c in comps[:4]:
        if out['mode'] == "land":
            print(f"    {c.get('comp_apn')}  {c.get('sale_date')}  {_money(c.get('sale_price'))}  "
                  f"{c.get('acreage')}ac  {_money(c.get('price_per_acre'))}/ac")
        else:
            print(f"    {c.get('comp_apn')}  {c.get('sale_date')}  {_money(c.get('sale_price'))}  {c.get('property_type')}")
    if len(comps) > 4:
        print(f"    … +{len(comps)-4} more")
    print("  ADJUSTMENTS (each itemized with its input):")
    for a in out["adjustments"]:
        vmark = "" if a["verified"] else "  ⚠ UNVERIFIED"
        print(f"    - {a['name']:<16} ×{a['factor']}{vmark}   [{a['input']}]")
        print(f"        {a['rationale']}")
    print("  INPUTS USED:")
    for i in out["inputs_used"]:
        src = f"  ({i['source']})" if i.get("source") else ""
        print(f"    - {i['field']} = {i['value']}{src}")
    print(f"  method_version: {out['method_version']}   generated_at: {out['generated_at']}")


def main(mode="land", federal=False, n=10):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set statement_timeout = '120s'")
        cur.execute(PICK_GROUNDED, (TEN, n))
        picks = cur.fetchall()
        if len(picks) < n:
            cur.execute(PICK_UNGROUNDED, (TEN, n - len(picks)))
            picks += cur.fetchall()
        print(f"{'='*78}\nVALUATION LAB — PROOF ON {len(picks)} PARCELS  (mode={mode}, federal={federal})\n{'='*78}")
        grounded = 0
        for p in picks:
            out = V.value_for_lead(cur, str(p["lead_id"]), mode=mode, federal=federal)
            render(out)
            if out["value"] is not None:
                grounded += 1
        print(f"\n{'='*78}\nSUMMARY: {grounded}/{len(picks)} produced a grounded number; "
              f"{len(picks)-grounded} honest-null (no comps / no acreage / no ARV).\n{'='*78}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="land", choices=["land", "house"])
    ap.add_argument("--federal", action="store_true", help="live NWI/3DEP buildable overlay")
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()
    main(mode=a.mode, federal=a.federal, n=a.n)

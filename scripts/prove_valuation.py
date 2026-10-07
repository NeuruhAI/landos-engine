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


# Diverse subjects straight from the county SALES FABRIC (for counties with leads that carry
# no acreage, e.g. Cass tax-sale govt parcels). width_bucket spreads the picks across acreage
# ranges; each carries its OWN recent sale so the model value can be checked against reality.
PICK_POOL = """
    select distinct on (bucket) parcel_apn, acreage, class_code, sale_price, sale_date
      from (
        select parcel_apn, acreage, class_code, sale_price, sale_date,
               width_bucket(acreage, array[0.5, 2, 5, 20, 60]) as bucket
          from landos.parcel_sale
         where county_fips = %s and class_code = any(%s)
           and acreage is not null and acreage > 0 and sale_price > 0
           and sale_date >= date '2010-01-01'
      ) s
     order by bucket, sale_date desc
"""


def prove_pool(county_fips, n=5):
    """Value n real vacant parcels from the county SALE FABRIC through the SAME land engine the
    endpoint uses (value_land_for_lead), and print the model value beside each parcel's ACTUAL
    sale — the 'would you stake money' check."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("set statement_timeout = '120s'")
        cur.execute(PICK_POOL, (county_fips, sorted(V.VACANT_LAND_CLASSES)))
        picks = cur.fetchall()[:n]
        print(f"{'='*78}\nVALUATION LAB — LAND PROOF ON {len(picks)} REAL {county_fips} PARCELS "
              f"(sale fabric)\n{'='*78}")
        grounded = 0
        for p in picks:
            # synthetic subject = exactly what resolve_lead returns for a bare parcel; the engine
            # resolves acreage + class from landos.parcel_sale itself.
            lead = {"lead_id": f"pool:{p['parcel_apn']}", "county_fips": county_fips,
                    "parcel_apn": p["parcel_apn"], "parcel_meta": None, "score_signals": None,
                    "situs_lat": None, "situs_lng": None, "assessed_total": None, "roll_acres": None}
            out = V.value_land_for_lead(cur, lead)
            out["lead_id"], out["county_fips"], out["parcel_apn"] = lead["lead_id"], county_fips, p["parcel_apn"]
            out["generated_at"] = V.datetime.now(V.timezone.utc).isoformat()
            render(out)
            actual_ppa = float(p["sale_price"]) / float(p["acreage"]) if p["acreage"] else None
            print(f"  ► ACTUAL SALE (this parcel): {_money(float(p['sale_price']))} on {p['sale_date']} "
                  f"· {p['acreage']}ac · class {p['class_code']} · {_money(actual_ppa)}/ac")
            if out["value"] is not None and actual_ppa:
                print(f"    model base $/ac {_money(out.get('base_ppa'))} vs this sale {_money(actual_ppa)}/ac "
                      f"(model is a BAND median; a single sale varies around it)")
            if out["value"] is not None:
                grounded += 1
        print(f"\n{'='*78}\nSUMMARY: {grounded}/{len(picks)} grounded a land value from real comps; "
              f"{len(picks)-grounded} honest-null.\n{'='*78}")


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
    ap.add_argument("--pool-county", help="prove land value on real parcels from a county's "
                    "sale fabric (e.g. 26027 = Cass) — for counties whose leads carry no acreage")
    a = ap.parse_args()
    if a.pool_county:
        prove_pool(a.pool_county, n=a.n if a.n != 10 else 5)
    else:
        main(mode=a.mode, federal=a.federal, n=a.n)

"""
score_strategy_fit.py — compute + store the alt_use_fitness (developer-fit) axis.

ADDITIVE + SEPARATE: writes ONLY landos.scores.inputs_snapshot->'strategy_fit'
->'alt_use_fitness' (a jsonb merge on the latest score row per lead). It NEVER
touches owner_component / distress_component / arbitrage_component / score /
weights_version, nor public.acquisition_leads (no CRM projection — that's a
separate decision). Versioned on alt_use_fitness_v1; reversible by that key.

    python -m scripts.score_strategy_fit            # dry-run (distribution + samples)
    python -m scripts.score_strategy_fit --apply     # write the annotation

Inputs (engine-side, the 136): signal_overlap.in_opportunity_zone, signal_overlap.
distress_stack (brownfield/part201), situs_lat/lng -> nearest CC3 substation
(haversine). Substation coverage: prefer substations_michiana.geojson; else the
NW-Indiana feed with coverage_scope='nw_indiana_only' and grounded=false for any MI
parcel whose nearest point is across the state line (do NOT pretend coverage).
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from statistics import mean

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.strategy_fit import alt_use_fitness, ALT_USE_VERSION

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
STRAT = os.path.expanduser("~/repos/landos-ingest/data/strategy")
STATE_BY_FIPS = {"26021": "MI", "18141": "IN"}


def load_substations() -> tuple[list, str, str, str]:
    """Return (substations, source_file, manifest_sha, attribution). Prefer the
    michiana feed; else the NW-Indiana feed."""
    cand = [f"{STRAT}/substations/staged/substations_michiana.geojson",
            f"{STRAT}/substations/staged/substations_nw_indiana.geojson"]
    path = next((p for p in cand if os.path.exists(p)), None)
    if not path:
        return [], "(none found)", None, None
    with open(path) as f:
        gj = json.load(f)
    subs = []
    for ft in gj.get("features", []):
        geom = ft.get("geometry") or {}
        props = ft.get("properties") or {}
        if geom.get("type") == "Point" and geom.get("coordinates"):
            lng, lat = geom["coordinates"][0], geom["coordinates"][1]
        else:
            lat, lng = props.get("LATITUDE"), props.get("LONGITUDE")
        if lat is None or lng is None:
            continue
        subs.append({"lat": float(lat), "lng": float(lng),
                     "state": (props.get("STATE") or "").strip().upper() or None})
    sha = attribution = None
    man = f"{STRAT}/manifests/substations_hifld.json"
    if os.path.exists(man):
        with open(man) as f:
            m = json.load(f)
        sha = m.get("sha256")
        attribution = m.get("attribution_text")
    return subs, os.path.basename(path), sha, attribution


def fetch_leads(cur) -> list:
    cur.execute("""
        with latest as (
          select distinct on (s.identity_node_id) s.identity_node_id, s.id score_id,
                 s.owner_component, s.distress_component, s.arbitrage_component,
                 s.score, s.weights_version
          from landos.scores s where s.tenant_id=%s
          order by s.identity_node_id, s.computed_at desc)
        select ol.lead_id, ol.parcel_apn, ol.county_fips, ol.situs_lat, ol.situs_lng,
               ol.signal_overlap, l.score_id, l.weights_version
        from landos.owner_leads ol
        join landos.identity_nodes n on n.tenant_id=%s and n.parcel_apn=ol.parcel_apn
        join latest l on l.identity_node_id=n.id
        where ol.tenant_id=%s and not ol.discovery_seed
        order by ol.parcel_apn""", (TENANT, TENANT, TENANT))
    return cur.fetchall()


def lead_facts(r: dict) -> dict:
    so = r["signal_overlap"]
    if isinstance(so, str):
        try:
            so = json.loads(so)
        except ValueError:
            so = {}
    so = so if isinstance(so, dict) else {}
    stack = so.get("distress_stack") or so.get("signals") or []
    return {
        "distress_tokens": stack,
        "distress_present": "distress_stack" in so or "signals" in so,
        "in_opportunity_zone": so.get("in_opportunity_zone"),
        "oz_present": "in_opportunity_zone" in so,
        "situs_lat": r["situs_lat"], "situs_lng": r["situs_lng"],
        "parcel_state": STATE_BY_FIPS.get(r["county_fips"]),
    }


def component_hash(cur) -> str:
    """Hash of the composite component columns + weights_version over the latest score
    per node — proves --apply leaves them untouched."""
    cur.execute("""
        with latest as (select distinct on (identity_node_id) owner_component,
                          distress_component, arbitrage_component, score, weights_version
                        from landos.scores where tenant_id=%s
                        order by identity_node_id, computed_at desc)
        select md5(string_agg(concat_ws('|', coalesce(owner_component::text,'∅'),
                 coalesce(distress_component::text,'∅'), coalesce(arbitrage_component::text,'∅'),
                 coalesce(score::text,'∅'), weights_version::text), ';' order by score)) h
        from latest""", (TENANT,))
    return cur.fetchone()["h"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the annotation (default: dry-run)")
    args = ap.parse_args()
    as_of = datetime.now(timezone.utc).isoformat()
    subs, src, sha, attribution = load_substations()
    print(f"substations: {len(subs)} points from {src}  manifest_sha={sha}")
    print(f"attribution: {attribution}")
    if not subs:
        print("WARN: no substation feed found — substation_proximity will be ungrounded for all.")

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        if not args.apply:
            conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = 120000")
            leads = fetch_leads(cur)
            before_h = component_hash(cur)
            results = []
            for r in leads:
                ann = alt_use_fitness(lead_facts(r), subs, sha, as_of)
                results.append({"r": r, "ann": ann})

            if args.apply:
                for x in results:
                    cur.execute("""
                        update landos.scores
                        set inputs_snapshot = inputs_snapshot || jsonb_build_object(
                            'strategy_fit',
                            coalesce(inputs_snapshot->'strategy_fit','{}'::jsonb)
                              || jsonb_build_object('alt_use_fitness', %s))
                        where id = %s""", (Jsonb(x["ann"]), x["r"]["score_id"]))
                conn.commit()
                after_h = component_hash(cur)
                print(f"\nWROTE alt_use_fitness on {len(results)} score rows.")
                print(f"composite components+weights_version hash: before={before_h} after={after_h} "
                      f"-> UNCHANGED={before_h == after_h}")
                # independent read-back of 5
                ids = [x["r"]["score_id"] for x in results[:5]]
                cur.execute("""select id, weights_version,
                                 inputs_snapshot->'strategy_fit'->'alt_use_fitness'->>'version' v,
                                 (inputs_snapshot->'strategy_fit'->'alt_use_fitness'->>'score')::numeric sc
                               from landos.scores where id = any(%s)""", (ids,))
                print("read-back (5):")
                for rb in cur.fetchall():
                    print(f"  score_id={str(rb['id'])[:8]} alt_use_version={rb['v']} score={rb['sc']} wv={rb['weights_version']}")
                # CRM firewall: strategy_fit NOT projected
                cur.execute("""select count(*) n from information_schema.columns
                               where table_schema='public' and table_name='acquisition_leads'
                                 and column_name like '%alt_use%'""")
                print(f"CRM acquisition_leads alt_use_* columns: {cur.fetchone()['n']} (0 = not projected, correct)")

    report(results, src)


def report(results, src) -> None:
    n = len(results)
    scores = [x["ann"]["score"] for x in results]
    bands = {}
    grounded3 = sub_ungrounded = 0
    for x in results:
        inp = x["ann"]["inputs"]
        b = inp["substation_proximity"]["band"] if inp["substation_proximity"]["grounded"] else "ungrounded"
        bands[b] = bands.get(b, 0) + 1
        g = [inp["contamination"]["grounded"], inp["opportunity_zone"]["grounded"],
             inp["substation_proximity"]["grounded"]]
        if all(g):
            grounded3 += 1
        if not inp["substation_proximity"]["grounded"]:
            sub_ungrounded += 1
    print(f"\n{'='*72}\nalt_use_fitness DRY-RUN distribution (n={n}, feed={src})\n{'='*72}")
    print(f"  score: min={min(scores)} max={max(scores)} avg={round(mean(scores),1)}")
    print(f"  grounded on all 3 inputs: {grounded3}/{n}   substation-ungrounded: {sub_ungrounded}/{n}")
    print(f"  substation band counts: " + ", ".join(f"{k}={v}" for k, v in sorted(bands.items())))
    oz = sum(1 for x in results if x["ann"]["inputs"]["opportunity_zone"].get("value") is True)
    contam = sum(1 for x in results if x["ann"]["inputs"]["contamination"].get("value") is True)
    print(f"  OZ true: {oz}   contamination true: {contam}   double-stack (both): "
          f"{sum(1 for x in results if x['ann']['inputs']['opportunity_zone'].get('value') and x['ann']['inputs']['contamination'].get('value'))}")
    print("\n  5 sample parcels (parcel | contam | OZ | substation | score):")
    for x in results[:5]:
        i = x["ann"]["inputs"]
        c = f"{i['contamination']['value']}/{i['contamination']['kind']}"
        sub = i["substation_proximity"]
        subs = (f"{sub['nearest_mi']}mi {sub['band']} g={sub['grounded']}"
                if sub['nearest_mi'] is not None else "n/a")
        print(f"    {x['r']['parcel_apn']:<26} contam={c:<18} OZ={i['opportunity_zone']['value']}  "
              f"sub=[{subs}]  score={x['ann']['score']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"strategy_fit FAILED: {e.__class__.__name__}: {e}", file=sys.stderr)
        raise

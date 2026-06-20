"""
score_comps.py — COMP-MATCHER v1 runner: ground arbitrage for the 12 St. Joseph leads.

Loads the SDF land-comp pool (sha-verified), matches each of the 12 subjects
(app.comp_match), writes the matched comps to landos.comps, sets
landos.scores.arbitrage_component + inputs_snapshot.arbitrage, and RECOMPUTES the
composite (score + top_factors) for ONLY those 12 identity_nodes — folding arbitrage
in WITHOUT recomputing owner/distress (those stay byte-identical). The other 124
leads are never touched. Conservative: insufficient comps -> arbitrage NULL, never 0.

    python -m scripts.score_comps            # dry-run (the lit-vs-NULL split)
    python -m scripts.score_comps --apply     # write comps + arbitrage + recompute + kin cleanup

Idempotent: clears prior landos.comps rows for the 12 subjects before re-insert; the
score recompute is deterministic. Reversible by the comp source/provenance + re-NULL.
"""
import argparse
import csv
import hashlib
import json
import os
import sys
from datetime import date

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.comp_match import match_subject, is_arms_length, METHOD
from app.scoring import DEFAULT_WEIGHTS

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
COMP_FILE = os.path.expanduser(
    "~/repos/landos-ingest/data/strategy/comps/staged/sdf_land_comps_indiana_2024.csv")
SHA_PREFIX = "b050b2ea"
RECENCY_CUT = "2023-06"   # ~24mo before now (the file is all 2024, so this rarely binds)


def load_pool() -> tuple[list, str]:
    with open(COMP_FILE, "rb") as f:
        sha = hashlib.sha256(f.read()).hexdigest()
    if not sha.startswith(SHA_PREFIX):
        raise SystemExit(f"comp_set_sha mismatch: {sha[:16]} (expected {SHA_PREFIX}...)")
    with open(COMP_FILE) as f:
        rows = [r for r in csv.DictReader(f) if r.get("county_fips") == "18141"]
    return rows, sha


def active_weights(cur) -> dict:
    cur.execute("select weights from landos.scoring_weights where tenant_id=%s and "
                "profile='default' and active=true limit 1", (TENANT,))
    row = cur.fetchone()
    if row:
        w = row["weights"]
        return json.loads(w) if isinstance(w, str) else w
    return DEFAULT_WEIGHTS


def fetch_subjects(cur) -> list:
    cur.execute("""
        with latest as (select distinct on (s.identity_node_id) s.id, s.identity_node_id,
                          s.owner_component, s.distress_component, s.score, s.top_factors,
                          s.weights_version, s.inputs_snapshot
                        from landos.scores s where s.tenant_id=%s
                        order by s.identity_node_id, s.computed_at desc)
        select ol.parcel_apn, ol.acreage, ol.raw_meta->>'mkt_val_land' av_land,
               l.id score_id, l.owner_component, l.distress_component, l.score, l.top_factors,
               l.weights_version, l.inputs_snapshot
        from landos.owner_leads ol
        join landos.identity_nodes n on n.tenant_id=%s and n.parcel_apn=ol.parcel_apn
        join latest l on l.identity_node_id=n.id
        where ol.tenant_id=%s and ol.county_fips='18141'
          and coalesce(ol.discovery_seed,false)=false
        order by ol.parcel_apn""", (TENANT, TENANT, TENANT))
    return cur.fetchall()


def reblend(owner_c, distress_c, arb_c, weights, top_factors):
    cw = weights["composite"]
    o, d = float(owner_c or 0), float(distress_c or 0)
    a = float(arb_c) if arb_c is not None else 0.0
    score = round(max(0.0, min(100.0, o * cw["owner"] + d * cw["distress"]
                               + a * cw["arbitrage"])), 2)
    factors = [f for f in (top_factors or []) if "assessed land" not in (f.get("factor") or "")]
    if arb_c and arb_c > 0:
        factors.append({"factor": f"{round(arb_c)}% over assessed land (comp-grounded)",
                        "points": round(a * cw["arbitrage"], 2)})
    factors.sort(key=lambda f: -(f.get("points") or 0))
    return score, factors[:3]


def composite_hashes(cur):
    """(owner/distress over 136, full-score over the 124 non-StJoseph, final_lead_score over 136)."""
    cur.execute("""
      with latest as (select distinct on (s.identity_node_id) s.identity_node_id, s.owner_component oc,
                        s.distress_component dc, s.arbitrage_component ac, s.score sc, s.weights_version wv,
                        s.top_factors::text tf
                      from landos.scores s where s.tenant_id=%s order by s.identity_node_id, s.computed_at desc),
      j as (select ol.county_fips cf, ol.parcel_apn apn, l.*
            from landos.owner_leads ol
            join landos.identity_nodes n on n.tenant_id=%s and n.parcel_apn=ol.parcel_apn
            join latest l on l.identity_node_id=n.id
            where ol.tenant_id=%s and not ol.discovery_seed)
      select md5(string_agg(concat_ws('|',apn,coalesce(oc::text,'∅'),coalesce(dc::text,'∅')),';' order by apn)) od_hash,
             md5(string_agg(concat_ws('|',apn,coalesce(oc::text,'∅'),coalesce(dc::text,'∅'),coalesce(ac::text,'∅'),
                  coalesce(sc::text,'∅'),wv::text,coalesce(tf,'∅')),';' order by apn)
                 filter (where cf<>'18141')) noStJoseph_hash
      from j""", (TENANT, TENANT, TENANT))
    r = cur.fetchone()
    cur.execute("""select md5(string_agg(concat_ws('|',parcel_apn,coalesce(final_lead_score::text,'∅')),';'
                        order by parcel_apn)) h from landos.owner_leads
                   where tenant_id=%s and not discovery_seed""", (TENANT,))
    return r["od_hash"], r["nostjoseph_hash"], cur.fetchone()["h"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    pool, sha = load_pool()
    arm_n = sum(1 for c in pool if is_arms_length(c))
    print(f"comp pool: {len(pool)} St-Joseph rows ({arm_n} arm's-length) | sha={sha[:16]} OK")

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        if not args.apply:
            conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("set statement_timeout=120000")
            weights = active_weights(cur)
            subjects = fetch_subjects(cur)
            results = []
            for s in subjects:
                m = match_subject({"parcel_apn": s["parcel_apn"], "acreage": s["acreage"],
                                   "av_land": s["av_land"]}, pool, RECENCY_CUT)
                new_score, new_tf = reblend(s["owner_component"], s["distress_component"],
                                            m["arbitrage_component"], weights, s["top_factors"])
                results.append({"s": s, "m": m, "new_score": new_score, "new_tf": new_tf})

            if args.apply:
                od0, nostj0, fls0 = composite_hashes(cur)
                apns = [r["s"]["parcel_apn"] for r in results]
                cur.execute("delete from landos.comps where tenant_id=%s and subject_apn = any(%s)",
                            (TENANT, apns))
                ncomps = 0
                for r in results:
                    for c in r["m"]["matched"]:
                        cur.execute("""insert into landos.comps
                            (tenant_id, subject_apn, comp_apn, sale_date, sale_price, acreage,
                             source, provenance) values (%s,%s,%s,%s,%s,%s,'dlgf_sdf_2024','county_sdf')""",
                            (TENANT, r["s"]["parcel_apn"], c["comp_apn"], c["sale_date"],
                             c["sale_price"], c["acreage"]))
                        ncomps += 1
                    snap = r["s"]["inputs_snapshot"] or {}
                    snap["arbitrage"] = {k: r["m"][k] for k in
                                         ("fmv", "median_ppa", "comps_n", "spread", "band_used",
                                          "date_range", "method", "status", "basis", "basis_source")}
                    snap["arbitrage"]["comp_set_sha"] = sha
                    if isinstance(snap.get("coverage"), dict):
                        snap["coverage"]["arbitrage"] = {
                            "status": ("grounded" if r["m"]["arbitrage_component"] is not None else "absent"),
                            "method": METHOD, "comps_n": r["m"]["comps_n"], "fmv": r["m"]["fmv"],
                            "band_used": r["m"]["band_used"]}
                    cur.execute("""update landos.scores set arbitrage_component=%s, score=%s,
                                     top_factors=%s, inputs_snapshot=%s where id=%s""",
                                (r["m"]["arbitrage_component"], r["new_score"], Jsonb(r["new_tf"]),
                                 Jsonb(snap), r["s"]["score_id"]))
                # kin cleanup
                cur.execute("delete from landos.contact_relations where relative_name like 'TEST%' returning relative_name")
                deleted_kin = [x["relative_name"] for x in cur.fetchall()]
                cur.execute("""insert into landos.comps_import_log
                    (tenant_id, source_file, row_count, valid_count, error_count, status, notes)
                    values (%s,%s,%s,%s,0,'ok',%s)""",
                    (TENANT, "dlgf_sdf_2024:18141", len(pool), ncomps,
                     f"comp_match_v1 matched_comps={ncomps} sha={sha[:16]}"))
                conn.commit()
                od1, nostj1, fls1 = composite_hashes(cur)
                print(f"\nWROTE {ncomps} comps; recomputed {len(results)} StJoseph scores; "
                      f"deleted kin: {deleted_kin}")
                print(f"INVARIANT owner/distress (136) unchanged: {od0==od1}")
                print(f"INVARIANT 124 non-StJoseph scores unchanged: {nostj0==nostj1}")
                print(f"INVARIANT final_lead_score (136) unchanged: {fls0==fls1}")

    report(results, sha)


def report(results, sha):
    lit = [r for r in results if r["m"]["arbitrage_component"] is not None]
    nul = [r for r in results if r["m"]["arbitrage_component"] is None]
    print(f"\n{'='*78}\nCOMP-MATCH v1 — {len(lit)} lit / {len(nul)} NULL of 12  (comp_set_sha={sha[:16]})\n{'='*78}")
    print(f"{'parcel_apn':<26} {'ac':>5} {'comps':>5} {'band':>7} {'FMV':>10} {'arb':>6} {'score':>12}")
    for r in results:
        m, s = r["m"], r["s"]
        arb = m["arbitrage_component"]
        print(f"{s['parcel_apn']:<26} {s['acreage']:>5} {m['comps_n']:>5} {str(m['band_used'] or '-'):>7} "
              f"{('—' if m['fmv'] is None else format(m['fmv'],',.0f')):>10} "
              f"{('NULL' if arb is None else format(arb,'.0f')):>6} "
              f"{format(float(s['score']),'.2f')+' -> '+format(r['new_score'],'.2f'):>12}")
    if lit:
        dr = [m for m in (r["m"] for r in lit) if m["date_range"]]
        if dr:
            lo = min(d["date_range"][0] for d in dr); hi = max(d["date_range"][1] for d in dr)
            print(f"\ndate range used: {lo} .. {hi}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"score_comps FAILED: {e.__class__.__name__}: {e}", file=sys.stderr)
        raise

"""
score_composite_batch.py — the BATCH path for the composite scorer (the piece
that was missing: score_parcel was API-only via /evaluate).

For each of the 136 production leads (landos.owner_leads, tenant, NOT
discovery_seed) it:
  1. assembles the parcel's REAL facts into score_parcel's input shape, tracking
     grounded-vs-MISSING per axis (app.composite_inputs.assemble — the HARD RULE
     lives there: MISSING is first-class, never 0);
  2. runs the SAME composite the API runs (app.scoring.score_parcel) with the
     SAME active weights _active_weights would load;
  3. writes landos.scores for the parcel's identity_node — score (a conservative
     FLOOR over modeled-grounded axes), per-axis component (NULL where the axis
     is absent or grounded-but-unmodeled — NOT 0), top_factors, weights_version,
     inputs_snapshot (full coverage: which inputs were present vs MISSING),
     computed_at.

SAFE BY DEFAULT: dry-run (computes + prints the coverage table, writes nothing).
Pass --apply to write. All writes are landos.* only (firewall); the CRM
projection (acquisition_leads.score_*) is a SEPARATE, later step — not here.

Idempotent: every batch row is tagged inputs_snapshot.batch_tag = BATCH_TAG;
--apply deletes this batch's prior rows for each node before re-inserting, so
re-running converges (and the whole batch is reversible by that tag).

    python -m scripts.score_composite_batch            # dry-run (default)
    python -m scripts.score_composite_batch --apply     # write to landos.scores
    python -m scripts.score_composite_batch --limit 5   # sample
"""
import argparse
import hashlib
import json
import re
import sys
from datetime import date
from statistics import median

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.scoring import score_parcel, DEFAULT_WEIGHTS
from app.composite_inputs import assemble
from app.comps import compute_arv

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
BATCH_TAG = "composite_136_v1"
TRACED = ("traced_web", "traced_paid")


def active_weights(cur) -> tuple[int, dict]:
    """Mirror app.main._active_weights for the default profile."""
    cur.execute(
        "select version, weights from landos.scoring_weights "
        "where tenant_id=%s and profile='default' and active=true limit 1",
        (TENANT,))
    row = cur.fetchone()
    if row:
        w = row["weights"]
        if isinstance(w, str):
            w = json.loads(w)
        return row["version"], w
    return 0, DEFAULT_WEIGHTS


def owner_hash_for(owner_name: str | None, lead_id) -> str:
    """Deterministic, documented owner_hash. No node exists for any of the 136
    today (audit: 0/136), so this scheme defines them; it is stable across
    re-runs and derived from the resolvable owner identity. Falls back to the
    lead's own id when the owner name is blank."""
    norm = re.sub(r"[^A-Z0-9]+", " ", (owner_name or "").upper()).strip()
    if norm:
        return "olname:" + hashlib.sha256(norm.encode()).hexdigest()[:24]
    return f"ol:{lead_id}"


def distress_tokens(signal_overlap, discovery_types) -> list[str]:
    """Union of owner_leads.signal_overlap (distress_stack ▸ signals) and any
    discovery_signals source_types matched to the lead."""
    toks: list[str] = []
    so = signal_overlap or {}
    if isinstance(so, str):
        try:
            so = json.loads(so)
        except ValueError:
            so = {}
    if isinstance(so, dict):
        stack = so.get("distress_stack") or so.get("signals") or []
        toks.extend(str(t) for t in stack if t)
    toks.extend(str(t) for t in (discovery_types or []) if t)
    # de-dupe, preserve order
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t); out.append(t)
    return out


def market_ppa_from_comps(comps_rows, acreage) -> tuple[float | None, int]:
    """Per-acre market value from comps via app.comps.compute_arv. Returns
    (market_price_per_acre | None, usable_comp_count). Dormant until comps land."""
    if not comps_rows:
        return None, 0
    arv = compute_arv(acreage, comps_rows, date.today())
    if arv.get("basis") == "per_acre" and arv.get("weighted_unit"):
        return float(arv["weighted_unit"]), int(arv.get("n") or 0)
    return None, int(arv.get("n") or 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write to landos.scores (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="limit leads (testing)")
    args = ap.parse_args()
    mode = "APPLY (writing landos.scores)" if args.apply else "DRY-RUN (no writes)"

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        if not args.apply:
            conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("set statement_timeout = 120000")
            version, weights = active_weights(cur)

            cur.execute(
                """select lead_id, parcel_apn, county_fips, owner_name, owner_type,
                          absentee_tier, absentee_score, skip_trace_status,
                          skip_trace_meta, signal_overlap, acreage
                   from landos.owner_leads
                   where tenant_id=%s and not discovery_seed
                   order by parcel_apn""", (TENANT,))
            leads = cur.fetchall()
            if args.limit:
                leads = leads[:args.limit]
            lead_ids = [r["lead_id"] for r in leads]
            apns = [r["parcel_apn"] for r in leads]

            # discovery_signals matched to these leads
            cur.execute(
                """select matched_lead_id, array_agg(distinct source_type) types
                   from landos.discovery_signals
                   where matched_lead_id = any(%s) group by 1""", (lead_ids,))
            disc = {r["matched_lead_id"]: r["types"] for r in cur.fetchall()}

            # comps keyed by subject_apn (empty today; wired for when they land)
            cur.execute(
                """select subject_apn, sale_date, sale_price, acreage, distance_mi
                   from landos.comps where subject_apn = any(%s)""", (apns,))
            comps_by_apn: dict = {}
            for r in cur.fetchall():
                comps_by_apn.setdefault(r["subject_apn"], []).append(r)

            print(f"\n{'='*72}\nBATCH composite scorer — {mode}\n{'='*72}")
            print(f"tenant={TENANT}  leads={len(leads)}  weights_version={version}  "
                  f"batch_tag={BATCH_TAG}")

            # ---- score every lead ------------------------------------------
            results = []
            for r in leads:
                stm = r["skip_trace_meta"] or {}
                if isinstance(stm, str):
                    try:
                        stm = json.loads(stm)
                    except ValueError:
                        stm = {}
                deceased = stm.get("deceased") if isinstance(stm, dict) else None
                mppa, comps_n = market_ppa_from_comps(
                    comps_by_apn.get(r["parcel_apn"]), r["acreage"])
                lead = {
                    "absentee_tier": r["absentee_tier"],
                    "owner_type": r["owner_type"],
                    "skip_traced": r["skip_trace_status"] in TRACED,
                    "is_deceased": (True if deceased is True else
                                    False if deceased is False else None),
                    "distress_tokens": distress_tokens(r["signal_overlap"], disc.get(r["lead_id"])),
                    "acreage": r["acreage"],
                    "market_ppa": mppa,
                    "list_ppa": None,           # no listing-price source for these leads
                    "comps_n": comps_n,
                }
                a = assemble(lead, weights)
                res = score_parcel(a["inputs"], weights)
                nc = a["null_components"]
                comp = {
                    "distress": None if nc["distress"] else res["distress_component"],
                    "owner": None if nc["owner"] else res["owner_component"],
                    "arbitrage": None if nc["arbitrage"] else res["arbitrage_component"],
                }
                snapshot = {
                    "batch_tag": BATCH_TAG,
                    "source": "scripts.score_composite_batch",
                    "lead_id": str(r["lead_id"]),
                    "parcel_apn": r["parcel_apn"],
                    "county_fips": r["county_fips"],
                    "score_basis": ("conservative floor over modeled-grounded axes; "
                                    "NULL components contributed 0 to the floor; "
                                    "partial composite, NOT renormalized"),
                    "confidence": a["confidence"],
                    "coverage": a["coverage"],
                    "modeled_inputs": a["inputs"],
                    "weights_version": version,
                }
                results.append({
                    "lead": r, "score": res["score"], "comp": comp,
                    "top_factors": res["top_factors"], "snapshot": snapshot,
                    "confidence": a["confidence"], "coverage": a["coverage"],
                    "owner_hash": owner_hash_for(r["owner_name"], r["lead_id"]),
                })

            # ---- delta source: prior batch rows + CRM untouched check ------
            stored = {}
            cur.execute(
                """select inputs_snapshot->>'lead_id' lid, score s, distress_component dc,
                          inputs_snapshot->'coverage'->'distress'->>'status' ds,
                          weights_version wv
                   from landos.scores where inputs_snapshot->>'batch_tag'=%s""",
                (BATCH_TAG,))
            for sr in cur.fetchall():
                stored[sr["lid"]] = sr
            for x in results:
                x["old"] = stored.get(str(x["lead"]["lead_id"]))
            cur.execute(
                """select count(*) filter (where score_owner is not null
                     or score_distress is not null or score_arbitrage is not null
                     or score_signals is not null) n from public.acquisition_leads""")
            crm_nonnull = cur.fetchone()["n"]

            # ---- WRITE (apply only) ----------------------------------------
            if args.apply:
                wrote = 0
                for x in results:
                    r = x["lead"]
                    cur.execute(
                        """insert into landos.identity_nodes
                             (tenant_id, parcel_apn, owner_hash, county, state)
                           values (%s,%s,%s,%s,%s)
                           on conflict (tenant_id, parcel_apn, owner_hash)
                             do update set updated_at = now()
                           returning id""",
                        (TENANT, r["parcel_apn"], x["owner_hash"], r["county_fips"], None))
                    node_id = cur.fetchone()["id"]

                    # idempotency: clear this batch's prior row for the node
                    cur.execute(
                        "delete from landos.scores where identity_node_id=%s "
                        "and inputs_snapshot->>'batch_tag'=%s", (node_id, BATCH_TAG))

                    cur.execute(
                        """insert into landos.scores
                             (tenant_id, identity_node_id, score, distress_component,
                              owner_component, arbitrage_component, top_factors,
                              weights_version, inputs_snapshot)
                           values (%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                        (TENANT, node_id, x["score"], x["comp"]["distress"],
                         x["comp"]["owner"], x["comp"]["arbitrage"],
                         Jsonb(x["top_factors"]), version, Jsonb(x["snapshot"])))
                    score_id = cur.fetchone()["id"]

                    cur.execute(
                        """insert into landos.event_ledger
                             (tenant_id, identity_node_id, channel, actor, action,
                              payload, idempotency_key)
                           values (%s,%s,'system','batch-composite','scored',%s,%s)
                           on conflict (tenant_id, idempotency_key) do nothing""",
                        (TENANT, node_id,
                         Jsonb({"score": x["score"], "batch_tag": BATCH_TAG,
                                "confidence": x["confidence"]}),
                         f"batch-score:{BATCH_TAG}:{node_id}"))
                    cur.execute(
                        """insert into landos.audit_log
                             (tenant_id, actor, operation, target_table, target_id, metadata)
                           values (%s,'batch-composite','batch_score_composite','scores',%s,%s)""",
                        (TENANT, score_id,
                         Jsonb({"parcel_apn": r["parcel_apn"], "batch_tag": BATCH_TAG,
                                "confidence": x["confidence"]})))
                    wrote += 1
                conn.commit()
                print(f"\nWROTE {wrote} score rows to landos.scores "
                      f"(+ event_ledger, audit_log). committed.")

    # ---- COVERAGE REPORT + DELTA/GATES (always) -------------------------------
    report(results, version)
    gates_pass = delta_gates(results, version, crm_nonnull)
    if not args.apply:
        print("\n" + ("ALL GATES PASS — safe to --apply."
                      if gates_pass else "GATES FAILED — do NOT apply."))


def _num_eq(a, b, tol=0.01):
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= tol


def _axis_counts(results, axis):
    c = {"grounded": 0, "grounded_unmodeled": 0, "absent": 0}
    for x in results:
        c[x["coverage"][axis]["status"]] += 1
    return c


def report(results, version) -> None:
    n = len(results)
    print(f"\n{'='*72}\nCOVERAGE — grounded vs flagged per axis (n={n}, weights v{version})\n{'='*72}")
    for axis in ("owner", "distress", "arbitrage"):
        c = _axis_counts(results, axis)
        nonnull = sum(1 for x in results if x["comp"][axis] is not None)
        print(f"  {axis:<10} grounded={c['grounded']:>3}  "
              f"grounded_unmodeled={c['grounded_unmodeled']:>3}  absent={c['absent']:>3}   "
              f"→ component stored (non-NULL): {nonnull}/{n}")

    scores = sorted(x["score"] for x in results)
    confs = sorted(x["confidence"] for x in results)
    real = sum(1 for x in results if x["score"] and x["score"] > 0)
    print(f"\n  composite score:  min={scores[0]}  median={median(scores)}  max={scores[-1]}  "
          f"  >0: {real}/{n}")
    print(f"  confidence:       min={confs[0]}  median={median(confs)}  max={confs[-1]}")

    # the exact remaining input gap (Bryan's data target)
    arb_absent = _axis_counts(results, "arbitrage")["absent"]
    dist_unmod = _axis_counts(results, "distress")["grounded_unmodeled"]
    print(f"\n  REMAINING INPUT GAP (Bryan's data target):")
    print(f"    • arbitrage: {arb_absent}/{n} need comps (market $/acre) + listing price "
          f"— landos.comps is empty")
    print(f"    • distress:  {dist_unmod}/{n} carry a grounded signal the model has NO slot for "
          f"(environmental_part201 / brownfield) → add a model key or remap")
    print(f"    • owner:     {n}/{n} missing age/DOB (age_band unscorable); "
          f"succession known only where skip-traced")

    # top 8 by score, with confidence + a coverage glyph per axis
    print(f"\n  TOP 8 by floor score (g=grounded · u=unmodeled · a=absent):")
    g = {"grounded": "g", "grounded_unmodeled": "u", "absent": "a"}
    for x in sorted(results, key=lambda x: -x["score"])[:8]:
        cov = "".join(g[x["coverage"][ax]["status"]] for ax in ("owner", "distress", "arbitrage"))
        facts = ", ".join(f["factor"] for f in x["top_factors"]) or "—"
        print(f"    {x['score']:>5}  conf={x['confidence']:.2f}  o/d/a={cov}  "
              f"{x['lead']['parcel_apn']:<16} [{facts}]")
    print()


def delta_gates(results, version, crm_nonnull) -> bool:
    """Delta vs the stored batch + the 5 apply-gates. Buckets by the PRIOR row's
    distress status: 'grounded' = the 33 financial leads, 'grounded_unmodeled' =
    the 103 env/brownfield leads."""
    print(f"\n{'='*72}\nDELTA vs stored + APPLY GATES\n{'='*72}")
    have_old = [x for x in results if x.get("old")]
    if not have_old:
        print("  (no prior batch rows — delta/gates N/A; first run)")
        return False
    fin = [x for x in have_old if x["old"]["ds"] == "grounded"]
    env = [x for x in have_old if x["old"]["ds"] == "grounded_unmodeled"]

    moved = [x for x in fin if not _num_eq(x["comp"]["distress"], x["old"]["dc"])]
    g1 = not moved
    bad = []
    for x in env:
        nd = x["comp"]["distress"]
        modeled = x["coverage"]["distress"]["modeled_tokens"]
        only_env = bool(modeled) and all(
            t in ("environmental_part201", "brownfield") for t in modeled)
        if only_env and not _num_eq(nd, 15.0):
            bad.append((x, f"env-only but distress={nd} (≠15)"))
        elif not only_env and (nd is None or float(nd) < 15.0):
            bad.append((x, f"distress={nd} (<15, unexplained)"))
    g2 = not bad
    g3 = version == 3
    c = _axis_counts(results, "distress")
    g4 = (c["grounded"] == len(results) and c["grounded_unmodeled"] == 0
          and c["absent"] == 0)
    g5 = crm_nonnull == 0
    m = lambda b: "PASS" if b else "FAIL"

    print(f"  gate 1  financial distress UNCHANGED ({len(fin)} leads): {m(g1)}"
          + (f"  — {len(moved)} MOVED" if moved else ""))
    print(f"  gate 2  env/brownfield distress = 15 ({len(env)} leads): {m(g2)}"
          + (f"  — {len(bad)} OFF" if bad else ""))
    print(f"  gate 3  weights_version == 3 (loaded v{version}): {m(g3)}")
    print(f"  gate 4  distress coverage {c['grounded']}/{c['grounded_unmodeled']}/{c['absent']} == {len(results)}/0/0: {m(g4)}")
    print(f"  gate 5  CRM score_* still NULL ({crm_nonnull} non-null): {m(g5)}")
    for x in moved[:5]:
        print(f"    [g1] {x['lead']['parcel_apn']}: {x['old']['dc']} -> {x['comp']['distress']}")
    for x, why in bad[:5]:
        print(f"    [g2] {x['lead']['parcel_apn']}: {why} tokens={x['coverage']['distress']['present_tokens']}")

    old_s = sorted(float(x["old"]["s"]) for x in have_old)
    new_s = sorted(x["score"] for x in have_old)
    shift = sorted(round(x["score"] - float(x["old"]["s"]), 2) for x in env)
    env_d = [x["comp"]["distress"] for x in env]
    print(f"\n  financial: distress unchanged {len(fin)-len(moved)}/{len(fin)}")
    print(f"  env/brownfield: {len(env)} leads, new distress {min(env_d, default=None)}..{max(env_d, default=None)}")
    print(f"  composite score OLD min/med/max = {old_s[0]}/{median(old_s)}/{old_s[-1]}")
    print(f"  composite score NEW min/med/max = {new_s[0]}/{median(new_s)}/{new_s[-1]}")
    if shift:
        print(f"  env score shift: +{shift[0]}..+{shift[-1]} (expect +6.0)")
    return all([g1, g2, g3, g4, g5])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"BATCH FAILED: {e.__class__.__name__}: {e}", file=sys.stderr)
        raise

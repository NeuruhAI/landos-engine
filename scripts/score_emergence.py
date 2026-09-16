"""score_emergence — emergence scorer with hang fail-closed (batched commits + timeouts)."""
import argparse, math, sys
from collections import defaultdict
from datetime import date
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from app.config import DATABASE_URL

EMERGENCE_WEIGHTS = {
    "probate_in": 1.0, "probate": 1.0, "taxsale": 0.9, "taxdelinquent": 0.7,
    "estate_sale": 0.6, "permit": 0.4, "rezoning": 0.4,
}
HALF_LIFE_DAYS = {
    "probate_in": 180, "probate": 180, "taxsale": 90, "taxdelinquent": 180,
    "estate_sale": 30, "permit": 60, "rezoning": 90,
}
DEFAULT_WEIGHT, DEFAULT_HALF_LIFE = 0.5, 90
LOCK_TIMEOUT, STATEMENT_TIMEOUT = "15s", "180s"
WRITE_BATCH_SIZE, SUMMARY_PRINT_CAP = 50, 30

QUALIFYING_SIGNALS = """
    select ds.signal_id, ds.source_type, ds.signal_date,
           coalesce(ds.confidence, 0.5) as confidence,
           ds.matched_lead_id, ds.signal_class
    from landos.discovery_signals ds
    join landos.owner_leads ol on ol.lead_id = ds.matched_lead_id
    where ds.signal_class = 'temporal'
      and ds.matched_lead_id is not null
      and ds.signal_date is not null
      and (%(lead_ids)s::uuid[] is null or ds.matched_lead_id = any(%(lead_ids)s))
      and (%(only_fips)s::text[] is null or ol.county_fips = any(%(only_fips)s))
    order by ds.matched_lead_id, ds.signal_date
"""

def score_signal(row, today):
    st = row["source_type"]
    weight = EMERGENCE_WEIGHTS.get(st, DEFAULT_WEIGHT)
    half_life = HALF_LIFE_DAYS.get(st, DEFAULT_HALF_LIFE)
    if st not in EMERGENCE_WEIGHTS:
        print(f"  WARN unmapped source_type {st!r} (signal {row['signal_id']})")
    age_days = (today - row["signal_date"]).days
    freshness = pow(0.5, age_days / half_life)
    confidence = float(row["confidence"])
    contribution = weight * freshness * confidence
    return contribution, {
        "signal_id": str(row["signal_id"]), "source_type": st,
        "signal_date": row["signal_date"].isoformat(), "age_days": age_days,
        "weight": weight, "freshness": freshness, "confidence": confidence,
        "contribution": contribution,
    }

def _apply_timeouts(cur):
    cur.execute("select set_config('lock_timeout', %s, true)", (LOCK_TIMEOUT,))
    cur.execute("select set_config('statement_timeout', %s, true)", (STATEMENT_TIMEOUT,))

def main(dry_run=False, lead_ids=None, only_fips=None):
    today = date.today()
    lead_ids = list(lead_ids) if lead_ids else None
    only_fips = list(only_fips) if only_fips else None
    params = {"lead_ids": lead_ids, "only_fips": only_fips}
    aborted, updated, results = None, 0, []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        _apply_timeouts(cur)
        cur.execute(QUALIFYING_SIGNALS, params)
        signals = cur.fetchall()
        leaked = [s["signal_id"] for s in signals if s["signal_class"] != "temporal"]
        assert not leaked, f"structural signal(s) reached the scorer — bug: {leaked}"
        per_lead = defaultdict(list)
        for s in signals:
            per_lead[s["matched_lead_id"]].append(s)
        print(f"score_emergence: signals={len(signals)} leads={len(per_lead)} dry_run={dry_run}", flush=True)
        pending = 0
        for i, (lead_id, lead_signals) in enumerate(per_lead.items(), start=1):
            contributions, audit = [], []
            for s in lead_signals:
                c, a = score_signal(s, today)
                contributions.append(c); audit.append(a)
            emerging_score = round(100 * (1 - math.exp(-sum(contributions))), 1)
            results.append((lead_id, emerging_score, len(audit)))
            if dry_run:
                continue
            try:
                cur.execute(
                    """update landos.owner_leads
                       set emerging_score=%s, emerging_signals=%s where lead_id=%s""",
                    (emerging_score, Jsonb(audit), lead_id))
            except Exception as e:
                sqlstate = getattr(e, "sqlstate", None)
                if sqlstate not in ("55P03", "57014") and type(e).__name__ not in (
                        "LockNotAvailable", "QueryCanceled"):
                    raise
                conn.rollback()
                aborted = f"{type(e).__name__}: {e}"
                print(f"  FAIL-CLOSED after {updated} updates: {aborted}", flush=True)
                break
            if cur.rowcount == 1:
                updated += 1
            pending += 1
            if pending >= WRITE_BATCH_SIZE:
                conn.commit(); pending = 0; _apply_timeouts(cur)
                print(f"  progress {i}/{len(per_lead)} ({updated} updated)", flush=True)
        if dry_run:
            conn.rollback()
            print(f"DRY-RUN {len(per_lead)} leads would score")
            for lead_id, es, n in results[:SUMMARY_PRINT_CAP]:
                print(f"  {lead_id} emerging_score={es} ({n})")
            return {"dry_run": True, "leads_scored": len(per_lead), "aborted": None}
        if aborted is None:
            conn.commit()
    print(f"emergence summary: signals={len(signals)} scored={len(per_lead)} updated={updated}"
          + (f" aborted={aborted}" if aborted else ""))
    for lead_id, es, n in results[:SUMMARY_PRINT_CAP]:
        print(f"  lead {lead_id} emerging_score={es} ({n})")
    if len(results) > SUMMARY_PRINT_CAP:
        print(f"  … +{len(results) - SUMMARY_PRINT_CAP} more")
    print("score_emergence DONE", flush=True)
    if aborted:
        raise RuntimeError(f"score_emergence fail-closed: {aborted}")
    return {"dry_run": False, "leads_scored": len(per_lead), "owner_leads_updated": updated,
            "aborted": None, "lead_ids": [r[0] for r in results]}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fips", action="append", dest="only_fips")
    args = ap.parse_args()
    main(dry_run=args.dry_run, only_fips=args.only_fips)

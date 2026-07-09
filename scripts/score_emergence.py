"""
score_emergence.py — CC2 step 2: the EMERGENCE SCORER (AXON Emergence Loop).

    python -m scripts.score_emergence [--dry-run]

Computes landos.owner_leads.emerging_score (+ emerging_signals audit trail) from
TEMPORAL discovery_signals with exponential freshness decay. Projection to the CRM is
NOT this script's job — sync_to_crm.py is the SOLE WRITER of public.acquisition_leads
(single-writer rule); run it afterward to carry emerging_score/_signals across.

Pure DB read -> compute -> DB write. NO network, NO spend.

Doctrine (see DISCOVERY_ADAPTERS.md invariant 6):
  * Only signal_class='temporal' rows that are matched (matched_lead_id) AND dated
    (signal_date) score a lead. Structural derivations NEVER emerge; an unmatched temporal
    signal has no lead to write. Both filters are load-bearing.
  * emerging_score is an INDEPENDENT axis. final_lead_score (absentee/reachability rank)
    and the composite are never read, written, or relabeled here.
  * NULL emerging_score = "not computed" (fail-safe). A lead with no qualifying temporal
    signal stays NULL — never a fabricated 0. (A computed score that DECAYS to ~0 because
    its signal is old is a real value, not a fabrication.)
  * A parcel COOLS on its own as the signal ages: freshness is recomputed from signal_date
    every run, so a score falls over time with no new input. Idempotent within a day;
    intended to drift downward across days.

Scoring (per qualifying signal, grouped by matched_lead_id):
    age_days     = today - signal_date
    freshness    = 0.5 ** (age_days / half_life_days[source_type])   # exponential half-life
    contribution = weight[source_type] * freshness * confidence
    raw_sum      = sum(contribution)                                 # over a lead's signals
    emerging_score = round(100 * (1 - exp(-raw_sum)), 1)             # bounded 0-100, saturating,
                                                                     # diminishing returns when
                                                                     # signals stack. Comparable
                                                                     # SCALE to the composite but
                                                                     # computed INDEPENDENTLY.

The weight / half-life tables are the moat IP — they live HERE (engine repo, behind the
firewall), never in the CRM. Ordering follows the ROI thesis: a death/probate event is the
strongest, freshest motivator; forward-demand (permit/rezoning) is weakest and decays fastest.
"""
import argparse
import math
from collections import defaultdict
from datetime import date

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL

# --- moat IP: scoring tables (TUNABLE) --------------------------------------------------
EMERGENCE_WEIGHTS = {
    "probate_in": 1.0, "probate": 1.0, "taxsale": 0.9, "taxdelinquent": 0.7,
    "estate_sale": 0.6, "code_violation": 0.6, "lis_pendens": 0.8,
    "permit": 0.4, "rezoning": 0.4,
}
HALF_LIFE_DAYS = {
    "probate_in": 180, "probate": 180, "taxsale": 90, "taxdelinquent": 180,
    "estate_sale": 30, "code_violation": 120, "lis_pendens": 150,
    "permit": 60, "rezoning": 90,
}
DEFAULT_WEIGHT = 0.5
DEFAULT_HALF_LIFE = 90

# The ONLY rows that may score a lead. Every filter is load-bearing:
#   temporal   — structural derivations never emerge.
#   matched    — an unmatched signal has no owner_lead to write (FK-joined to prove it resolves).
#   dated      — freshness decay needs an event date.
QUALIFYING_SIGNALS = """
    select ds.signal_id, ds.source_type, ds.signal_date,
           coalesce(ds.confidence, 0.5) as confidence,
           ds.matched_lead_id, ds.signal_class
    from landos.discovery_signals ds
    join landos.owner_leads ol on ol.lead_id = ds.matched_lead_id
    where ds.signal_class = 'temporal'
      and ds.matched_lead_id is not null
      and ds.signal_date is not null
      and (%(sources)s::text[] is null or ds.matched_lead_id in (
          select ds_filter.matched_lead_id
          from landos.discovery_signals ds_filter
          where ds_filter.source_type = any(%(sources)s)
            and ds_filter.matched_lead_id is not null
      ))
    order by ds.matched_lead_id, ds.signal_date
"""


def score_signal(row, today):
    """One temporal signal -> (contribution, auditable 'show your work' object)."""
    st = row["source_type"]
    weight = EMERGENCE_WEIGHTS.get(st, DEFAULT_WEIGHT)
    half_life = HALF_LIFE_DAYS.get(st, DEFAULT_HALF_LIFE)
    if st not in EMERGENCE_WEIGHTS:
        # never silently score an unmapped type
        print(f"  WARN unmapped source_type {st!r} -> default weight {DEFAULT_WEIGHT}, "
              f"half_life {DEFAULT_HALF_LIFE} (signal {row['signal_id']})")
    age_days = (today - row["signal_date"]).days
    freshness = pow(0.5, age_days / half_life)
    confidence = float(row["confidence"])
    contribution = weight * freshness * confidence
    audit = {
        "signal_id": str(row["signal_id"]),
        "source_type": st,
        "signal_date": row["signal_date"].isoformat(),
        "age_days": age_days,
        "weight": weight,
        "freshness": freshness,        # full precision on purpose — audit survives deep decay
        "confidence": confidence,
        "contribution": contribution,
    }
    return contribution, audit


def main(dry_run=False, sources=None):
    today = date.today()
    source_filter = list(sources) if sources else None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(QUALIFYING_SIGNALS, {"sources": source_filter})
        signals = cur.fetchall()

        # RAIL (assert, defense in depth): a structural row must NEVER reach the scorer.
        leaked = [s["signal_id"] for s in signals if s["signal_class"] != "temporal"]
        assert not leaked, f"structural signal(s) reached the scorer — bug: {leaked}"

        per_lead = defaultdict(list)
        for s in signals:
            per_lead[s["matched_lead_id"]].append(s)

        results = []
        updated = 0
        for lead_id, lead_signals in per_lead.items():
            contributions, audit = [], []
            for s in lead_signals:
                c, a = score_signal(s, today)
                contributions.append(c)
                audit.append(a)
            raw_sum = sum(contributions)
            emerging_score = round(100 * (1 - math.exp(-raw_sum)), 1)
            results.append((lead_id, emerging_score, len(audit)))
            if dry_run:
                continue
            cur.execute(
                """update landos.owner_leads
                   set emerging_score = %s, emerging_signals = %s
                   where lead_id = %s""",
                (emerging_score, Jsonb(audit), lead_id))
            if cur.rowcount == 1:
                updated += 1
            else:
                print(f"  WARN owner_leads {lead_id} not updated (rowcount={cur.rowcount})")

        if dry_run:
            conn.rollback()
            print(f"\n=== EMERGENCE DRY-RUN — {len(per_lead)} lead(s) would score, NO writes ===")
            for lead_id, es, n in results:
                print(f"  {lead_id}  emerging_score={es}  ({n} signal(s))")
            print("\nHARD STOP — dry-run only. Re-run without --dry-run to write owner_leads.")
            return

        conn.commit()

    print(f"\nemergence summary: temporal_qualifying_signals={len(signals)} "
          f"leads_scored={len(per_lead)} owner_leads_updated={updated}")
    for lead_id, es, n in results:
        print(f"  lead {lead_id}  emerging_score={es}  ({n} contributing signal(s))")
    print("\nProjection is sync_to_crm.py's job (single-writer). "
          "Run `python -m scripts.sync_to_crm` next to carry it into public.acquisition_leads.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CC2 step 2 — emergence scorer (writes landos.owner_leads)")
    ap.add_argument("--dry-run", action="store_true", help="compute + preview; write nothing")
    ap.add_argument("--source", action="append", help="limit to leads touched by this source_type; repeatable")
    args = ap.parse_args()
    main(dry_run=args.dry_run, sources=args.source)

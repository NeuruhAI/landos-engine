#!/usr/bin/env python3
"""skip_trace_national_plan.py — NATIONAL skip-trace DRY-RUN planner. $0. Submits nothing.

The batch skip-trace RUNNER already exists (scripts/skip_trace_tracerfy.py: run_batch / trace_pool,
national by default, --county / --provenance / --limit scoping, --confirm print-stop, HARD_CAP_USD).
This is its PLANNING companion: it answers "how big is the real national untraced cohort, what would
it cost, and exactly which chunked commands does the operator fire" — WITHOUT touching Tracerfy, the
DB (read-only), or any writer. County-agnostic: works for any US county in the cohort, not just Michiana.

Doctrine reused verbatim from the runner (single source of truth — no forked predicate, no forked price):
  - cohort predicate ...... TARGET_QUERY / select_targets   (skip_trace_tracerfy.py)
  - price ................. COST_PER_CREDIT / CREDITS_PER_TRACE / _est
  - autonomous ceiling .... HARD_CAP_USD  (a single fire may never exceed this)

Result rail (unchanged, NOT re-implemented here): Tracerfy -> import_resolved ->
landos.owner_leads.skip_trace_meta -> DNC scrub (queue-2353 canonical-phone guard, now on main) ->
sync_to_crm (SOLE writer to public.acquisition_leads; unknown DNC stays fail-closed; digits never stored).

This planner NEVER submits, NEVER spends, NEVER writes. It prints the plan and the operator fire commands.
"""
import argparse
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg
from psycopg.rows import dict_row

# Reuse the runner's constants + cohort selector — do not fork them.
from scripts.skip_trace_tracerfy import (
    TENANT, HARD_CAP_USD, TRACERFY_TRACE_TYPE, _est, select_targets,
)


def _county_counts(cur):
    """Per-county untraced counts, from the RUNNER's exact cohort selection (no forked SQL)."""
    rows = select_targets(cur, provenance=None, limit=None)          # the real national cohort
    counts = {}
    for r in rows:
        fips = (r.get("county_fips") if isinstance(r, dict) else None) or "(null)"
        counts[fips] = counts.get(fips, 0) + 1
    return len(rows), counts


def _chunks_for_county(fips, n, cap):
    """Split one county into sub-batches that each stay at/under the autonomous cap.
    A single fire may never exceed HARD_CAP_USD, so a big county is fired as several capped --limit runs."""
    per_batch = max(1, int(cap / (_est(1)[0] or 0.02)))            # max leads whose worst-case <= cap
    n_batches = math.ceil(n / per_batch)
    size = math.ceil(n / n_batches)
    out = []
    remaining = n
    while remaining > 0:
        take = min(size, remaining)
        out.append(take)
        remaining -= take
    return out


def main():
    ap = argparse.ArgumentParser(description="National skip-trace DRY-RUN planner ($0, submits nothing)")
    ap.add_argument("--cap-usd", type=float, default=HARD_CAP_USD,
                    help=f"per-fire ceiling (default HARD_CAP_USD={HARD_CAP_USD}); a fire may not exceed it")
    ap.add_argument("--top", type=int, default=40, help="show this many counties in the breakdown")
    a = ap.parse_args()
    cap = min(a.cap_usd, HARD_CAP_USD)                              # never plan above the autonomous ceiling

    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        sys.exit("DATABASE_URL not set — this planner is read-only but needs the spine to count.")

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("set default_transaction_read_only = on")   # belt: this tool cannot write
            total, counts = _county_counts(cur)

    worst_total, credits = _est(total)
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])

    print(f"\n  NATIONAL SKIP-TRACE PLAN  ·  trace_type={TRACERFY_TRACE_TYPE} ({credits} credit/lead)  ·  DRY-RUN, $0")
    print("  " + "─" * 74)
    print(f"  untraced real cohort : {total:,} leads across {len(counts)} counties")
    print(f"  worst-case spend     : ${worst_total:,.2f}   (misses are free — actual bills only on hits)")
    print(f"  autonomous ceiling   : ${cap:,.2f} per fire")
    print(f"  fires needed         : cohort worst-case ${worst_total:,.2f} "
          f"{'exceeds' if worst_total > cap else 'is within'} the ceiling"
          f"{' -> must chunk' if worst_total > cap else ''}")
    print("\n  BY COUNTY (county_fips · leads · worst-$)\n  " + "─" * 74)
    for fips, n in ordered[:a.top]:
        c, _ = _est(n)
        print(f"    {fips:<10} {n:>6}   ${c:>7,.2f}")
    if len(ordered) > a.top:
        rest = sum(n for _, n in ordered[a.top:])
        print(f"    (+{len(ordered) - a.top} more counties, {rest:,} leads)")

    print("\n  OPERATOR FIRE COMMANDS — chunked to stay <= ${:.2f}/fire. Nothing fires until you run these.".format(cap))
    print("  Each is print-stopped by the runner's own --confirm gate; DNC scrub + sync run after.\n  " + "─" * 74)
    fire_n = 0
    for fips, n in ordered:
        for i, size in enumerate(_chunks_for_county(fips, n, cap), 1):
            c, _ = _est(size)
            fire_n += 1
            tag = f"  # {fips} chunk {i} — {size} leads, worst-case ${c:.2f}"
            print(f"  python -m scripts.skip_trace_tracerfy --batch --county {fips} --limit {size} --confirm{tag}")
    print(f"\n  {fire_n} fire command(s). Total worst-case across all fires: ${worst_total:,.2f}.")
    print("  This planner spent $0, submitted nothing, wrote nothing. The operator fires the runs.\n")


if __name__ == "__main__":
    main()

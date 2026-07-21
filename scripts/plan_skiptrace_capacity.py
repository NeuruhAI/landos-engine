"""
plan_skiptrace_capacity.py — P-27: READ-ONLY dry-run batch planner for the 30k lane.

    python -m scripts.plan_skiptrace_capacity                    # count the live pool (read-only) + plan
    python -m scripts.plan_skiptrace_capacity --pool 30000       # offline: plan a stated pool, NO DB
    python -m scripts.plan_skiptrace_capacity --target 30000 --batch-size 2000 --trace-type normal
    python -m scripts.plan_skiptrace_capacity --no-dnc           # plan without the DNC scrub lane

WHAT THIS TOOL CANNOT DO — BY CONSTRUCTION:
  * It never calls Tracerfy (or any network endpoint): it imports only the pure
    planner (app/capacity_plan) and, in DB mode, runs ONE read-only COUNT/GROUP BY
    over the SAME eligibility predicate the tracer uses (skip_trace_tracerfy's
    TARGET_QUERY semantics) — then rolls back.
  * It never writes: no UPDATE/INSERT anywhere in this module; the connection is
    rolled back on exit either way.
  * It never approves: the plan's approval_gate.approved is constant False. The only
    path to spend is the SEPARATE operator keystroke printed in the plan
    (scripts/skip_trace_tracerfy.py --batch --confirm), which gates itself again.

The plan (expected hits, phone volume, spend range, batch slicing, per-county pool)
prints human-readable plus --json for receipts. Deterministic given the same pool.
"""
import argparse
import json
import sys

from app.capacity_plan import plan_capacity, plan_canonical, render_plan

# The eligibility predicate mirrors scripts/skip_trace_tracerfy.py TARGET_QUERY —
# same tenant, same drops, same not_traced/individual/named/addressed terms — but
# COUNTs it instead of fetching rows (a plan needs volume, not PII).
POOL_COUNT = """
    select county_fips, count(*) as n
    from landos.owner_leads
    where tenant_id = %s and not coalesce(discovery_seed, false)
      and owner_type = 'individual'
      and coalesce(skip_trace_status,'not_traced') = 'not_traced'
      and owner_name is not null and btrim(owner_name) <> ''
      and (owner_mailing_address is not null or situs_address is not null)
      and owner_name not ilike all(%s)
    group by county_fips
    order by county_fips
"""


def count_pool_readonly():
    """One read-only aggregate over the live spine; rolled back on exit."""
    import psycopg
    from psycopg.rows import dict_row

    from app.config import DATABASE_URL
    from scripts.skip_trace_tracerfy import DROP_PATTERNS, TENANT

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(POOL_COUNT, (TENANT, DROP_PATTERNS))
        rows = cur.fetchall()
        conn.rollback()   # nothing to commit — belt and suspenders
    per_county = {r["county_fips"] or "unknown": r["n"] for r in rows}
    return sum(per_county.values()), per_county


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="READ-ONLY skip-trace capacity plan — never traces, never spends")
    ap.add_argument("--target", type=int, default=30_000,
                    help="records to plan for (default 30000)")
    ap.add_argument("--batch-size", type=int, default=2_000,
                    help="records per Tracerfy queue submission (default 2000)")
    ap.add_argument("--trace-type", default="normal", choices=("normal", "advanced"))
    ap.add_argument("--pool", type=int,
                    help="state the eligible pool explicitly (offline mode, NO DB read)")
    ap.add_argument("--no-dnc", action="store_true",
                    help="plan without the per-phone DNC scrub lane")
    ap.add_argument("--json", action="store_true", help="emit the canonical plan JSON")
    a = ap.parse_args(argv)

    if a.pool is not None:
        pool, per_county = a.pool, None
        print(f"(offline mode: pool stated as {pool:,}, no DB read)\n", file=sys.stderr)
    else:
        pool, per_county = count_pool_readonly()

    plan = plan_capacity(pool, target=a.target, batch_size=a.batch_size,
                         trace_type=a.trace_type, dnc=not a.no_dnc,
                         per_county=per_county)
    if a.json:
        print(plan_canonical(plan))
    else:
        print(render_plan(plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())

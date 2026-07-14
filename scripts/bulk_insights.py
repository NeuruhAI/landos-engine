"""
bulk_insights.py — DS-07 JOB 4: bulk-generate briefs for the top-N leads by score on the
LOCAL model ($0), print samples verbatim so the operator can judge whether a VA reading one
would be MORE effective. Never uses the paid tier (allow_paid=False). Honest about quality:
prints the grounding-richness distribution, and if the model was unreachable it says so and
serves the deterministic grounded floor (the exact facts the local model is constrained to).

    python -m scripts.bulk_insights [--limit 200] [--samples 5] [--commit]

Without --commit the cache/ledger writes are rolled back (a preview). With --commit they land.
"""
import argparse
import os

os.environ.setdefault("INSIGHT_FALLBACK_DETERMINISTIC", "1")  # never crash on an offline model

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from app.insight import service, meter

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

TOP_N_SQL = """
    select lead_id from public.acquisition_leads
    where tenant_id = %s
    order by coalesce(score_distress, score_owner, 0) desc, lead_id
    limit %s
"""


def _render(sample):
    o = sample
    lines = [f"lead {o['lead_id']}   [model: {o['model']}, facts: {o['n_facts']}]",
             "  WHAT WE KNOW: " + (o["brief"] or "(empty)"),
             "  THE ANGLE:    " + (o["angle"] or "(empty)"),
             "  DO NOT SAY:"]
    for d in o["do_not_say"]:
        lines.append(f"    - {d}")
    lines.append("  GROUNDING (claim -> field):")
    for k, fv in list(o["grounding"].items())[:12]:
        lines.append(f"    - {k} = {fv['value']!r}   [{fv['field']}]")
    return "\n".join(lines)


def main(limit=200, samples=5, commit=False):
    conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    cur = conn.cursor()
    cur.execute(TOP_N_SQL, (TENANT, limit))
    ids = [str(r["lead_id"]) for r in cur.fetchall()]

    results, models, empty, thin, rich = [], {}, 0, 0, 0
    for lid in ids:
        out = service.generate_insight(cur, lid, allow_paid=False)
        if out is None:
            continue
        n = len(out.get("grounding", {}))
        out["n_facts"] = n
        out["lead_id"] = lid
        results.append(out)
        models[out["model"]] = models.get(out["model"], 0) + 1
        if not (out["brief"] or out["angle"]):
            empty += 1
        elif n <= 2:
            thin += 1
        elif n >= 5:
            rich += 1

    print(f"\n{'='*74}\nDS-07 BULK INSIGHTS — top {len(results)} by score (local model, $0)\n{'='*74}")
    print(f"models: {models}")
    print(f"richness: empty={empty}  thin(<=2 facts)={thin}  mid=({len(results)-empty-thin-rich})  rich(>=5 facts)={rich}")
    print(f"meter: {meter.summary(cur)}")

    # samples spread across the ranked, non-empty set — representative, not cherry-picked.
    nonempty = [r for r in results if (r["brief"] or r["angle"])]
    if nonempty and samples:
        step = max(1, len(nonempty) // samples)
        picks = nonempty[::step][:samples]
        print(f"\n{'-'*74}\n{len(picks)} SAMPLE BRIEFS (verbatim, spread across the ranked set)\n{'-'*74}")
        for i, s in enumerate(picks, 1):
            print(f"\n[{i}] " + _render(s))

    if commit:
        conn.commit()
        print(f"\n[committed] cache + ledger persisted.")
    else:
        conn.rollback()
        print(f"\n[preview] cache/ledger rolled back (re-run with --commit to persist).")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DS-07 bulk insight generation (local model, $0)")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--commit", action="store_true", help="persist cache + ledger (default: preview/rollback)")
    a = ap.parse_args()
    main(limit=a.limit, samples=a.samples, commit=a.commit)

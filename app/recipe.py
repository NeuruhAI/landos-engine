"""
recipe.py — RECIPE-2 composer CLI (the terminal twin of POST /compose +
/decide). Drives the same recipe_core the API does, straight against the spine.

    python -m app.recipe compose acquire "Berrien discovered parcels, need 50"
    python -m app.recipe compose dispose "sell these" --lead <uuid> --lead <uuid> \
            --target 50000 --timeline 90
    python -m app.recipe decide <proposal_id> approve
    python -m app.recipe decide <proposal_id> reject
    python -m app.recipe list [--n 10]

Compose ALWAYS syncs a proposal row (proposed, or needs_clarification with
questions). Decide is the operator's action — approve mints the composed recipe
+ a run; reject flips status. Same single-writer projection as the API.
"""
import argparse
import sys

import psycopg
from psycopg.rows import dict_row

from .config import DATABASE_URL
from . import recipe_core


def _compose(args):
    dispo = None
    if args.goal_type == "dispose":
        params = {}
        if args.target:
            params["target_price_usd"] = args.target
        if args.timeline:
            params["timeline_days"] = args.timeline
        if args.note:
            params["note"] = args.note
        dispo = {"inventory": [{"lead_id": lid, "disposition_id": None, "county_fips": None}
                               for lid in (args.lead or [])],
                 "params": params}
        if not dispo["inventory"]:
            raise SystemExit("dispose needs inventory — pass --lead <uuid> (repeatable)")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        pid, body = recipe_core.compose_and_sync(cur, args.goal_type, args.goal_text, dispo)
        conn.commit()
    print(f"proposal {pid}  [{body['status']}]  gate={body['gate']['verdict']}")
    if body["needs_clarification"]:
        print("  the composer asks:")
        for q in body["needs_clarification"]:
            print(f"    - {q}")
    else:
        y = body["expected_yield"]
        print(f"  yield: {y['available_now']} available now"
              + (f" · target {y['target']}" if y.get("target") else ""))
        for ing in body["ingredients"]:
            print(f"  ingredient: {ing['source']} — {ing['filter']} = {ing['est_count']}")
    print(f"\ndecide it: python -m app.recipe decide {pid} approve|reject")


def _decide(args):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        try:
            res = recipe_core.decide(cur, args.proposal_id, args.action)
        except (LookupError, ValueError) as e:
            conn.rollback()
            raise SystemExit(f"decide failed: {e}")
        conn.commit()
    print(f"proposal {res['proposal_id']} -> {res['status']}")
    if res.get("recipe_id"):
        print(f"  composed recipe: {res['recipe_id']}")
    if res.get("run_id"):
        print(f"  run: {res['run_id']}")


def _list(args):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("""select proposal_id, goal_type, status, is_fixture,
                              left(goal_text, 48) goal, gate->>'verdict' gate,
                              expected_yield->>'available_now' avail
                       from public.recipe_proposals_view
                       order by created_at desc limit %s""", (args.n,))
        rows = cur.fetchall()
    print(f"{'proposal_id':38} {'type':8} {'status':20} {'gate':8} {'avail':6} goal")
    for r in rows:
        tag = " [fixture]" if r["is_fixture"] else ""
        print(f"{str(r['proposal_id']):38} {r['goal_type']:8} {r['status']:20} "
              f"{r['gate'] or '—':8} {r['avail'] or '—':6} {r['goal']}{tag}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="RECIPE-2 composer CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("compose", help="compose a goal into a proposal")
    c.add_argument("goal_type", choices=["acquire", "dispose"])
    c.add_argument("goal_text")
    c.add_argument("--lead", action="append", help="dispose inventory lead_id (repeatable)")
    c.add_argument("--target", type=float, help="dispose target price usd")
    c.add_argument("--timeline", type=int, help="dispose timeline days")
    c.add_argument("--note", help="dispose note")
    c.set_defaults(fn=_compose)

    d = sub.add_parser("decide", help="approve|reject a proposal")
    d.add_argument("proposal_id")
    d.add_argument("action", choices=["approve", "reject"])
    d.set_defaults(fn=_decide)

    l = sub.add_parser("list", help="recent proposals")
    l.add_argument("--n", type=int, default=10)
    l.set_defaults(fn=_list)

    args = ap.parse_args(argv)
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())

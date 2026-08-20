#!/usr/bin/env python3
"""Re-route every unresolved candidate through the v3 router.

v1 decided from owner condition alone. v3 also knows two things the system
learned since: which entity a candidate's owner belongs to and what researching
that entity already produced, and which automated sources have already been run
against the candidate and returned nothing.

The practical effect is that 4,452 candidates owned by 2,764 entities stop being
told to search the NC SOS individually when someone has already searched for
that entity, and a candidate that has been through the voter file is not sent
back to the voter file.

Nothing about contact, callability, score, rank or qualification is touched. The
only columns written are the route: operator_queue, contact_status, next_source,
next_source_why, next_source_manual, blocking_reason.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.resolution.router import ROUTER_VERSION, QUEUE_CONTACT_STATUS, next_best_source


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--cascade-dir", action="append", default=[],
                    help="A $0 contact-cascade output directory. Repeatable. See "
                         "the note on negative memory below.")
    args = ap.parse_args()

    # ------------------------------------------------------------------
    # Negative memory.
    #
    # The query below derives "already attempted" from the evidence rows a
    # candidate carries -- which means it can only ever see sources that
    # SUCCEEDED. A candidate the voter file was asked about and had no phone for
    # produces no evidence, looks untried, and is routed straight back to the
    # voter file. 62 Qualified rows carried exactly that instruction.
    #
    # The cascade's own _rejected.json is the missing record: it lists every
    # subject the source was asked about and returned nothing for. HELD is not
    # in that set -- a source whose file was absent from the host was never
    # actually asked, and retiring it would discard a route that still has yield.
    # ------------------------------------------------------------------
    EXHAUSTED_VERDICTS = {"NO_MATCH", "IDENTITY_CORROBORATED_NO_PHONE"}
    negative: dict = {}
    for d in args.cascade_dir:
        rej = Path(d).expanduser() / "_rejected.json"
        if not rej.exists():
            print(f"cascade dir has no _rejected.json, skipped: {d}", file=sys.stderr)
            continue
        for row in json.loads(rej.read_text()):
            cid = (row.get("candidate_id") or "").strip()
            if cid and row.get("verdict") in EXHAUSTED_VERDICTS:
                negative.setdefault(cid, set()).add("NCSBE_PUBLIC_VOTER_FILE")
    if args.cascade_dir:
        print(f"negative memory: {len(negative)} candidates carry a source that was "
              f"run and returned nothing")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                select c.import_row_id::text as candidate_id,
                       c.owner_class, c.owner_condition, c.contact_resolution_state,
                       c.operator_queue, c.next_source, c.high_phone_count,
                       c.has_living_identity, c.estate_resolution_state,
                       coalesce(r.normalized ->> 'county', '') as county,
                       e.resolution_state as entity_state,
                       coalesce(e.principal_name, '') as entity_principal,
                       -- Every source that has produced an evidence row for this
                       -- candidate is, by definition, a source already run.
                       coalesce(array_agg(distinct ev.source_family)
                                filter (where ev.source_family is not null), '{}') as attempted
                  from public.candidate_contact_resolution c
                  join public.lead_import_rows r on r.row_id = c.import_row_id
                  left join public.entity_identity e on e.entity_identity_id = c.entity_identity_id
                  left join public.candidate_parties cp on cp.import_row_id = c.import_row_id
                  left join public.contact_evidence ev on ev.evidence_id = cp.evidence_id
                 group by c.import_row_id, r.row_id, e.entity_identity_id
            """)
            rows = cur.fetchall()

        changes, queues = [], Counter()
        for row in rows:
            route = next_best_source(
                owner_class=row["owner_class"],
                owner_condition=row["owner_condition"],
                contact_resolution_state=row["contact_resolution_state"],
                county=row["county"],
                has_living_identity=bool(row["has_living_identity"]),
                # T30A. A recorded public-index no-match is a RESULT, and it
                # sends the candidate to a different desk than one nobody has
                # looked up. Read from the state the search itself wrote.
                estate_public_index_searched=(
                    row["estate_resolution_state"] == "ESTATE_NO_PUBLIC_CASE_FOUND"),
                has_high_phone=(row["high_phone_count"] or 0) > 0,
                tier_c_held=row["contact_resolution_state"] == "TIER_C_HELD_NOT_APPENDED",
                attempted_sources=frozenset(row["attempted"] or ())
                                  | negative.get(row["candidate_id"], set()),
                entity_resolution_state=row["entity_state"] or "",
                entity_principal_known=bool(row["entity_principal"]),
            )
            queues[route.queue] += 1
            if route.queue != row["operator_queue"] or route.next_source != row["next_source"]:
                changes.append((row["candidate_id"], route.queue,
                                QUEUE_CONTACT_STATUS.get(route.queue, ""),
                                route.next_source, route.why,
                                route.manual_or_automatic, route.blocking_reason))

        report = {"generated_at": utcnow(), "router_version": ROUTER_VERSION,
                  "scanned": len(rows), "rows_rerouted": len(changes),
                  "negative_memory_candidates": len(negative),
                  "queues_after": dict(queues.most_common())}
        print(json.dumps(report, indent=1))
        if not args.apply:
            print("DRY RUN. No route written.")
            return 0

        with conn.cursor() as cur:
            cur.execute("""
                create temp table _t24a_route (
                  import_row_id uuid primary key, operator_queue text, contact_status text,
                  next_source text, next_source_why text, next_source_manual text,
                  blocking_reason text) on commit drop
            """)
            with cur.copy("copy _t24a_route (import_row_id, operator_queue, contact_status, "
                          "next_source, next_source_why, next_source_manual, blocking_reason) "
                          "from stdin") as cp:
                for c in changes:
                    cp.write_row(c)
            cur.execute("""
                update public.candidate_contact_resolution c
                   set operator_queue = t.operator_queue,
                       contact_status = t.contact_status,
                       next_source = t.next_source,
                       next_source_why = t.next_source_why,
                       next_source_manual = t.next_source_manual,
                       blocking_reason = t.blocking_reason
                  from _t24a_route t
                 where t.import_row_id = c.import_row_id
            """)
            written = cur.rowcount
        conn.commit()
        print(json.dumps({"rows_written": written}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

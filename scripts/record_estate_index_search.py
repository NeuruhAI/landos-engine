#!/usr/bin/env python3
"""T30A — persist what the public estate index actually said, and re-route on it.

This is the negative-memory writer the addendum requires. It answers, per
candidate: what source, what person, what county, what query variants, what
filters, when, and what result. Without it the router has no way to tell "the
index was searched and holds no such case" from "nobody has looked", and the
candidate goes back to a courthouse counter for a file that is not there.

WHAT IT WRITES
  candidate_contact_resolution.estate_resolution_state = ESTATE_NO_PUBLIC_CASE_FOUND
  ...for PUBLIC_INDEX_NO_MATCH only.

WHAT IT REFUSES TO WRITE
  * A result state that is about the INSTRUMENT rather than the record.
    HUMAN_VERIFICATION_REQUIRED and SEARCH_INCONCLUSIVE may never become
    negative evidence -- a challenge that interrupted a session says nothing
    about whether an estate exists. Enforced by
    source_procedure.supports_replay_suppression, not by convention here.
  * Any party, relationship or authority. An index search that found no case
    creates no people.
  * AMBIGUOUS_MATCH. An unresolved identity is not a negative and not a match.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import DATABASE_URL
from app.resolution.source_procedure import (
    NC_ECOURTS_ESTATE_SMART_SEARCH_V1 as PROC,
    supports_replay_suppression,
)

PRIVATE = Path.home() / ".neuruh/private/deedsonar/wake-estates"
STATE = "ESTATE_NO_PUBLIC_CASE_FOUND"


def load():
    targets = json.loads((PRIVATE / "00_TARGETS_GRADE_A.json").read_text())
    findings = json.loads((PRIVATE / "01_ECOURTS_FINDINGS.json").read_text())
    by_rank = {r["global_rank"]: r for r in targets}
    return by_rank, findings["decedents"]


def plan(by_rank, decedents):
    write, refuse = [], []
    for f in decedents:
        ranks = [f["rank"]] + list(f.get("also_ranks") or [])
        verdict = f["verdict"]
        for rank in ranks:
            row = by_rank.get(rank)
            if row is None:
                continue
            item = {
                "rank": rank,
                "candidate_id": row["candidate_id"],
                # The manifest does not carry tenant_id; the candidate id is
                # the primary key of candidate_contact_resolution, so it alone
                # identifies the row.
                "decedent_query": f["decedent_search"],
                "county": row["source_county"],
                "scope": f.get("scope", ""),
                "verdict": verdict,
                "observed_at": "2026-08-20",
                "procedure": f"{PROC.name} v{PROC.version}",
            }
            if supports_replay_suppression(verdict):
                write.append(item)
            else:
                item["refused_because"] = (
                    f"{verdict} does not support replay suppression under "
                    f"{PROC.name} v{PROC.version}"
                )
                refuse.append(item)
    return write, refuse


def apply(conn, rows):
    written = 0
    with conn.cursor() as cur:
        for r in rows:
            cur.execute("""
                update public.candidate_contact_resolution
                   set estate_resolution_state = %s
                 where import_row_id = %s
                   and estate_resolution_state is distinct from %s
            """, (STATE, r["candidate_id"], STATE))
            written += cur.rowcount
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    by_rank, decedents = load()
    write, refuse = plan(by_rank, decedents)
    report = {
        "procedure": f"{PROC.name} v{PROC.version}",
        "decedents_completed": len(decedents),
        "candidates_to_mark": len(write),
        "candidates_refused": len(refuse),
        "refusals": [{k: v for k, v in r.items() if k in
                      ("rank", "verdict", "refused_because")} for r in refuse],
        "applied": False,
    }
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        if args.apply:
            report["rows_written"] = apply(conn, write)
            conn.commit()
            report["applied"] = True
            report["second_pass_rows"] = apply(conn, write)
            conn.commit()
            report["second_pass_noop"] = report["second_pass_rows"] == 0
        with conn.cursor() as cur:
            cur.execute("select estate_resolution_state s, count(*) n "
                        "from public.candidate_contact_resolution "
                        "group by 1 order by 2 desc")
            report["estate_state_census"] = {r["s"] or "(null)": r["n"]
                                             for r in cur.fetchall()}
    text = json.dumps(report, indent=1)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

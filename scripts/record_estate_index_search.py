#!/usr/bin/env python3
"""T30A — persist what the public estate index actually said, and re-route on it.

This is the negative-memory writer the addendum requires. It answers, per
candidate: what source, what person, what county, what query variants, what
filters, when, and what result. Without it the router has no way to tell "the
index was searched and holds no such case" from "nobody has looked", and the
candidate goes back to a courthouse counter for a file that is not there.

WHAT IT WRITES
  candidate_contact_resolution.estate_resolution_state:
    PUBLIC_INDEX_NO_MATCH        -> ESTATE_NO_PUBLIC_CASE_FOUND
    OFFICIAL_ESTATE_CASE_FOUND   -> ESTATE_OFFICIAL_CASE_FOUND   (T50A)

  ...and, for BOTH, one row of public.case_source_attempt: who looked at what
  source, for which decedent, with which query, and what came back.

T50A CLOSED THE OTHER HALF. This script originally wrote the negative only,
because the negative was the only one that had a state to be written as. The
positive -- an official estate case actually located, with a file number -- had
nowhere to go, so the most expensive result this lane produced was the one it
could not record, and the next lane to work the candidate would search the
index again. Migration 20260821110000 added the state; app.case.CaseSourceAttempt
added the durable record of the look itself.

A LOCATED CASE IS NOT AUTHORITY. ESTATE_OFFICIAL_CASE_FOUND says the index holds
a case. It does not say who may act: an index entry is not letters, and the
appointment is in the file. Nothing here writes a party, a relationship or a
relationship_class, so the product still refuses to call anyone a signatory.

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

from app.contact_authority import contact_dsn_or_exit
from app.resolution.source_procedure import (
    NC_ECOURTS_ESTATE_SMART_SEARCH_V1 as PROC,
    supports_replay_suppression,
)
from app.case import CaseSourceAttempt, is_positive
from app.case.attempt import INSERT_SQL, insert_params

PRIVATE = Path.home() / ".neuruh/private/deedsonar/wake-estates"

#: The verdict -> resolution state map. Only these two verdicts change the
#: candidate's estate state; everything else is recorded as an attempt and
#: changes nothing, which is the point of the refusal list above.
STATE_FOR_VERDICT = {
    "PUBLIC_INDEX_NO_MATCH": "ESTATE_NO_PUBLIC_CASE_FOUND",
    "OFFICIAL_ESTATE_CASE_FOUND": "ESTATE_OFFICIAL_CASE_FOUND",
}


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
            state = STATE_FOR_VERDICT.get(verdict)
            if state:
                item["state"] = state
                item["positive"] = is_positive(verdict)
                write.append(item)
            elif supports_replay_suppression(verdict):
                # A verdict that suppresses a replay but has no estate state is
                # still worth an attempt row; it just does not move the
                # candidate's estate position.
                item["state"] = None
                item["positive"] = False
                write.append(item)
            else:
                item["refused_because"] = (
                    f"{verdict} does not support replay suppression under "
                    f"{PROC.name} v{PROC.version}"
                )
                refuse.append(item)
    return write, refuse


def attempt_for(r):
    """The durable record of the look itself, positive or negative.

    Constructed through app.case.CaseSourceAttempt so the vocabulary check, the
    subject requirement and the cost/currency rule all run here rather than at
    the database. The SUBJECT is the DECEDENT, not the candidate: one decedent
    can own several parcels -- T30A's nine candidates were eight decedents, two
    pairs sharing an estate -- so a person searched once must be findable from
    every case that person touches.
    """
    return CaseSourceAttempt(
        candidate_id=r["candidate_id"],
        subject_kind="PERSON",
        subject_ref=r["decedent_query"],
        subject_label=r["decedent_query"],
        domain="AUTHORITY",
        source_id=PROC.name,
        source_name="NC eCourts Portal — estates index",
        source_family=PROC.source_family,
        procedure=PROC.name,
        procedure_version=PROC.version,
        query=r["decedent_query"],
        actor_type="ENGINE",
        started_at=f"{r['observed_at']}T00:00:00+00:00",
        completed_at=f"{r['observed_at']}T00:00:00+00:00",
        result_state=r["verdict"],
        notes=(
            f"Scope: {r.get('scope') or 'not recorded'}. "
            f"County: {r.get('county') or 'not recorded'}."
        ),
    )


def apply(conn, rows):
    """Write the estate state and the attempt. Both idempotent."""
    written = 0
    attempts = 0
    with conn.cursor() as cur:
        for r in rows:
            state = r.get("state")
            if state:
                cur.execute("""
                    update public.candidate_contact_resolution
                       set estate_resolution_state = %s
                     where import_row_id = %s
                       and estate_resolution_state is distinct from %s
                """, (state, r["candidate_id"], state))
                written += cur.rowcount
            # The attempt row is what stops the next lane re-running this
            # search. `on conflict do nothing` makes a second pass a no-op.
            cur.execute(INSERT_SQL, insert_params(attempt_for(r)))
            if cur.fetchone():
                attempts += 1
    return written, attempts


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
        # T50A: both halves are now countable. Before this the positives were
        # invisible because they had nowhere to be written.
        "positives": sum(1 for r in write if r.get("positive")),
        "negatives": sum(1 for r in write if r.get("state") == "ESTATE_NO_PUBLIC_CASE_FOUND"),
        "candidates_refused": len(refuse),
        "refusals": [{k: v for k, v in r.items() if k in
                      ("rank", "verdict", "refused_because")} for r in refuse],
        "applied": False,
    }
    # DEEDSONAR_DATABASE_URL, then DATABASE_URL; the retired legacy DB is refused (exit 2).
    with psycopg.connect(contact_dsn_or_exit(), row_factory=dict_row) as conn:
        if args.apply:
            report["rows_written"], report["attempts_written"] = apply(conn, write)
            conn.commit()
            report["applied"] = True
            second_rows, second_attempts = apply(conn, write)
            conn.commit()
            report["second_pass_rows"] = second_rows
            report["second_pass_attempts"] = second_attempts
            report["second_pass_noop"] = second_rows == 0 and second_attempts == 0
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

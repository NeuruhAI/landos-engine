"""
read_lead_outcomes.py — JOB 8 nightly reader: CRM call dispositions -> landos.lead_outcomes.

    python -m scripts.read_lead_outcomes [--dry-run]

Lifts observed dispositions from public.lead_activity into the durable feedback-fact
store landos.lead_outcomes, idempotent on (source, source_ref). READ-ONLY on the CRM
(public.*); writes landos.lead_outcomes only. The scorer reads the LATEST disposition
per lead as a deterministic modifier next run (see docs/lead_outcomes_scorer_modifier.md).

Honest-null: an unmapped CRM outcome is counted + skipped, never coerced into a
disposition. Only the mapped vocabulary below becomes a fact.
"""
import argparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL

# CRM outcome vocabulary -> lead_outcomes disposition domain (lowercased on read).
OUTCOME_MAP = {
    "connected": "interested",         # a live connect = engaged owner
    "voicemail": "callback",           # reached the line, deferred
    "callback": "callback",
    "bad_number": "wrong_number",
    "wrong_number": "wrong_number",
    "dnc": "dnc", "do_not_call": "dnc",
    "deceased": "dead", "dead": "dead", "not_interested": "dead",
}

READ_ACTIVITY = """
    select activity_id::text src_ref, lead_id, tenant_id, outcome, type, note,
           coalesce(created_at, now()) noted_at
    from public.lead_activity
    where outcome is not null and btrim(outcome) <> ''
"""

UPSERT = """
    insert into landos.lead_outcomes
        (tenant_id, lead_id, disposition, source, source_ref, noted_at, raw)
    values (%(tenant_id)s, %(lead_id)s, %(disposition)s, 'lead_activity', %(src_ref)s,
            %(noted_at)s, %(raw)s)
    on conflict (source, source_ref) do nothing
"""


def main(dry_run: bool = False) -> int:
    ingested = skipped_unmapped = 0
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(READ_ACTIVITY)
        rows = cur.fetchall()
        for r in rows:
            disp = OUTCOME_MAP.get((r["outcome"] or "").strip().lower())
            if not disp:
                skipped_unmapped += 1
                continue
            if dry_run:
                ingested += 1
                continue
            cur.execute(UPSERT, {
                "tenant_id": r["tenant_id"], "lead_id": r["lead_id"], "disposition": disp,
                "src_ref": r["src_ref"], "noted_at": r["noted_at"],
                "raw": Jsonb({"outcome": r["outcome"], "type": r["type"], "note": r["note"]}),
            })
            ingested += cur.rowcount
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"read_lead_outcomes {mode}: candidates={len(rows)} "
          f"{'would_ingest' if dry_run else 'ingested'}={ingested} skipped_unmapped={skipped_unmapped}")
    return ingested


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="nightly CRM disposition -> landos.lead_outcomes")
    ap.add_argument("--dry-run", action="store_true", help="count + preview; write nothing")
    main(ap.parse_args().dry_run)

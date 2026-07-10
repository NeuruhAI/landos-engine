"""
read_lead_outcomes.py — JOB 8 nightly reader: CRM call dispositions -> landos.lead_outcomes.

    python -m scripts.read_lead_outcomes [--dry-run]

Lifts observed dispositions from public.lead_activity into the durable feedback-fact
store landos.lead_outcomes, idempotent on (source, source_ref). READ-ONLY on the CRM
(public.*); writes landos.lead_outcomes only. The scorer reads the LATEST disposition
per lead as a deterministic modifier next run (see docs/lead_outcomes_scorer_modifier.md).

TWO WRITERS, TWO VOCABULARIES (2026-07-09):
  * legacy calls route -> writes its 6-value outcome DIRECTLY into lead_activity.outcome
    (connected / voicemail / bad_number / no_answer / dnc_request / appointment).
  * Caller Desk (Gate 3, mig 054 trigger) -> writes a LOSSY funnel into
    lead_activity.outcome (contacted/callback/not_interested/interested_promote all
    collapse to 'connected'; only no_answer/dnc_request survive) and preserves the
    PRECISE disposition in lead_activity.meta.disposition (meta.source='caller_desk').
Reading the lossy .outcome for Caller Desk rows drops dnc_request (TCPA!) and reads a
DECLINE (not_interested) as 'connected'->interested (a wrong-direction RAISE). So we
prefer meta.disposition when the row is caller_desk, and only fall back to the .outcome
funnel for legacy rows.

Honest-null: an unmapped signal is counted + skipped, never coerced. Self-healing:
ON CONFLICT DO UPDATE re-maps existing facts to the current vocabulary on re-run (so a
fixed mapping corrects prior mis-reads without a delete) — still never duplicates.
"""
import argparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL

# LEGACY path: lead_activity.outcome vocabulary -> disposition domain (lowercased on read).
OUTCOME_MAP = {
    "connected": "interested",         # a live connect = engaged owner
    "voicemail": "callback",           # reached the line, deferred
    "callback": "callback",
    "bad_number": "wrong_number",
    "wrong_number": "wrong_number",
    "dnc": "dnc", "do_not_call": "dnc",
    "deceased": "dead", "dead": "dead", "not_interested": "dead",
}

# CALLER DESK path: the PRECISE disposition in meta.disposition -> disposition domain.
# This is the true reachability verdict, before the mig-054 funnel collapses it.
CALLER_DESK_MAP = {
    "interested_promote": "interested",   # warm — promote
    "contacted": "callback",              # reached, neutral -> treat as a soft callback
    "callback": "callback",               # asked to call back
    "not_interested": "dead",             # owner DECLINED — a down verdict, never a raise
    "dnc_request": "dnc",                 # do-not-call — TCPA suppression (must be captured)
    "no_answer": None,                    # not reached — neutral, produces NO fact
}


def resolve_disposition(outcome, meta) -> str | None:
    """The single mapping seam. Prefer the Caller Desk's PRECISE disposition
    (meta.disposition) over the lossy lead_activity.outcome funnel; fall back to the
    legacy .outcome vocabulary for non-caller-desk rows. Returns None (honest-null) for
    unmapped or explicitly-neutral signals — the caller skips those."""
    m = meta or {}
    if isinstance(m, dict) and m.get("source") == "caller_desk":
        return CALLER_DESK_MAP.get((m.get("disposition") or "").strip().lower())
    return OUTCOME_MAP.get((outcome or "").strip().lower())


READ_ACTIVITY = """
    select activity_id::text src_ref, lead_id, tenant_id, outcome, type, note, meta,
           coalesce(created_at, now()) noted_at
    from public.lead_activity
    where outcome is not null and btrim(outcome) <> ''
"""

# self-healing upsert: re-run re-maps disposition/raw to the CURRENT vocabulary (heals a
# prior mis-read) but never duplicates (unique key holds). (xmax = 0) => a fresh insert.
UPSERT = """
    insert into landos.lead_outcomes
        (tenant_id, lead_id, disposition, source, source_ref, noted_at, raw)
    values (%(tenant_id)s, %(lead_id)s, %(disposition)s, 'lead_activity', %(src_ref)s,
            %(noted_at)s, %(raw)s)
    on conflict (source, source_ref) do update
        set disposition = excluded.disposition,
            noted_at    = excluded.noted_at,
            raw         = excluded.raw
    returning (xmax = 0) as inserted
"""


def main(dry_run: bool = False) -> int:
    inserted = corrected = skipped_unmapped = 0
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(READ_ACTIVITY)
        rows = cur.fetchall()
        for r in rows:
            disp = resolve_disposition(r["outcome"], r["meta"])
            if not disp:
                skipped_unmapped += 1
                continue
            if dry_run:
                inserted += 1
                continue
            cur.execute(UPSERT, {
                "tenant_id": r["tenant_id"], "lead_id": r["lead_id"], "disposition": disp,
                "src_ref": r["src_ref"], "noted_at": r["noted_at"],
                "raw": Jsonb({"outcome": r["outcome"], "type": r["type"], "note": r["note"],
                              "meta": r["meta"], "resolved_disposition": disp}),
            })
            if cur.fetchone()["inserted"]:
                inserted += 1
            else:
                corrected += 1
        if dry_run:
            conn.rollback()
        else:
            conn.commit()
    mode = "DRY-RUN" if dry_run else "APPLY"
    verb = "would_ingest" if dry_run else "inserted"
    print(f"read_lead_outcomes {mode}: candidates={len(rows)} "
          f"{verb}={inserted} corrected={corrected} skipped_unmapped={skipped_unmapped}")
    return inserted + corrected


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="nightly CRM disposition -> landos.lead_outcomes")
    ap.add_argument("--dry-run", action="store_true", help="count + preview; write nothing")
    main(ap.parse_args().dry_run)

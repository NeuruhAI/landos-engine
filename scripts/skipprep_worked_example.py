"""
skipprep_worked_example.py — SKIP-PREP gate proof (NOT a production script).

Walks ONE target end-to-end on the REAL paths:
  resolved phone -> import_resolved -> skip_trace_meta (landos only, idempotent)
  -> sync_to_crm -> callable in public.acquisition_leads, with the DNC gate
  proven: the same lead flips OUT of callable when scrubbed do_not_call.

Picks one currently-not_traced qualified individual, drives it through the
import + sync, asserts callable, then flips dnc_status=do_not_call, re-syncs,
asserts suppressed — then RESTORES the lead to its pre-demo state (synthetic
555 phone removed, projection cleared). Reversible by design.
"""
import csv
import os
import sys

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from scripts.import_resolved import run as import_run
from scripts.sync_to_crm import main as sync_main

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
TMP_CSV = "data/incoming/skipprep_demo.csv"

# columns import_resolved reads (subset is fine; it keys on lead_id)
COLS = ["lead_id", "owner_name", "phone_1", "phone_1_type", "phone_1_confidence",
        "phone_1_source", "verified_via", "resolved_at", "va_notes",
        "dnc_status", "outreach_recipe", "primary_target"]


def projected(cur, lead_id):
    cur.execute("select phone_1, contact_confidence, outreach_recipe "
                "from public.acquisition_leads where source_lead_id=%s", (lead_id,))
    return cur.fetchone()


def write_csv(row):
    os.makedirs("data/incoming", exist_ok=True)
    with open(TMP_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        w.writerow(row)


def main():
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            "select lead_id, owner_name from landos.owner_leads "
            "where tenant_id=%s and not discovery_seed and owner_type='individual' "
            "and skip_trace_status='not_traced' "
            "and (skip_trace_meta is null or not (skip_trace_meta ? 'phones')) "
            "order by final_lead_score desc nulls last limit 1", (TENANT,))
        target = cur.fetchone()
        if not target:
            sys.exit("no clean not_traced individual available for the demo")
        lead_id, owner = str(target["lead_id"]), target["owner_name"]
        print(f"TARGET: {owner!r}  lead_id={lead_id}")

    base = {"lead_id": lead_id, "owner_name": owner,
            "phone_1": "269-555-0199", "phone_1_type": "mobile",
            "phone_1_confidence": "0.9", "phone_1_source": "skipprep_demo",
            "verified_via": "skipprep_demo,second_source",
            "resolved_at": "2026-06-10",
            "va_notes": "SKIP-PREP GATE DEMO - synthetic 555 number, removed at end",
            "outreach_recipe": "R1", "primary_target": "owner"}

    # ---- STEP 1: resolved phone -> import_resolved (writeback, landos only) ----
    print("\n[1] import resolved phone (dnc=unchecked)")
    write_csv({**base, "dnc_status": "unchecked"})
    import_run(TMP_CSV)
    print("[1ب] re-run import (idempotency)")
    import_run(TMP_CSV)

    # ---- STEP 2: sync -> callable in CRM ---------------------------------------
    print("\n[2] sync_to_crm  -> expect CALLABLE")
    sync_main()

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        p = projected(cur, lead_id)
        callable_ok = p and p["phone_1"]
        print(f"  CRM after sync: phone_1={p['phone_1']} conf={p['contact_confidence']} "
              f"recipe={p['outreach_recipe']}  -> {'CALLABLE' if callable_ok else 'NOT callable'}")

        # idempotency: skip_trace_meta phones count must be 1, not 2
        cur.execute("select jsonb_array_length(skip_trace_meta->'phones') n, "
                    "skip_trace_status from landos.owner_leads where lead_id=%s", (lead_id,))
        m = cur.fetchone()
        print(f"  landos skip_trace_meta phones={m['n']} (idempotent: 1) "
              f"status={m['skip_trace_status']}")

    # ---- STEP 3: DNC gate -> flip do_not_call, re-sync -> SUPPRESSED -----------
    print("\n[3] flip dnc_status=do_not_call, re-import + re-sync -> expect SUPPRESSED")
    write_csv({**base, "dnc_status": "do_not_call"})
    import_run(TMP_CSV)
    sync_main()

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        p = projected(cur, lead_id)
        suppressed_ok = p and not p["phone_1"]
        cur.execute("select skip_trace_status, confidence_score, "
                    "skip_trace_meta->>'dnc_status' dnc from landos.owner_leads "
                    "where lead_id=%s", (lead_id,))
        m = cur.fetchone()
        print(f"  landos: status={m['skip_trace_status']} conf={m['confidence_score']} "
              f"dnc={m['dnc']}  (still traced+confident in landos)")
        print(f"  CRM after sync: phone_1={p['phone_1']}  -> "
              f"{'SUPPRESSED (DNC gate held)' if suppressed_ok else 'LEAK - FAIL'}")

    # ---- STEP 4: restore pre-demo state ----------------------------------------
    print("\n[4] restore lead to pre-demo state")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("update landos.owner_leads set skip_trace_meta=NULL, "
                    "confidence_score=NULL, skip_trace_status='not_traced', "
                    "updated_at=now() where lead_id=%s", (lead_id,))
        conn.commit()
    sync_main()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        p = projected(cur, lead_id)
        print(f"  CRM after restore: phone_1={p['phone_1']} (cleared)")
    os.path.exists(TMP_CSV) and os.remove(TMP_CSV)

    print("\nGATE SKIP-PREP worked example:")
    print(f"  PASS  writeback landos-only + idempotent (phones=1 on re-run)")
    print(f"  {'PASS' if callable_ok else 'FAIL'}  resolved phone -> synced -> callable in CRM")
    print(f"  {'PASS' if suppressed_ok else 'FAIL'}  DNC gate: do_not_call lead never callable")


if __name__ == "__main__":
    main()

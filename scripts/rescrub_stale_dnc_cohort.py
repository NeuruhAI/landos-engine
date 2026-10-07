"""
rescrub_stale_dnc_cohort.py — DNC-only re-scrub of the 39 FREE-SCRUB-41 HOLD leads.

These CRM leads carry dnc=FALSE from the July 13 Tracerfy scrub. Since migration
20260925225641 (31-day TSR window) that scrub no longer counts as clear evidence, so
none of them can reach callable_leads until the numbers are scrubbed again.

Default is read-only and $0:

    .venv/bin/python -m scripts.rescrub_stale_dnc_cohort

Paid fire (38 unique phones x $0.02 = $0.76, hard cap $1.00):

    .venv/bin/python -m scripts.rescrub_stale_dnc_cohort \
      --apply --confirm-spend 0.76 --cap-usd 1.00

Already-paid queue recovery ($0; GET-only, mutually exclusive with --apply):

    .venv/bin/python -m scripts.rescrub_stale_dnc_cohort --resume-dnc-queue <id>

The paid call goes through scripts.skip_trace_tracerfy.submit_dnc, which is wrapped by
the FREE-FIRST paid_call guard: it clears only with a Governance ALLOW for this spend.
This script never authorizes, bypasses or retries around that guard.

It does NOT skip-trace anyone and never writes either database. It writes an immutable,
mode-0600 staged CSV (scrubbed_at = scrub time) for the existing
intake_tracerfy_phones -> sync_to_crm rail.

Order is enforced:
  1. the cohort is exactly the 39 pinned lead ids (the 3 synthetic 555 fixtures are
     refused), all tenant-scoped, archived, spine-linked, not opted out, dnc=FALSE;
  2. none of them is in callable_leads before the scrub;
  3. the paid payload is exactly 38 unique phones / $0.76, under the $1.00 cap;
  4. Tracerfy must return one result for every submitted phone;
  5. only then is the staged receipt written.
Un-archiving is a separate step and is never done here.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from scripts.scrub_crm_phone_unknowns import (
    CSV_FIELDS,
    _write_private,
    classify_vendor_result,
    nanp_key,
)
from scripts.skip_trace_tracerfy import (
    COST_PER_CREDIT,
    _dnc_index,
    poll_dnc,
    submit_dnc,
)


TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
AUTHORIZED_CAP_USD = Decimal("1.00")
EXPECTED_ROWS = 39
EXPECTED_UNIQUE_PHONES = 38
EXPECTED_COST_USD = Decimal("0.76")
SOURCE_LABEL = "tracerfy_dnc_rescrub_20260926"

# FREE-SCRUB-41 HOLD cohort, read from efxowqudmsfqjmbeidjc on 2026-09-26.
COHORT_IDS = frozenset({
    "03620af3-a781-4813-af06-2969bcee319d", "05a3d690-6639-4cd0-b803-8639f2c08bc4",
    "08dd20fa-15bf-44dd-94f6-afa4a2f50cb6", "0a6eef98-0359-471e-b7a3-3c9e3049ff80",
    "0d0e03f3-5fe8-417d-bb4b-3d30ec994651", "0d3f7180-1b01-40c1-af2c-617db708fca4",
    "0d605cac-1eaf-49e7-9125-44035e882a7e", "124938ce-4d57-4964-bab2-ff97f629e2c2",
    "139875d4-aa58-4d76-8d72-10314a959539", "17b2182d-de57-495b-a264-02fa1a908ad5",
    "1b3f3fd1-98ba-4a0f-a902-96227bd4eac8", "1c399658-a094-4316-b164-27e6cdfb7329",
    "1d1f1204-763b-4257-8e80-28bb936b27d4", "1d8ffa8c-0a85-44e1-9d85-4136c4e9c283",
    "1e104802-6259-457b-be08-cb783b34f3e3", "202aa998-8418-4eac-bd64-c681c346c24b",
    "21ec34d2-59e1-4b3d-83f6-1d327921dafc", "24f860b7-2895-437d-a9b1-0d93eb3b92a3",
    "2502d30b-6fda-45af-ad8b-eb23791a804b", "26f50089-0c9d-485a-815a-26d81fa838ed",
    "27884e34-44a9-41a1-879f-477e38183a42", "2b1a7949-7322-426d-8014-47586268d815",
    "2b3b38ee-09ea-4636-a84a-7e37c7541242", "2b4149cc-9d1a-45d1-9fea-f7e3d072315c",
    "2ba77ebc-7905-46e4-a270-ec82904681f1", "33148274-9059-420d-bc70-7e9b9b70444f",
    "338eb6d6-e8c2-4efe-a680-4fe6fb76473d", "33c49709-4252-4b06-b333-428d4db09696",
    "358e9a38-8fd7-46dd-a10a-eaeebfbb9a2a", "3855f0ea-0e8e-4681-83a3-743e0715a8af",
    "38aee7fb-9a3c-437e-aeef-d754b998c823", "38eb26bb-1d0d-4f74-822c-4c43b918df15",
    "3a397fa6-99de-4d1f-b540-d9be2f30c3ed", "3a7f72c1-90d0-4a2f-9a75-b79bbc2db49b",
    "3ba68b39-1b2a-474e-873a-b04f11aa1f1c", "3bdb2ebd-07bb-455f-a0e2-b628961a07ad",
    "3bfa5232-925f-445d-b0a7-c35990916396", "3c7236e5-8113-48f9-94bd-9c40792acac6",
    "3d842f58-cb67-463a-96d9-172f41d25a42",
})
# Synthetic 555 / test fixtures: never submitted, never billed.
EXCLUDED_ID_PREFIXES = ("087a66d2", "1adeac15", "8897b18e")


COHORT_SQL = """
    select
      a.lead_id::text as target_key,
      a.source_lead_id::text as owner_lead_id,
      a.phone_1 as phone,
      a.phone_1_type as phone_type,
      a.dnc,
      a.stage,
      coalesce(a.is_test, false) or coalesce(a.is_synthetic, false) as is_fixture,
      exists (
        select 1 from comms.opt_outs o
         where o.lead_id = a.lead_id and o.channel in ('call', 'all')
      ) as opted_out
    from public.acquisition_leads a
    where a.tenant_id = %s
      and a.lead_id = any(%s::uuid[])
    order by a.lead_id
"""


def is_nanp_555(phone: str | None) -> bool:
    return nanp_key(phone)[3:6] == "555"


def cost_for(count: int) -> Decimal:
    return (Decimal(str(COST_PER_CREDIT)) * count).quantize(Decimal("0.01"))


def validate_cohort(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ids = {r["target_key"] for r in rows}
    if len(rows) != EXPECTED_ROWS or ids != COHORT_IDS:
        missing = sorted(COHORT_IDS - ids)
        extra = sorted(ids - COHORT_IDS)
        raise RuntimeError(
            f"STOP — cohort drift: rows={len(rows)} missing={missing} extra={extra}")

    excluded = sorted(i for i in ids if i.startswith(EXCLUDED_ID_PREFIXES))
    if excluded:
        raise RuntimeError(f"STOP — excluded synthetic fixtures in payload: {excluded}")

    problems = Counter()
    for r in rows:
        if r["dnc"] is not False:
            problems["dnc_not_false"] += 1
        if r["stage"] != "archived":
            problems["not_archived"] += 1
        if not r["owner_lead_id"]:
            problems["no_spine_row"] += 1
        if r["is_fixture"]:
            problems["test_or_synthetic"] += 1
        if r["opted_out"]:
            problems["opted_out"] += 1
        if is_nanp_555(r["phone"]):
            problems["nanp_555"] += 1
    if problems:
        raise RuntimeError(f"STOP — cohort state drift: {dict(problems)}")

    phones = sorted({nanp_key(r["phone"]) for r in rows})
    if len(phones) != EXPECTED_UNIQUE_PHONES:
        raise RuntimeError(
            f"STOP — expected {EXPECTED_UNIQUE_PHONES} unique billable phones, "
            f"found {len(phones)}")
    return {
        "rows": len(rows),
        "unique_phones": len(phones),
        "phones": phones,
        "cost_usd": cost_for(len(phones)),
    }


def read_plan() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(COHORT_SQL, (TENANT, sorted(COHORT_IDS)))
            rows = list(cur.fetchall())
            cur.execute(
                "select count(*) as n from public.callable_leads where lead_id = any(%s::uuid[])",
                (sorted(COHORT_IDS),))
            callable_members = cur.fetchone()["n"]
    stats = validate_cohort(rows)
    stats["callable_members"] = callable_members
    return rows, stats


def assert_paid_preflight(stats: dict[str, Any], cap: Decimal) -> None:
    if cap > AUTHORIZED_CAP_USD:
        raise RuntimeError(
            f"STOP — requested cap USD {cap:.2f} exceeds authorized USD {AUTHORIZED_CAP_USD:.2f}")
    if stats["cost_usd"] > cap:
        raise RuntimeError(
            f"STOP — estimated USD {stats['cost_usd']:.2f} exceeds cap USD {cap:.2f}")
    if stats["cost_usd"] != EXPECTED_COST_USD:
        raise RuntimeError(
            f"STOP — expected exact authorized cost USD {EXPECTED_COST_USD:.2f}, "
            f"found USD {stats['cost_usd']:.2f}")
    if stats["callable_members"]:
        raise RuntimeError(
            f"STOP — {stats['callable_members']} cohort rows already appear callable")


def write_receipts(
    rows: list[dict[str, Any]],
    vendor_index: dict[str, dict[str, Any]],
    output: Path,
    queue_id: str,
    cost: Decimal,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    submitted = {nanp_key(r["phone"]) for r in rows}
    canonical_vendor: dict[str, dict[str, Any]] = {}
    for raw_phone, info in vendor_index.items():
        key = nanp_key(raw_phone)
        if key in canonical_vendor:
            raise RuntimeError(
                f"STOP — Tracerfy result alias collision: queue_id={queue_id} key={key}; "
                "no receipt written")
        canonical_vendor[key] = info
    returned = set(canonical_vendor)
    if returned != submitted:
        raise RuntimeError(
            f"STOP — Tracerfy result mismatch: queue_id={queue_id} "
            f"missing={len(submitted - returned)} extra={len(returned - submitted)}; "
            "no partial receipt written")

    scrubbed_at = (now or datetime.now(timezone.utc)).isoformat().replace("+00:00", "Z")
    staged: list[dict[str, str]] = []
    counts = Counter()
    for row in rows:
        status, flags = classify_vendor_result(canonical_vendor[nanp_key(row["phone"])])
        counts[status] += 1
        staged.append({
            "target_key": row["target_key"],
            "full_name": "",
            "phone": row["phone"],
            "phone_type": row["phone_type"] or "",
            "confidence": "",
            "source": SOURCE_LABEL,
            "pulled_at": scrubbed_at,
            "scrub_status": status,
            "scrub_flags": flags,
            "scrubbed_at": scrubbed_at,
        })

    def csv_writer(f):
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        w.writerows(staged)

    _write_private(output, csv_writer)
    sha = hashlib.sha256(output.read_bytes()).hexdigest()
    manifest_path = output.with_suffix(output.suffix + ".manifest.json")
    manifest = {
        "kind": "tracerfy_dnc_rescrub_stale_cohort",
        "generated_at_utc": scrubbed_at,
        "queue_id": str(queue_id),
        "submitted_rows": len(rows),
        "unique_phones": len(submitted),
        "estimated_cost_usd": f"{cost:.2f}",
        "hard_cap_usd": f"{AUTHORIZED_CAP_USD:.2f}",
        "clean_rows": counts["clean"],
        "dnc_rows": counts["dnc"],
        "csv_file": output.name,
        "csv_sha256": sha,
        "excluded_fixtures": list(EXCLUDED_ID_PREFIXES),
    }
    _write_private(manifest_path, lambda f: json.dump(manifest, f, indent=2, sort_keys=True))
    return {"csv": output, "manifest": manifest_path, **manifest}


def print_plan(stats: dict[str, Any]) -> None:
    print("TRACERFY DNC RE-SCRUB — FREE-SCRUB-41 HOLD COHORT — READ-ONLY PREFLIGHT")
    print(f"  rows={stats['rows']}  unique_phones={stats['unique_phones']}  "
          f"cohort_in_callable={stats['callable_members']}")
    print(f"  excluded fixtures (never submitted): {', '.join(EXCLUDED_ID_PREFIXES)}")
    print(f"  spend={stats['unique_phones']} x ${COST_PER_CREDIT} = USD {stats['cost_usd']:.2f}  "
          f"cap=USD {AUTHORIZED_CAP_USD:.2f}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="DNC re-scrub of the 39 stale FREE-SCRUB-41 leads")
    ap.add_argument("--apply", action="store_true",
                    help="submit the paid DNC scrub (guarded) and write the staged receipt")
    ap.add_argument("--confirm-spend", type=Decimal,
                    help="must equal the exact planned spend (0.76)")
    ap.add_argument("--cap-usd", type=Decimal, default=AUTHORIZED_CAP_USD,
                    help="hard ceiling; values above 1.00 are rejected")
    ap.add_argument("--resume-dnc-queue", dest="resume_dnc_queue",
                    help="GET an already-paid DNC queue and reconcile it; never resubmit")
    ap.add_argument("--output", type=Path,
                    help="new staged CSV path; existing files are never overwritten")
    args = ap.parse_args(argv)

    if args.resume_dnc_queue and (args.apply or args.confirm_spend is not None):
        raise SystemExit("STOP — --resume-dnc-queue is GET-only; drop --apply/--confirm-spend")

    rows, stats = read_plan()
    print_plan(stats)
    if not args.apply and not args.resume_dnc_queue:
        print("  DRY-RUN: no network, no spend, no files, no database writes.")
        return

    assert_paid_preflight(stats, args.cap_usd)
    if not args.resume_dnc_queue and args.confirm_spend != stats["cost_usd"]:
        raise SystemExit(f"STOP — --confirm-spend must equal {stats['cost_usd']:.2f}")

    key = (os.environ.get("TRACERFY_API_KEY") or "").strip()
    if not key:
        raise SystemExit("STOP — TRACERFY_API_KEY is not configured")

    if args.resume_dnc_queue:
        queue_id = str(args.resume_dnc_queue)
        print(f"  RESUME queue={queue_id}; no new submission and no new spend")
    else:
        queue_id, _ = submit_dnc(stats["phones"], key)
        print(f"  SUBMITTED exact={len(stats['phones'])} queue={queue_id} "
              f"authorized_spend=USD {stats['cost_usd']:.2f}")

    vendor_rows = poll_dnc(queue_id, key, expected_phones=stats["phones"])
    output = args.output or Path(
        "data/incoming/"
        + datetime.now(timezone.utc).strftime("tracerfy_dnc_rescrub39_%Y%m%dT%H%M%SZ.csv"))
    receipt = write_receipts(
        rows, _dnc_index(vendor_rows), output, str(queue_id), stats["cost_usd"])
    print(f"  COMPLETE clean_rows={receipt['clean_rows']} dnc_rows={receipt['dnc_rows']} "
          f"csv={receipt['csv']} sha256={receipt['csv_sha256']}")
    print(f"  manifest={receipt['manifest']}")
    print("  NEXT (operator sequence):")
    print(f"    .venv/bin/python -m scripts.intake_tracerfy_phones --csv {receipt['csv']}")
    print(f"    .venv/bin/python -m scripts.intake_tracerfy_phones --csv {receipt['csv']} --apply")
    print("    .venv/bin/python -m scripts.sync_to_crm --dry-run")
    print("    .venv/bin/python -m scripts.sync_to_crm")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, psycopg.Error) as exc:
        sys.exit(str(exc))

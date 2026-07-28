"""
scrub_crm_phone_unknowns.py — DNC-only scrub for the exact legacy CRM cohort.

Default is read-only and $0:

    .venv/bin/python -m scripts.scrub_crm_phone_unknowns

Paid fire (authorized 2026-07-14 for exactly 48 unique phones, $0.96, cap $2):

    .venv/bin/python -m scripts.scrub_crm_phone_unknowns \
      --apply --confirm-spend 0.96 --cap-usd 2.00

This does NOT skip-trace anyone. It submits only the 48 phone numbers already
present on CRM rows whose seller contact did not come from the spine_sync
scrubbed projection. It writes an immutable, mode-0600 staged CSV for the
existing intake_tracerfy_phones -> sync_to_crm rail. It never writes either
database schema.

Order is enforced:
  1. migration 080 must already have made dnc nullable/no-default;
  2. none of the 48 unknown rows may still carry legacy dnc=FALSE;
  3. paid payload must be exactly 48 rows / 48 unique phones / $0.96;
  4. Tracerfy must return one result for every submitted phone;
  5. only then is the staged receipt written.

Three CRM-native rows are intentionally present in the paid payload but have no
LandOS source_lead_id. Intake will receipt them as NO_SPINE_ROW:
  * operator-owned smoke fixture — never auto-promoted; all-channel opt-out wins;
  * Belinda Meien — real manual lead; scrub is not consent, remains held;
  * existing dnc=TRUE row — hard suppression stays sticky.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from scripts.skip_trace_tracerfy import (
    COST_PER_CREDIT,
    _dnc_index,
    poll_dnc,
    submit_dnc,
)


TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
EXPECTED_ROWS = 48
EXPECTED_UNIQUE_PHONES = 48
AUTHORIZED_CAP_USD = Decimal("2.00")
EXPECTED_COST_USD = Decimal("0.96")
SMOKE_ID = "90391cd1-e792-4fa1-bc18-78818d55cde2"
BELINDA_ID = "8c661c22-22f3-40d8-9b2d-2a579d4c4097"
KNOWN_DNC_ID = "1ccf7876-98b2-4183-a04c-7cf8f50c8a27"
EXPECTED_UNLINKED = {SMOKE_ID, BELINDA_ID, KNOWN_DNC_ID}
EXPECTED_PROVENANCE = Counter({
    "phone_1_backfill": 45,
    "homeowner_direct": 1,
    "(null)": 2,
})
CSV_FIELDS = [
    "target_key", "full_name", "phone", "phone_type", "confidence", "source",
    "pulled_at", "scrub_status", "scrub_flags", "scrubbed_at",
]


COHORT_SQL = """
    select
      a.lead_id::text as target_key,
      a.source_lead_id::text as owner_lead_id,
      a.phone_1 as phone,
      a.phone_1_type as phone_type,
      a.dnc,
      case
        when c.contact_id is null then '(null)'
        when c.source is null then '(null)'
        else c.source
      end as seller_contact_source
    from public.acquisition_leads a
    left join lateral (
      select contact_id, source
        from public.contacts
       where related_lead_id = a.lead_id
         and contact_type = 'seller'
       order by created_at asc
       limit 1
    ) c on true
    where a.tenant_id = %s
      and nullif(btrim(a.phone_1), '') is not null
      and coalesce(c.source, '') <> 'spine_sync'
    order by a.lead_id
"""


SCHEMA_SQL = """
    select is_nullable, column_default
      from information_schema.columns
     where table_schema = 'public'
       and table_name = 'acquisition_leads'
       and column_name = 'dnc'
"""


def digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def cost_for(count: int) -> Decimal:
    return (Decimal(str(COST_PER_CREDIT)) * count).quantize(Decimal("0.01"))


def classify_vendor_result(info: dict[str, Any]) -> tuple[str, str]:
    """Return staged (status, flags). Missing/contradictory clean truth fails DNC."""
    flag_names = [
        name for name in ("national_dnc", "state_dnc", "dma", "litigator")
        if bool(info.get(name))
    ]
    clean = bool(info.get("is_clean")) and not flag_names
    if clean:
        return "clean", ""
    return "dnc", ";".join(flag_names or ["vendor_not_clean"])


def validate_cohort(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rows) != EXPECTED_ROWS:
        raise RuntimeError(
            f"STOP — cohort drift: expected {EXPECTED_ROWS} rows, found {len(rows)}")

    normalized = [digits(r["phone"]) for r in rows]
    if any(len(n) not in (10, 11) for n in normalized):
        raise RuntimeError("STOP — cohort contains an invalid phone shape")
    if len(set(normalized)) != EXPECTED_UNIQUE_PHONES:
        raise RuntimeError(
            "STOP — cohort must be exactly 48 distinct billable phone numbers")

    linked = [r for r in rows if r["owner_lead_id"]]
    unlinked = {r["target_key"] for r in rows if not r["owner_lead_id"]}
    if len(linked) != 45 or unlinked != EXPECTED_UNLINKED:
        raise RuntimeError(
            f"STOP — bridge drift: linked={len(linked)} unlinked_ids={sorted(unlinked)}")

    provenance = Counter(r["seller_contact_source"] for r in rows)
    if provenance != EXPECTED_PROVENANCE:
        raise RuntimeError(
            f"STOP — provenance drift: expected {dict(EXPECTED_PROVENANCE)}, "
            f"found {dict(provenance)}")

    states = Counter(
        "true" if r["dnc"] is True else "false" if r["dnc"] is False else "null"
        for r in rows
    )
    return {
        "rows": len(rows),
        "unique_phones": len(set(normalized)),
        "linked": len(linked),
        "unlinked": len(unlinked),
        "provenance": provenance,
        "dnc_states": states,
        "phones": normalized,
    }


def read_plan() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
            schema = cur.fetchone()
            cur.execute(COHORT_SQL, (TENANT,))
            rows = list(cur.fetchall())
            cur.execute("""
                select count(*) as n
                  from public.callable_leads cl
                 where cl.lead_id = any(%s::uuid[])
            """, ([r["target_key"] for r in rows],))
            callable_members = cur.fetchone()["n"]

    stats = validate_cohort(rows)
    stats["schema_ready"] = bool(
        schema
        and schema["is_nullable"] == "YES"
        and schema["column_default"] is None
    )
    stats["callable_members"] = callable_members
    stats["cost_usd"] = cost_for(stats["unique_phones"])
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
    if not stats["schema_ready"]:
        raise RuntimeError("STOP — migration 080 is not live (dnc is not nullable/no-default)")
    if stats["dnc_states"].get("false", 0):
        raise RuntimeError(
            "STOP — legacy dnc=FALSE remains in the 48-phone cohort; apply migration 080 first")
    if stats["callable_members"]:
        raise RuntimeError(
            f"STOP — {stats['callable_members']} unknown cohort rows still appear callable")


def _write_private(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise RuntimeError(f"STOP — refusing to overwrite existing receipt: {path}")
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        raise RuntimeError(f"STOP — stale temp receipt exists: {tmp}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", newline="") as f:
            writer(f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def write_receipts(
    rows: list[dict[str, Any]],
    vendor_index: dict[str, dict[str, Any]],
    output: Path,
    queue_id: str,
    cost: Decimal,
) -> dict[str, Any]:
    submitted = {digits(r["phone"]) for r in rows}
    returned = set(vendor_index)
    if returned != submitted:
        missing = len(submitted - returned)
        extra = len(returned - submitted)
        raise RuntimeError(
            f"STOP — Tracerfy result mismatch: missing={missing} extra={extra}; "
            "no partial receipt written")

    scrubbed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    staged: list[dict[str, str]] = []
    counts = Counter()
    for row in rows:
        status, flags = classify_vendor_result(vendor_index[digits(row["phone"])])
        counts[status] += 1
        staged.append({
            "target_key": row["target_key"],
            "full_name": "",
            "phone": row["phone"],
            "phone_type": row["phone_type"] or "",
            "confidence": "",
            "source": "tracerfy_dnc_20260714",
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
        "kind": "tracerfy_dnc_unknown_crm_phones",
        "generated_at_utc": scrubbed_at,
        "queue_id": str(queue_id),
        "submitted_rows": len(rows),
        "unique_phones": len(submitted),
        "estimated_cost_usd": f"{cost:.2f}",
        "hard_cap_usd": f"{AUTHORIZED_CAP_USD:.2f}",
        "clean": counts["clean"],
        "dnc": counts["dnc"],
        "csv_file": output.name,
        "csv_sha256": sha,
        "linked_targets": 45,
        "unlinked_targets": 3,
        "unlinked_policy": {
            SMOKE_ID: "operator fixture; all-channel opt-out; never auto-promote",
            BELINDA_ID: "manual human lead; scrub is not consent; hold",
            KNOWN_DNC_ID: "existing hard DNC remains sticky",
        },
    }
    _write_private(
        manifest_path,
        lambda f: json.dump(manifest, f, indent=2, sort_keys=True),
    )
    return {"csv": output, "manifest": manifest_path, **manifest}


def print_plan(stats: dict[str, Any]) -> None:
    print("TRACERFY DNC-ONLY COHORT — READ-ONLY PREFLIGHT")
    print(f"  rows={stats['rows']}  unique_phones={stats['unique_phones']}  "
          f"linked={stats['linked']}  unlinked={stats['unlinked']}")
    print(f"  provenance={dict(stats['provenance'])}")
    print(f"  current_dnc={dict(stats['dnc_states'])}")
    print(f"  migration_080_ready={stats['schema_ready']}  "
          f"cohort_in_callable={stats['callable_members']}")
    print(f"  spend={stats['unique_phones']} x $0.02 = USD {stats['cost_usd']:.2f}  "
          f"authorized_cap=USD {AUTHORIZED_CAP_USD:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="DNC-scrub exactly 48 existing unknown CRM phones")
    ap.add_argument("--apply", action="store_true",
                    help="submit paid DNC scrub and write staged receipt")
    ap.add_argument("--confirm-spend", type=Decimal,
                    help="must equal the exact planned spend (0.96)")
    ap.add_argument("--cap-usd", type=Decimal, default=AUTHORIZED_CAP_USD,
                    help="hard ceiling; values above 2.00 are rejected")
    ap.add_argument("--resume-queue-id",
                    help="poll an already-paid DNC queue; never resubmit")
    ap.add_argument("--output", type=Path,
                    help="new staged CSV path; existing files are never overwritten")
    args = ap.parse_args()

    rows, stats = read_plan()
    print_plan(stats)
    if not args.apply:
        print("  DRY-RUN: no network, no spend, no files, no database writes.")
        return

    assert_paid_preflight(stats, args.cap_usd)
    if not args.resume_queue_id and args.confirm_spend != stats["cost_usd"]:
        raise SystemExit(
            f"STOP — --confirm-spend must equal {stats['cost_usd']:.2f}")

    key = (os.environ.get("TRACERFY_API_KEY") or "").strip()
    if not key:
        raise SystemExit("STOP — TRACERFY_API_KEY is not configured")

    if args.resume_queue_id:
        queue_id = str(args.resume_queue_id)
        print(f"  RESUME queue={queue_id}; no new submission and no new spend")
    else:
        queue_id, _ = submit_dnc(stats["phones"], key)
        print(f"  SUBMITTED exact={len(stats['phones'])} queue={queue_id} "
              f"authorized_spend=USD {stats['cost_usd']:.2f}")

    vendor_rows = poll_dnc(queue_id, key)
    output = args.output or Path(
        "data/incoming/"
        + datetime.now(timezone.utc).strftime("tracerfy_dnc_unknown_48_%Y%m%dT%H%M%SZ.csv")
    )
    receipt = write_receipts(
        rows, _dnc_index(vendor_rows), output, str(queue_id), stats["cost_usd"])
    print(f"  COMPLETE clean={receipt['clean']} dnc={receipt['dnc']} "
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

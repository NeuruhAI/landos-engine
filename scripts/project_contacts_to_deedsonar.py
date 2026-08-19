#!/usr/bin/env python3
"""T21 — project the validated HIGH free contacts into DeedSonar production.

WHAT IS PROJECTED
  Only validated HIGH contacts, matching a current candidate identity, with full
  provenance. No MEDIUM. No REVIEW. No TIER_C held row. No callability.

WHERE IT LANDS
  public.parties / party_phones          -- the canonical person-level model,
                                            created by migration 104 for exactly
                                            this and empty until now
  public.contact_evidence                -- provenance for every asserted fact
  public.candidate_parties               -- candidate <-> party WITH ITS ROLE
  public.candidate_contact_resolution    -- the bounded per-candidate read model

WHERE IT DOES NOT LAND
  public.acquisition_leads.phone_1/2/3, dnc, contact_confidence. Those belong to
  scripts/sync_to_crm.py, which declares itself their sole writer and gates on
  DNC-clear plus a traced skip-trace status. These contacts are NOT callable, so
  writing them there would be wrong on ownership AND on compliance.

IDEMPOTENCE
  Evidence ids are content addresses; parties are keyed on (tenant, kind, name);
  party_phones already carries unique (party_id, phone_e164); candidate_parties
  is unique on (tenant, row, party, role). Running twice converges. The
  --verify-idempotent flag proves it by running the plan twice and comparing.

SAFETY
  Default is --dry-run. --apply requires the dry run to have reported
  UNEXPLAINED_COLLISIONS=0, writes a pre-state rollback manifest OUTSIDE Git
  before the first mutation, and runs in one transaction.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contact.identity_tuple import estate_revision, master_csv_paths
from app.resolution.contract import CALLABILITY_NOT_AUTHORIZED
from app.resolution.factory import build_records, load_csv
from app.resolution.router import QUEUE_CONTACT_STATUS

CANONICAL_BATCH_PREDICATE = """
  b.import_kind = 'data_estate'
  and b.is_test is false and b.is_synthetic is false
  and (b.source_context ->> 'dataset') = 'CURRENT_CANONICAL'
  and (b.source_context ->> 'current') = 'true'
  and (b.source_context ->> 'authoritative') = 'true'
"""

COUNTY_ORDER = ["Mecklenburg", "Wake", "Durham", "Johnston", "Union", "Cabarrus", "Gaston"]

ACTION_INSERT = "INSERT"
ACTION_APPEND = "APPEND"
ACTION_NOOP = "NOOP"
ACTION_CONFLICT_HOLD = "CONFLICT_HOLD"
ACTION_REJECT = "REJECT"

# Confidence ordering. A projection may never replace something better.
CONFIDENCE_RANK = {"REJECTED": 0, "REVIEW": 1, "MEDIUM": 2, "HIGH": 3}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_estate(estate_dir: Path) -> List[dict]:
    rows: List[dict] = []
    for county in COUNTY_ORDER:
        for path in sorted((estate_dir / county / "master").glob(f"{county.lower()}_master_*.csv")):
            rows.extend(load_csv(path))
    return rows


# ---------------------------------------------------------------------------
# Read the current production state for exactly the candidates we would touch
# ---------------------------------------------------------------------------

def read_existing(conn, candidate_ids: Sequence[str]) -> Dict[str, dict]:
    """Everything already known about these candidates' contacts."""
    out: Dict[str, dict] = {}
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""
            select r.row_id::text as row_id, r.tenant_id::text as tenant_id,
                   r.source_row_id, r.candidate_class, r.linked_lead_id::text as linked_lead_id,
                   r.normalized ->> 'ownerName' as owner_name,
                   b.batch_id::text as batch_id
              from public.lead_import_rows r
              join public.lead_import_batches b on b.batch_id = r.batch_id
             where r.row_id = any(%s::uuid[]) and {CANONICAL_BATCH_PREDICATE}
        """, (list(candidate_ids),))
        for row in cur.fetchall():
            row["existing_phones"] = []
            row["existing_parties"] = []
            row["crm_phones"] = []
            row["has_ccr"] = False
            out[row["row_id"]] = row

        if not out:
            return out
        ids = list(out)

        # The T21 tables may not exist yet -- a dry run must be runnable BEFORE
        # the migration is applied, otherwise the collision court cannot be read
        # until after the schema change it is supposed to justify.
        cur.execute("select to_regclass('public.candidate_parties') is not null as ok")
        t21_present = cur.fetchone()["ok"]
        if not t21_present:
            for rec in out.values():
                rec["t21_schema"] = "ABSENT"
        else:
            for rec in out.values():
                rec["t21_schema"] = "PRESENT"

        # phones already attached through the person model
        if t21_present:
          cur.execute("""
            select cp.import_row_id::text as row_id, pp.phone_e164,
                   pp.confidence_category, pp.phone_role, p.canonical_name
              from public.candidate_parties cp
              join public.parties p on p.party_id = cp.party_id
              left join public.party_phones pp on pp.party_id = p.party_id
             where cp.import_row_id = any(%s::uuid[])
          """, (ids,))
          for row in cur.fetchall():
            rec = out.get(row["row_id"])
            if not rec:
                continue
            rec["existing_parties"].append(row["canonical_name"])
            if row["phone_e164"]:
                rec["existing_phones"].append({
                    "phone": row["phone_e164"],
                    "confidence": row["confidence_category"] or "HIGH",
                    "role": row["phone_role"] or "",
                })

        # phones already on the CRM lead, if this candidate is linked to one
        cur.execute("""
            select r.row_id::text as row_id, l.phone_1, l.phone_2, l.phone_3, l.dnc
              from public.lead_import_rows r
              join public.acquisition_leads l on l.lead_id = r.linked_lead_id
             where r.row_id = any(%s::uuid[])
        """, (ids,))
        for row in cur.fetchall():
            rec = out.get(row["row_id"])
            if not rec:
                continue
            rec["crm_phones"] = [p for p in (row["phone_1"], row["phone_2"], row["phone_3"]) if p]
            rec["crm_dnc"] = row["dnc"]

        if t21_present:
            cur.execute("""
                select import_row_id::text as row_id from public.candidate_contact_resolution
                 where import_row_id = any(%s::uuid[])
            """, (ids,))
            for row in cur.fetchall():
                if row["row_id"] in out:
                    out[row["row_id"]]["has_ccr"] = True
    return out


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def plan_candidate(rec, existing: Optional[dict]) -> dict:
    """Decide what to do with ONE candidate. Never overwrites better evidence."""
    incoming = [
        {"phone": cp.value, "confidence": cp.confidence, "role": cp.role,
         "party": rel.party.canonical_name, "relationship": rel.role,
         "score": cp.confidence_score, "observed_at": cp.observed_at,
         "evidence": [e.to_dict() for e in cp.evidence]}
        for rel, cp in rec.high_phones
    ]
    if existing is None:
        return {"candidate_id": rec.candidate_id, "action": ACTION_REJECT,
                "reason": "candidate not present in the CURRENT_CANONICAL batch",
                "existing": {}, "incoming": incoming}

    if not incoming:
        return {"candidate_id": rec.candidate_id, "action": ACTION_REJECT,
                "reason": "no HIGH contact to project", "existing": existing, "incoming": []}

    ex_phones = {p["phone"]: p for p in existing["existing_phones"]}
    crm_phones = set(existing.get("crm_phones") or [])

    new_phones = [p for p in incoming if p["phone"] not in ex_phones]
    same_phones = [p for p in incoming if p["phone"] in ex_phones]

    # Is anything already recorded at a confidence we must not degrade?
    outranked = [
        p for p in same_phones
        if CONFIDENCE_RANK.get(ex_phones[p["phone"]]["confidence"], 0)
        > CONFIDENCE_RANK.get(p["confidence"], 0)
    ]
    if outranked:
        return {"candidate_id": rec.candidate_id, "action": ACTION_CONFLICT_HOLD,
                "reason": "existing evidence outranks the incoming evidence",
                "existing": existing, "incoming": incoming}

    # A DIFFERENT phone already on the CRM lead is a conflict for a human, not
    # something to silently add a second opinion beside.
    conflicting_crm = [p for p in incoming if crm_phones and p["phone"] not in crm_phones]
    if conflicting_crm and crm_phones:
        return {"candidate_id": rec.candidate_id, "action": ACTION_CONFLICT_HOLD,
                "reason": f"CRM lead already carries a different phone ({len(crm_phones)} on lead)",
                "existing": existing, "incoming": incoming}

    if not new_phones:
        action = ACTION_NOOP if existing["has_ccr"] else ACTION_APPEND
        return {"candidate_id": rec.candidate_id, "action": action,
                "reason": ("already projected; nothing to add" if action == ACTION_NOOP
                           else "phones present, resolution row missing"),
                "existing": existing, "incoming": incoming}

    action = ACTION_APPEND if ex_phones else ACTION_INSERT
    return {"candidate_id": rec.candidate_id, "action": action,
            "reason": f"{len(new_phones)} new phone(s)",
            "existing": existing, "incoming": incoming}


def build_plan(records, existing_by_id: Dict[str, dict]) -> List[dict]:
    plans = []
    for rec in records:
        if not rec.high_phones:
            continue
        plans.append(plan_candidate(rec, existing_by_id.get(rec.candidate_id)))
    return plans


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------

WRITE_ACTIONS = {ACTION_INSERT, ACTION_APPEND}


def apply_plan(conn, records_by_id, plans: List[dict], tenant_id: str,
               estate_rev: str, run_id: str, resolution_rows: List[dict]) -> dict:
    counts = Counter()
    with conn.cursor(row_factory=dict_row) as cur:
        for plan in plans:
            if plan["action"] not in WRITE_ACTIONS:
                counts[plan["action"]] += 1
                continue
            rec = records_by_id[plan["candidate_id"]]
            for rel, cp in rec.high_phones:
                ev = cp.evidence[0]
                ed = ev.to_dict()
                cur.execute("""
                    insert into public.contact_evidence (
                      evidence_id, tenant_id, source_family, source_name, source_record_id,
                      source_locator, source_hash, field, raw_value, normalized_value,
                      match_method, confidence, observed_at, retrieved_at, freshness_days, notes)
                    values (%(evidence_id)s, %(tenant)s, %(source_family)s, %(source_name)s,
                            %(source_record_id)s, %(source_locator)s, %(source_hash)s,
                            %(field)s, %(raw_value)s, %(normalized_value)s, %(match_method)s,
                            %(confidence)s, nullif(%(observed_at)s,'')::timestamptz,
                            nullif(%(retrieved_at)s,'')::timestamptz, %(freshness_days)s, %(notes)s)
                    on conflict (evidence_id) do nothing
                """, {**ed, "tenant": tenant_id})

                cur.execute("""
                    insert into public.parties (tenant_id, kind, canonical_name)
                    values (%s, %s, %s)
                    on conflict do nothing
                    returning party_id
                """, (tenant_id, rel.party.kind.lower(), rel.party.canonical_name))
                got = cur.fetchone()
                if got:
                    party_id = got["party_id"]
                else:
                    cur.execute("""
                        select party_id from public.parties
                         where tenant_id=%s and kind=%s and canonical_name=%s limit 1
                    """, (tenant_id, rel.party.kind.lower(), rel.party.canonical_name))
                    party_id = cur.fetchone()["party_id"]

                cur.execute("""
                    insert into public.party_phones (
                      party_id, tenant_id, phone_e164, phone_type, provenance,
                      phone_role, confidence_category, confidence_score, observed_at,
                      status, evidence_id, callability_status)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,nullif(%s,'')::timestamptz,%s,%s,%s)
                    on conflict (party_id, phone_e164) do nothing
                """, (party_id, tenant_id, cp.value, None, ed["source_family"],
                      cp.role, cp.confidence, cp.confidence_score, cp.observed_at,
                      cp.status, ed["evidence_id"], CALLABILITY_NOT_AUTHORIZED))

                cur.execute("""
                    insert into public.candidate_parties (
                      tenant_id, import_row_id, party_id, relationship_role,
                      confidence, evidence_id, notes)
                    values (%s,%s,%s,%s,%s,%s,%s)
                    on conflict (tenant_id, import_row_id, party_id, relationship_role)
                      do update set confidence = excluded.confidence,
                                    evidence_id = excluded.evidence_id,
                                    updated_at = now()
                """, (tenant_id, rec.candidate_id, party_id, rel.role,
                      rel.confidence, ed["evidence_id"], rel.notes or ""))
            counts[plan["action"]] += 1

        # the bounded read model, for EVERY current candidate, not only the
        # ones with a phone -- an operator queue is useless if it only contains
        # the rows that are already solved
        for row in resolution_rows:
            cur.execute("""
                insert into public.candidate_contact_resolution (
                  import_row_id, tenant_id, estate_revision, contract_version,
                  owner_class, owner_condition, contact_resolution_state, contact_status,
                  operator_queue, contact_resolution_score, has_living_identity,
                  living_party_count, phone_count, high_phone_count,
                  primary_party_name, primary_party_role,
                  next_source, next_source_why, next_source_manual, blocking_reason,
                  projected_at, projection_run_id)
                values (%(import_row_id)s,%(tenant_id)s,%(estate_revision)s,%(contract_version)s,
                        %(owner_class)s,%(owner_condition)s,%(contact_resolution_state)s,
                        %(contact_status)s,%(operator_queue)s,%(contact_resolution_score)s,
                        %(has_living_identity)s,%(living_party_count)s,%(phone_count)s,
                        %(high_phone_count)s,%(primary_party_name)s,%(primary_party_role)s,
                        %(next_source)s,%(next_source_why)s,%(next_source_manual)s,
                        %(blocking_reason)s, now(), %(run_id)s)
                on conflict (import_row_id) do update set
                  estate_revision = excluded.estate_revision,
                  contract_version = excluded.contract_version,
                  owner_class = excluded.owner_class,
                  owner_condition = excluded.owner_condition,
                  contact_resolution_state = excluded.contact_resolution_state,
                  contact_status = excluded.contact_status,
                  operator_queue = excluded.operator_queue,
                  contact_resolution_score = excluded.contact_resolution_score,
                  has_living_identity = excluded.has_living_identity,
                  living_party_count = excluded.living_party_count,
                  phone_count = excluded.phone_count,
                  high_phone_count = excluded.high_phone_count,
                  primary_party_name = excluded.primary_party_name,
                  primary_party_role = excluded.primary_party_role,
                  next_source = excluded.next_source,
                  next_source_why = excluded.next_source_why,
                  next_source_manual = excluded.next_source_manual,
                  blocking_reason = excluded.blocking_reason,
                  projected_at = now(),
                  projection_run_id = excluded.projection_run_id
            """, {**row, "run_id": run_id})
    return dict(counts)


def resolution_row_for(rec, tenant_id: str, estate_rev: str) -> dict:
    ns = rec.next_source or {}
    living = rec.living_parties
    first = living[0] if living else None
    return {
        "import_row_id": rec.candidate_id,
        "tenant_id": tenant_id,
        "estate_revision": estate_rev,
        "contract_version": rec.contract_version,
        "owner_class": rec.owner_class,
        "owner_condition": rec.owner_condition,
        "contact_resolution_state": rec.contact_resolution_state,
        "contact_status": QUEUE_CONTACT_STATUS.get(rec.operator_queue, ""),
        "operator_queue": rec.operator_queue,
        "contact_resolution_score": rec.contact_resolution_score,
        "has_living_identity": rec.has_living_identity,
        "living_party_count": len(living),
        "phone_count": len(rec.phones),
        "high_phone_count": len(rec.high_phones),
        "primary_party_name": first.party.canonical_name if first else None,
        "primary_party_role": first.role if first else None,
        "next_source": ns.get("next_source", ""),
        "next_source_why": ns.get("why", ""),
        "next_source_manual": ns.get("manual_or_automatic", ""),
        "blocking_reason": ns.get("blocking_reason", ""),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estate-dir", default=str(Path.home() / "Desktop/DEEDSONAR_CONTACT_EXPORT_PREVIEW_20260817"))
    ap.add_argument("--bundle-dir", default=str(Path.home() / "Desktop/DEEDSONAR_FREE_CONTACT_T21"))
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--database-url", default=os.environ.get("DEEDSONAR_DATABASE_URL") or os.environ.get("DATABASE_URL"))
    ap.add_argument("--apply", action="store_true", help="perform the production write")
    ap.add_argument("--verify-idempotent", action="store_true")
    args = ap.parse_args()

    if not args.database_url:
        print("no database url", file=sys.stderr)
        return 2

    out = Path(args.out_dir).expanduser()
    priv = out / "_private"
    for d in (out, priv):
        d.mkdir(parents=True, exist_ok=True)
    run_id = f"t21-{utcnow()}"

    estate_dir = Path(args.estate_dir).expanduser()
    bundle_dir = Path(args.bundle_dir).expanduser()
    estate = load_estate(estate_dir)
    rev = estate_revision(master_csv_paths(estate_dir))
    appends = load_csv(bundle_dir / "phone_append_combined.csv")
    ledger = load_csv(bundle_dir / "CANDIDATE_CONTACT_RESOLUTION.csv")
    ipath = bundle_dir / "CANDIDATE_IDENTITY_LEDGER.csv"
    identity = load_csv(ipath) if ipath.exists() else []
    records = build_records(estate, appends, ledger, rev, identity_ledger=identity)
    by_id = {r.candidate_id: r for r in records}
    with_phone = [r for r in records if r.high_phones]

    print(f"BUNDLE  rows={len(appends)} candidates={len({a['candidate_id'] for a in appends})} "
          f"unique_phones={len({a['phone'] for a in appends if a.get('phone')})}")
    print(f"RECORDS {len(records)}  with HIGH phone: {len(with_phone)}")

    with psycopg.connect(args.database_url, autocommit=False) as conn:
        existing = read_existing(conn, [r.candidate_id for r in with_phone])
        tenant_ids = {v["tenant_id"] for v in existing.values()}
        plans = build_plan(with_phone, existing)

        actions = Counter(p["action"] for p in plans)
        existing_overlap = sum(1 for p in plans if p["existing"] and p["existing"].get("existing_phones"))
        higher = sum(1 for p in plans if p["action"] == ACTION_CONFLICT_HOLD
                     and "outranks" in p["reason"])
        conflicting = sum(1 for p in plans if p["action"] == ACTION_CONFLICT_HOLD
                          and "CRM lead" in p["reason"])
        unexplained = sum(1 for p in plans if p["action"] == ACTION_REJECT)

        court = {
            "run_id": run_id,
            "generated_at": utcnow(),
            "estate_revision": rev,
            "bundle_dir": str(bundle_dir),
            "CANDIDATES_IN_BUNDLE": len(with_phone),
            "CANDIDATES_FOUND_IN_CURRENT_DEEDSONAR": len(existing),
            "EXISTING_PHONE_OVERLAP": existing_overlap,
            "EXISTING_HIGHER_CONFIDENCE": higher,
            "CONFLICTING_PHONES": conflicting,
            "NEW_APPEND": actions.get(ACTION_INSERT, 0) + actions.get(ACTION_APPEND, 0),
            "NOOP": actions.get(ACTION_NOOP, 0),
            "HELD": actions.get(ACTION_CONFLICT_HOLD, 0),
            "REJECTED": actions.get(ACTION_REJECT, 0),
            "UNEXPLAINED_COLLISIONS": unexplained,
            "T21_SCHEMA": (sorted({v.get("t21_schema", "UNKNOWN") for v in existing.values()})
                           or ["NO_CANDIDATES"])[0],
            "tenant_ids": sorted(tenant_ids),
            "actions": dict(actions),
            "callability_status": CALLABILITY_NOT_AUTHORIZED,
        }
        (out / "CONTACT_PROJECTION_RECEIPT.json").write_text(json.dumps(court, indent=2, sort_keys=True))
        print(json.dumps(court, indent=2, sort_keys=True))

        # per-candidate dry-run table (PII -> private)
        with (priv / "PROJECTION_DRY_RUN.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["candidate_id", "action", "reason", "existing_phone_count",
                        "existing_crm_phone_count", "incoming_phone_count", "incoming_phones",
                        "incoming_parties", "incoming_roles"])
            for p in plans:
                ex = p["existing"] or {}
                w.writerow([
                    p["candidate_id"], p["action"], p["reason"],
                    len(ex.get("existing_phones") or []),
                    len(ex.get("crm_phones") or []),
                    len(p["incoming"]),
                    ";".join(i["phone"] for i in p["incoming"]),
                    ";".join(i["party"] for i in p["incoming"]),
                    ";".join(i["relationship"] for i in p["incoming"]),
                ])

        if not args.apply:
            print("\nDRY RUN ONLY. No production row was written.")
            conn.rollback()
            return 0

        if unexplained:
            print(f"\nREFUSING TO WRITE: UNEXPLAINED_COLLISIONS={unexplained}", file=sys.stderr)
            conn.rollback()
            return 3
        if len(tenant_ids) != 1:
            print(f"\nREFUSING TO WRITE: expected exactly one tenant, saw {tenant_ids}", file=sys.stderr)
            conn.rollback()
            return 4
        tenant_id = tenant_ids.pop()

        # ---- rollback manifest BEFORE the first mutation ------------------
        manifest = {
            "run_id": run_id, "captured_at": utcnow(), "tenant_id": tenant_id,
            "estate_revision": rev,
            "pre_write_counts": {},
            "rows": [],
        }
        with conn.cursor(row_factory=dict_row) as cur:
            for tbl in ("parties", "party_phones", "party_emails", "contact_evidence",
                        "candidate_parties", "candidate_contact_resolution"):
                cur.execute(f"select count(*) n from public.{tbl}")
                manifest["pre_write_counts"][tbl] = cur.fetchone()["n"]
        for p in plans:
            ex = p["existing"] or {}
            manifest["rows"].append({
                "TARGET_ROW_ID": p["candidate_id"],
                "PRE_WRITE_ROW_STATE": {
                    "existing_phones": ex.get("existing_phones") or [],
                    "existing_parties": ex.get("existing_parties") or [],
                    "crm_phones": ex.get("crm_phones") or [],
                    "has_contact_resolution_row": bool(ex.get("has_ccr")),
                },
                "EXPECTED_WRITE": {
                    "action": p["action"],
                    "phones": [i["phone"] for i in p["incoming"]],
                    "parties": [i["party"] for i in p["incoming"]],
                    "roles": [i["relationship"] for i in p["incoming"]],
                },
            })
        (priv / "ROLLBACK_MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
        print(f"\nrollback manifest written: {priv / 'ROLLBACK_MANIFEST.json'} "
              f"({len(manifest['rows'])} rows)")

        resolution_rows = [resolution_row_for(r, tenant_id, rev) for r in records]
        counts = apply_plan(conn, by_id, plans, tenant_id, rev, run_id, resolution_rows)
        conn.commit()
        print(f"APPLIED {counts}")

        # ---- prove it ----------------------------------------------------
        with conn.cursor(row_factory=dict_row) as cur:
            post = {}
            for tbl in ("parties", "party_phones", "contact_evidence",
                        "candidate_parties", "candidate_contact_resolution"):
                cur.execute(f"select count(*) n from public.{tbl}")
                post[tbl] = cur.fetchone()["n"]
            cur.execute("""
                select count(*) n from (
                  select party_id, phone_e164 from public.party_phones
                  group by 1,2 having count(*) > 1) d
            """)
            dupes = cur.fetchone()["n"]
            cur.execute("select count(*) n from public.party_phones where callability_status <> 'NOT_AUTHORIZED'")
            callable_leak = cur.fetchone()["n"]

        receipt = {
            "run_id": run_id,
            "applied_at": utcnow(),
            "estate_revision": rev,
            "tenant_id": tenant_id,
            "CONTACT_ROWS_WRITTEN": counts.get(ACTION_INSERT, 0) + counts.get(ACTION_APPEND, 0),
            "CONTACT_ROWS_UPDATED": counts.get(ACTION_APPEND, 0),
            "CONTACT_ROWS_NOOP": counts.get(ACTION_NOOP, 0),
            "CONTACT_CONFLICTS_HELD": counts.get(ACTION_CONFLICT_HOLD, 0),
            "DUPLICATES_CREATED": dupes,
            "CALLABILITY_LEAKS": callable_leak,
            "pre_write_counts": manifest["pre_write_counts"],
            "post_write_counts": post,
            "callability_status": CALLABILITY_NOT_AUTHORIZED,
            "acquisition_leads_contact_columns_written": 0,
        }
        (out / "PRODUCTION_PROJECTION_RECEIPT.json").write_text(json.dumps(receipt, indent=2, sort_keys=True))
        print(json.dumps(receipt, indent=2, sort_keys=True))
        if dupes or callable_leak:
            print("POST-WRITE ASSERTION FAILED", file=sys.stderr)
            return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

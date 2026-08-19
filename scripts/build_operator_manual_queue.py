#!/usr/bin/env python3
"""Turn "research probate" into assembly-line work.

A manual queue that says "research the estate" is a wish. This one names the
office, the exact record, the field to capture, which relationship counts as
proof and where the answer goes back in, so the person doing it never has to
reconstruct the method.

The queue is grouped by the unit of work, not by parcel: one entity that owns
89 parcels is ONE row here, because resolving it once unlocks all 89.

No PII: this writes county, APN, owner string and instructions. Names of living
people stay in custody.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COLUMNS = [
    "work_unit", "unit_key", "priority", "county", "candidates_unlocked",
    "qualified_unlocked", "best_rank", "subject", "official_source",
    "where_to_go", "what_to_search", "what_to_capture", "what_counts_as_proof",
    "what_does_not_count", "where_to_import", "expected_effort", "cost_usd",
]

ESTATE_PLAYBOOK = {
    "official_source": "Clerk of Superior Court — Estates Division, county of the decedent",
    "where_to_go": "The county Clerk of Superior Court estates index, in person or via the "
                   "county's public terminal. NC eCourts is free but operator-only by its "
                   "terms; it is not scripted here for that reason.",
    "what_to_search": "The decedent's name as it appears on the parcel roll, plus the "
                      "county and the APN to confirm the estate names this property.",
    "what_to_capture": "Estate file number; the personal representative / executor / "
                       "administrator as NAMED in the appointment; their service address; "
                       "the date of appointment; the attorney of record if one is listed.",
    "what_counts_as_proof": "A role the FILE STATES: executor, administrator, personal "
                            "representative, or an heir named in a filed heirship "
                            "determination.",
    "what_does_not_count": "A shared surname. A person at the same address. The party who "
                           "receives the tax bill. None of those establish heirship, and "
                           "recording one as HEIR puts a guess in a legal column.",
    "where_to_import": "landos-engine import_packages/estate/estate_import_template.csv, "
                       "then scripts/project_contacts_to_deedsonar.py --apply",
    "expected_effort": "HIGH",
}

ENTITY_PLAYBOOK = {
    "official_source": "NC Secretary of State — Business Registration",
    "where_to_go": "sosnc.gov business search, by hand. The search prohibits automated "
                   "querying, and the bulk data set is a paid FTP subscription.",
    "what_to_search": "The entity's legal name exactly as it appears on the parcel roll, "
                      "then open the entity detail page.",
    "what_to_capture": "SOS entity ID; status; registered agent and their address; "
                       "officers, managers or members with their titles; the principal "
                       "office address.",
    "what_counts_as_proof": "A person the FILING names, with the title the filing gives "
                            "them: manager, member, officer, principal.",
    "what_does_not_count": "The registered agent as the owner -- an agent is a service "
                           "address, frequently a law firm or a formation service, and is "
                           "never the seller.",
    "where_to_import": "landos-engine import_packages/entity/sos_import_template.csv, "
                       "then scripts/project_entity_identities.py --apply",
    "expected_effort": "MEDIUM",
}

TRUST_PLAYBOOK = {
    "official_source": "County Register of Deeds — the recorded instrument",
    "where_to_go": "The county Register of Deeds index for the parcel's book and page.",
    "what_to_search": "The instrument that vested title in the trust, and any later "
                      "instrument naming or substituting a trustee.",
    "what_to_capture": "The trust's exact name; the trustee named in the instrument; the "
                       "date; the instrument type.",
    "what_counts_as_proof": "An instrument that names a trust FIDUCIARY -- trustee of the "
                            "trust that holds title.",
    "what_does_not_count": "The trustee under a deed of trust. In NC foreclosure that is "
                           "the lender's substitute trustee, a completely different party "
                           "from the trustee of a living trust, and the two share a word "
                           "and nothing else.",
    "where_to_import": "landos-engine import_packages/estate/estate_import_template.csv "
                       "with relationship=TRUSTEE",
    "expected_effort": "MEDIUM",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.home() / "neuruh-workspace"
                                         / "T24A_X_RESOLUTION_INTELLIGENCE_V3_20260819"
                                         / "OPERATOR_MANUAL_QUEUE.csv"))
    args = ap.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    rows = []
    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        # ENTITY: one row per entity, ranked by what resolving it unlocks.
        cur.execute("""
            select e.normalized_name, e.legal_name, e.entity_class, e.resolution_state,
                   count(cei.import_row_id)::int as candidates,
                   count(*) filter (where r.candidate_class = 'QUALIFIED_CANDIDATE')::int as qualified,
                   min(r.global_rank) as best_rank,
                   min(coalesce(r.normalized ->> 'county', '')) as county
              from public.entity_identity e
              join public.candidate_entity_identity cei
                on cei.entity_identity_id = e.entity_identity_id
              join public.lead_import_rows r on r.row_id = cei.import_row_id
             where e.resolution_state in ('ENTITY_IDENTITY_REQUIRED',
                                          'ENTITY_OFFICIAL_ADDRESS_FOUND')
             group by e.entity_identity_id
             order by qualified desc, candidates desc, best_rank nulls last
        """)
        for r in cur.fetchall():
            rows.append({
                "work_unit": "ENTITY", "unit_key": r["normalized_name"],
                "priority": 1 if r["qualified"] else 2,
                "county": r["county"], "candidates_unlocked": r["candidates"],
                "qualified_unlocked": r["qualified"], "best_rank": r["best_rank"],
                "subject": r["legal_name"], "cost_usd": "0.00", **ENTITY_PLAYBOOK,
            })

        # ESTATE: one row per candidate, because each decedent is their own file.
        cur.execute("""
            select c.import_row_id::text as candidate_id,
                   coalesce(r.normalized ->> 'county', '') as county,
                   coalesce(r.normalized ->> 'apn', '') as apn,
                   coalesce(r.normalized ->> 'ownerName', '') as owner_name,
                   r.candidate_class, r.global_rank
              from public.candidate_contact_resolution c
              join public.lead_import_rows r on r.row_id = c.import_row_id
             where c.estate_resolution_state = 'ESTATE_IDENTITY_REQUIRED'
             order by (r.candidate_class = 'QUALIFIED_CANDIDATE') desc,
                      r.global_rank nulls last
        """)
        for r in cur.fetchall():
            qualified = r["candidate_class"] == "QUALIFIED_CANDIDATE"
            rows.append({
                "work_unit": "ESTATE", "unit_key": f"{r['county']}:{r['apn']}",
                "priority": 1 if qualified else 3,
                "county": r["county"], "candidates_unlocked": 1,
                "qualified_unlocked": 1 if qualified else 0,
                "best_rank": r["global_rank"],
                "subject": r["owner_name"], "cost_usd": "0.00", **ESTATE_PLAYBOOK,
            })

        # TRUST
        cur.execute("""
            select c.import_row_id::text as candidate_id,
                   coalesce(r.normalized ->> 'county', '') as county,
                   coalesce(r.normalized ->> 'apn', '') as apn,
                   coalesce(r.normalized ->> 'ownerName', '') as owner_name,
                   r.candidate_class, r.global_rank
              from public.candidate_contact_resolution c
              join public.lead_import_rows r on r.row_id = c.import_row_id
             where c.operator_queue = 'TRUSTEE_REQUIRED'
             order by (r.candidate_class = 'QUALIFIED_CANDIDATE') desc,
                      r.global_rank nulls last
        """)
        for r in cur.fetchall():
            qualified = r["candidate_class"] == "QUALIFIED_CANDIDATE"
            rows.append({
                "work_unit": "TRUST", "unit_key": f"{r['county']}:{r['apn']}",
                "priority": 1 if qualified else 3,
                "county": r["county"], "candidates_unlocked": 1,
                "qualified_unlocked": 1 if qualified else 0,
                "best_rank": r["global_rank"],
                "subject": r["owner_name"], "cost_usd": "0.00", **TRUST_PLAYBOOK,
            })

    rows.sort(key=lambda r: (r["priority"], -r["qualified_unlocked"],
                             -r["candidates_unlocked"]))
    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})

    summary = {"generated_at": utcnow(), "rows": len(rows), "out": str(out)}
    for unit in ("ENTITY", "ESTATE", "TRUST"):
        sub = [r for r in rows if r["work_unit"] == unit]
        summary[unit] = {
            "work_items": len(sub),
            "candidates_unlocked": sum(r["candidates_unlocked"] for r in sub),
            "qualified_unlocked": sum(r["qualified_unlocked"] for r in sub),
        }
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

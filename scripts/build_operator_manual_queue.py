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
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contact_authority import contact_dsn_or_exit  # noqa: E402

#: The Qualified work queue. One row per Qualified opportunity that is still
#: unresolved, ordered the way the operator actually picks work up: by grade,
#: then by rank inside the grade. Everything needed to do the job is on the row,
#: including what has ALREADY been tried, so nobody re-runs an exhausted source.
QUALIFIED_COLUMNS = [
    "grade", "global_rank", "opportunity_score", "property_address", "county", "apn",
    "recorded_owner", "owner_class", "owner_condition", "why_opportunity",
    "what_is_already_known", "what_source_already_ran", "next_source",
    "next_source_manual", "where_to_go", "exact_record_needed", "exact_fact_to_capture",
    "what_counts_as_proof", "what_does_not_count_as_proof", "where_to_write_result_back",
    "expected_unlock", "blocker", "callability", "candidate_id",
]

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


UNKNOWN_PLAYBOOK = {
    "official_source": "Triage — the owner string does not classify",
    "where_to_go": "The county parcel roll record for this APN, read by hand.",
    "what_to_search": "The owner-of-record string exactly as the roll prints it.",
    "what_to_capture": "Whether the owner is a person, an estate, a trust or an entity, "
                       "and any death or fiduciary marker in the string.",
    "what_counts_as_proof": "The roll's own owner field.",
    "what_does_not_count": "A guess from the address or from a similar name elsewhere.",
    "where_to_import": "re-run owner classification once the class is known",
    "expected_effort": "LOW",
}

PLAYBOOK_BY_QUEUE = {
    "ESTATE_IDENTITY_REQUIRED": ESTATE_PLAYBOOK,
    "ESTATE_IDENTITY_FOUND_CONTACT_NEEDED": ESTATE_PLAYBOOK,
    "SOS_ENTITY_IDENTITY_REQUIRED": ENTITY_PLAYBOOK,
    "ENTITY_IDENTITY_FOUND_CONTACT_NEEDED": ENTITY_PLAYBOOK,
    "ENTITY_PRINCIPAL_FOUND_CONTACT_NEEDED": ENTITY_PLAYBOOK,
    "TRUSTEE_REQUIRED": TRUST_PLAYBOOK,
}

#: Why this parcel is an opportunity at all, said in the operator's terms.
WHY_BY_CONDITION = {
    "DECEASED": "The roll's owner of record is dead. Title has to move through an "
                "estate or an heir before anyone can sell, and nobody has asked yet.",
    "HEIRS_UNRESOLVED": "The roll names HEIRS rather than a person. The parcel has "
                        "passed by intestacy and the heirs have never been adjudicated.",
    "ESTATE_OPEN": "An estate is open on the owner of record. There is a named "
                   "fiduciary with authority to convey.",
    "LIVING": "The owner is living and the parcel still qualified on its distress "
              "signals, so the approach is direct once a contact exists.",
    "ENTITY_ACTIVE": "An active entity holds title. One registry lookup names the "
                     "human, and that human usually controls other parcels too.",
    "TRUST_HELD": "A trust holds title. The trustee of the trust -- not a deed-of-trust "
                  "trustee -- is the only party who can convey.",
    "UNKNOWN": "The owner string does not classify. It needs a human eye before any "
               "route can be chosen.",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def qualified_queue(cur, negative_memory: dict) -> list:
    """One row per unresolved Qualified opportunity, ordered by grade then rank."""
    cur.execute("""
        select c.import_row_id::text as candidate_id,
               r.priority_tier as grade, r.global_rank, r.opportunity_score,
               coalesce(r.normalized ->> 'county', '') as county,
               coalesce(r.normalized ->> 'apn', '') as apn,
               coalesce(r.normalized ->> 'propertyAddress', '') as property_address,
               coalesce(r.normalized ->> 'ownerName', '') as recorded_owner,
               c.owner_class, c.owner_condition, c.operator_queue,
               c.contact_resolution_state, c.living_party_count, c.high_phone_count,
               c.primary_party_role, c.next_source, c.next_source_manual,
               -- T30. HOW the parties on this candidate were established. The
               -- row used to say every living party was "named by an official
               -- roll", which was true when the roll was the only supply and
               -- became false the moment household evidence arrived. An
               -- operator told the wrong source cannot judge the fact.
               (select string_agg(distinct cp.relationship_class, ',')
                  from public.candidate_parties cp
                 where cp.import_row_id = c.import_row_id) as party_classes,
               (select count(*) from public.candidate_parties cp
                 where cp.import_row_id = c.import_row_id
                   and cp.relationship_class = 'LEGAL_AUTHORITY') as authority_count,
               coalesce(c.blocking_reason, '') as blocking_reason,
               coalesce(c.callability_status, 'NOT_AUTHORIZED') as callability,
               e.normalized_name as entity_name,
               (select count(*) from public.candidate_entity_identity x
                 where x.entity_identity_id = c.entity_identity_id) as entity_parcels
          from public.candidate_contact_resolution c
          join public.lead_import_rows r on r.row_id = c.import_row_id
          left join public.entity_identity e
                 on e.entity_identity_id = c.entity_identity_id
         where r.candidate_class = 'QUALIFIED_CANDIDATE'
           and coalesce(c.high_phone_count, 0) = 0
    """)
    out = []
    for r in cur.fetchall():
        pb = PLAYBOOK_BY_QUEUE.get(r["operator_queue"], UNKNOWN_PLAYBOOK)
        ran = sorted(negative_memory.get(r["candidate_id"], set()))
        known = []
        classes = set((r["party_classes"] or "").split(",")) - {""}
        if (r["living_party_count"] or 0) > 0:
            # Name the class of evidence, not a source that may not be the one.
            if classes & {"PROPERTY_INTEREST"}:
                how = "named on the property record"
            elif classes & {"ROLE_STATED_ON_RECORD"}:
                how = "a role stated on a record, not adjudicated"
            elif classes & {"FAMILY"}:
                how = "family evidence only -- no authority over the property"
            else:
                how = "established on corroborated evidence"
            known.append(f"{r['living_party_count']} living part"
                         f"{'y' if r['living_party_count'] == 1 else 'ies'} known "
                         f"({how}), as {r['primary_party_role'] or 'a related party'}")
        else:
            known.append("no living party named yet")
        # The whole point of the estate route, said as a number: nobody has
        # legal authority here until the file is read.
        known.append(f"{r['authority_count'] or 0} party with adjudicated legal authority"
                     if (r["authority_count"] or 0) == 1 else
                     f"{r['authority_count'] or 0} parties with adjudicated legal authority")
        known.append(f"resolution state {r['contact_resolution_state']}")
        if r["entity_name"] and (r["entity_parcels"] or 0) > 1:
            known.append(f"owner entity {r['entity_name']} holds {r['entity_parcels']} parcels")
        unlock = "this parcel"
        if r["entity_name"] and (r["entity_parcels"] or 0) > 1:
            unlock = (f"{r['entity_parcels']} parcels held by {r['entity_name']} -- "
                      f"resolve the entity once, not once per parcel")
        elif (r["living_party_count"] or 0) > 0:
            unlock = "a contact point for a party this parcel already has proven"
        out.append({
            "grade": r["grade"] or "", "global_rank": r["global_rank"],
            "opportunity_score": r["opportunity_score"],
            "property_address": r["property_address"], "county": r["county"],
            "apn": r["apn"], "recorded_owner": r["recorded_owner"],
            "owner_class": r["owner_class"], "owner_condition": r["owner_condition"],
            "why_opportunity": WHY_BY_CONDITION.get(r["owner_condition"], ""),
            "what_is_already_known": "; ".join(known),
            "what_source_already_ran": (", ".join(ran) + " (run, returned nothing)")
                                       if ran else "no automated source has been "
                                                   "recorded as run and empty",
            "next_source": r["next_source"] or "",
            "next_source_manual": r["next_source_manual"] or "",
            "where_to_go": pb["where_to_go"],
            "exact_record_needed": pb["what_to_search"],
            "exact_fact_to_capture": pb["what_to_capture"],
            "what_counts_as_proof": pb["what_counts_as_proof"],
            "what_does_not_count_as_proof": pb["what_does_not_count"],
            "where_to_write_result_back": pb["where_to_import"],
            "expected_unlock": unlock,
            "blocker": r["blocking_reason"], "callability": r["callability"],
            "candidate_id": r["candidate_id"],
        })
    grade_order = {"A": 0, "B": 1, "C": 2}
    out.sort(key=lambda r: (grade_order.get(r["grade"], 9),
                            r["global_rank"] if r["global_rank"] is not None else 10 ** 9))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path.home() / "neuruh-workspace"
                                         / "T24A_X_RESOLUTION_INTELLIGENCE_V3_20260819"
                                         / "OPERATOR_MANUAL_QUEUE.csv"))
    ap.add_argument("--qualified-out", default="",
                    help="Also write the Qualified work queue -- one row per "
                         "unresolved Qualified opportunity, ordered by grade then rank.")
    ap.add_argument("--cascade-dir", action="append", default=[],
                    help="A $0 contact-cascade output directory, so each row can say "
                         "which automated source has already been run and came back "
                         "empty. Repeatable.")
    args = ap.parse_args()

    negative_memory: dict = {}
    for d in args.cascade_dir:
        rej = Path(d).expanduser() / "_rejected.json"
        if not rej.exists():
            continue
        for row in json.loads(rej.read_text()):
            cid = (row.get("candidate_id") or "").strip()
            if cid and row.get("verdict") in {"NO_MATCH", "IDENTITY_CORROBORATED_NO_PHONE"}:
                negative_memory.setdefault(cid, set()).add("NCSBE_PUBLIC_VOTER_FILE")
    # DEEDSONAR_DATABASE_URL, then DATABASE_URL; the retired legacy DB is refused (exit 2).
    dsn = contact_dsn_or_exit()

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

    if args.qualified_out:
        with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            qrows = qualified_queue(cur, negative_memory)
        qout = Path(args.qualified_out).expanduser()
        qout.parent.mkdir(parents=True, exist_ok=True)
        with qout.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=QUALIFIED_COLUMNS)
            w.writeheader()
            for r in qrows:
                w.writerow({c: r.get(c, "") for c in QUALIFIED_COLUMNS})
        by_grade: dict = {}
        for r in qrows:
            by_grade[r["grade"] or "?"] = by_grade.get(r["grade"] or "?", 0) + 1
        summary["QUALIFIED_QUEUE"] = {"rows": len(qrows), "by_grade": by_grade,
                                      "out": str(qout)}
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

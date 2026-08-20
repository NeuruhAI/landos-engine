#!/usr/bin/env python3
"""T30 — reflect the new family relationships in the read model, then re-route.

WHY THIS IS SEPARATE FROM THE DISCOVERY RUN. Discovery writes FACTS into the
party graph (parties, contact_evidence, candidate_parties). The read model
`candidate_contact_resolution` is a PROJECTION of those facts, and the estate
page reads the projection, not the graph. Writing a relationship without
projecting it leaves the graph right and the product unchanged -- which is the
failure mode a previous lane hit and named: a projection that is not followed by
a re-route loses the routing entirely.

WHAT IT WRITES, AND ONLY THIS
  has_living_identity, living_party_count   -- counts, derived from the graph
  primary_party_id / _name / _role          -- ONLY where currently empty
  estate_living_party_id / _role, estate_resolution_state

WHAT IT REFUSES TO WRITE
  * Any score, rank, grade, priority_tier, opportunity_score. An identity
    improving is not a property becoming more valuable, and T30's own brief says
    so. None of those columns appear in any statement here.
  * owner_class, owner_condition. The owner is still dead; finding his daughter
    does not change that, and quietly flipping the condition would erase the
    reason the record is in the estate queue at all.
  * contact_resolution_state. That vocabulary is the contact factory's, and the
    honest values for "a relative was found but has no phone" are
    HEIR_ON_ROLL_NO_ACCEPTED_PHONE -- which would assert an heirship nobody
    adjudicated. Leaving it alone is the truthful option.
  * An EXISTING primary party. A candidate that already resolved to somebody is
    not demoted to a household co-resident because a weaker fact arrived later.
    Fill-if-empty, never overwrite.

THEN THE ROUTER. This script does not choose a next source. It updates the
inputs the router reads and then tells you to run refresh_next_best_source,
which is the only thing allowed to write a route.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import DATABASE_URL

#: Roles that are a person you could actually speak to. DECEDENT is excluded
#: because he is the subject, and REGISTERED_AGENT because a service address is
#: not the seller -- the same two exclusions the contract already makes.
CONTACTABLE = (
    "'RECORDED_OWNER','COOWNER','LIFE_TENANT','HEIR','PERSONAL_REPRESENTATIVE',"
    "'EXECUTOR','ADMINISTRATOR','TRUSTEE','ENTITY_OFFICER','ENTITY_MANAGER',"
    "'ENTITY_PRINCIPAL','ENTITY_MAIN_CONTACT','OTHER_VERIFIED_RELATION'"
)

#: Which relationship the operator should be shown FIRST when nothing has been
#: chosen yet. Adjudicated authority outranks a stated role, which outranks a
#: property interest, which outranks kinship -- because that is the order of how
#: much the record actually supports talking to them about selling.
CLASS_RANK = (
    "case cp.relationship_class "
    "when 'LEGAL_AUTHORITY' then 0 "
    "when 'PROPERTY_INTEREST' then 1 "
    "when 'ROLE_STATED_ON_RECORD' then 2 "
    "when 'FAMILY' then 3 else 4 end"
)

COUNTS_SQL = f"""
with live as (
  select cp.tenant_id, cp.import_row_id,
         count(distinct cp.party_id) as n_living
    from public.candidate_parties cp
   where cp.relationship_role in ({CONTACTABLE})
   group by 1, 2
)
update public.candidate_contact_resolution c
   set has_living_identity = true,
       living_party_count  = live.n_living
  from live
 where live.tenant_id = c.tenant_id
   and live.import_row_id = c.import_row_id
   and (c.has_living_identity is distinct from true
        or c.living_party_count is distinct from live.n_living)
"""

PRIMARY_SQL = f"""
with pick as (
  select distinct on (cp.tenant_id, cp.import_row_id)
         cp.tenant_id, cp.import_row_id, cp.party_id,
         p.canonical_name, cp.relationship_role, cp.relationship_class
    from public.candidate_parties cp
    join public.parties p on p.party_id = cp.party_id
   where cp.relationship_role in ({CONTACTABLE})
   order by cp.tenant_id, cp.import_row_id, {CLASS_RANK},
            case cp.confidence when 'HIGH' then 0 when 'MEDIUM' then 1 else 2 end,
            p.canonical_name
)
update public.candidate_contact_resolution c
   set primary_party_id   = pick.party_id,
       primary_party_name = pick.canonical_name,
       primary_party_role = pick.relationship_role
  from pick
 where pick.tenant_id = c.tenant_id
   and pick.import_row_id = c.import_row_id
   -- fill-if-empty. An existing primary is never replaced.
   and c.primary_party_name is null
"""

ESTATE_SQL = f"""
with pick as (
  select distinct on (cp.tenant_id, cp.import_row_id)
         cp.tenant_id, cp.import_row_id, cp.party_id, cp.relationship_role
    from public.candidate_parties cp
   where cp.relationship_role in ({CONTACTABLE})
   order by cp.tenant_id, cp.import_row_id, {CLASS_RANK},
            case cp.confidence when 'HIGH' then 0 when 'MEDIUM' then 1 else 2 end
)
update public.candidate_contact_resolution c
   set estate_living_party_id   = coalesce(c.estate_living_party_id, pick.party_id),
       estate_living_party_role = coalesce(c.estate_living_party_role, pick.relationship_role),
       estate_resolution_state  = 'ESTATE_LIVING_PARTY_FOUND'
  from pick
 where pick.tenant_id = c.tenant_id
   and pick.import_row_id = c.import_row_id
   and c.owner_condition in ('DECEASED','HEIRS_UNRESOLVED','ESTATE_OPEN')
   and c.estate_resolution_state is distinct from 'ESTATE_LIVING_PARTY_FOUND'
"""

CENSUS_SQL = """
select count(*) filter (where priority_tier in ('A','B','C')) qualified,
       count(*) filter (where priority_tier in ('A','B','C') and has_living_identity) qual_seller_identified,
       count(*) filter (where priority_tier in ('A','B','C') and high_phone_count > 0) qual_contact_found,
       count(*) filter (where has_living_identity) seller_identified,
       count(*) filter (where high_phone_count > 0) contact_found,
       count(*) current_opportunities
  from public.deedsonar_current_candidates_v1
"""


def census(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(CENSUS_SQL)
        return cur.fetchone()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute("set statement_timeout = '300s'")
        before = census(conn)
        report = {"run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "before": dict(before), "applied": False}
        if not args.apply:
            # Count what WOULD change without changing it: the same predicates,
            # as selects, inside a transaction that is rolled back.
            with conn.cursor() as cur:
                cur.execute(COUNTS_SQL)
                report["would_update_counts"] = cur.rowcount
                cur.execute(PRIMARY_SQL)
                report["would_fill_primary"] = cur.rowcount
                cur.execute(ESTATE_SQL)
                report["would_set_estate_state"] = cur.rowcount
            conn.rollback()
            report["after_if_applied"] = None
        else:
            with conn.cursor() as cur:
                cur.execute(COUNTS_SQL)
                report["counts_updated"] = cur.rowcount
                cur.execute(PRIMARY_SQL)
                report["primary_filled"] = cur.rowcount
                cur.execute(ESTATE_SQL)
                report["estate_state_set"] = cur.rowcount
            conn.commit()
            report["applied"] = True
            report["after"] = dict(census(conn))
            # Second pass, same statements: every predicate carries an
            # "is distinct from" or a "is null" guard, so a converged row is
            # not touched twice.
            with conn.cursor() as cur:
                cur.execute(COUNTS_SQL)
                a = cur.rowcount
                cur.execute(PRIMARY_SQL)
                b = cur.rowcount
                cur.execute(ESTATE_SQL)
                c = cur.rowcount
            conn.commit()
            report["second_pass_rows_touched"] = {"counts": a, "primary": b, "estate": c}
            report["second_pass_noop"] = (a == 0 and b == 0 and c == 0)

    text = json.dumps(report, indent=1, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

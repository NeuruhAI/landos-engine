#!/usr/bin/env python3
"""Make the ENTITY the unit of research instead of the parcel.

4,058 candidate rows are owned by an entity, and those rows resolve to roughly
2,446 distinct entities. Working per-candidate means doing the same NC SOS
lookup 89 times for one LLC that owns 89 parcels, and then having 89 places
where the answer might be stale. So the entity gets a record of its own, the
candidates point at it, and one resolution fans out to every parcel it owns.

WHAT IS ASSERTED. Only what a source states. T22's entity evidence carries an
official mailing address for 3,724 candidates and a registry match for 20; a row
whose review_state is not accepted contributes an owner string and a
"still needs an official source" state, which is a real answer, not a blank.

WHAT IS NOT ASSERTED. A registered agent is not the owner. A mailing address is
not a principal. An entity with no registry record is ENTITY_IDENTITY_REQUIRED,
never "resolved with what we had".
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contact_authority import contact_dsn_or_exit  # noqa: E402

from app.contact.entity_public import org_key
from app.contact.owner_class import owner_route_class
from app.custody.sync_guard import prepare_output_dir

ENTITY_CLASSES = {
    "LLC": "LLC", "CORPORATION": "CORPORATION", "NONPROFIT": "NONPROFIT",
    "TRUST": "TRUST", "GOVERNMENT": "GOVERNMENT", "OTHER_ENTITY": "OTHER_ENTITY",
}

ACCEPTED_REVIEW = {"ACCEPT", "ACCEPTED", "EXACT", "HIGH"}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _s(row: dict, key: str) -> str:
    return " ".join(str(row.get(key) or "").strip().split())


def load_csv(path: Path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def entity_class_for(owner_name: str, declared: str) -> str:
    declared = (declared or "").strip().upper()
    if declared in ENTITY_CLASSES:
        return ENTITY_CLASSES[declared]
    route = owner_route_class(owner_name)
    return ENTITY_CLASSES.get(route, "UNKNOWN")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--t22-evidence", default=str(
        Path.home() / "neuruh-workspace" / "T22_PUBLIC_RECORD_DATA_SUPPLY_20260819"
        / "04_NORMALIZED" / "ENTITY_RESOLUTION_EVIDENCE.csv"))
    ap.add_argument("--out-dir", default=str(
        Path.home() / "neuruh-private" / "contact-intelligence" / "runs" / "T24A_ENTITY_FANOUT"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    # DEEDSONAR_DATABASE_URL, then DATABASE_URL; the retired legacy DB is refused (exit 2).
    dsn = contact_dsn_or_exit()
    out = prepare_output_dir(args.out_dir, what="entity fan-out evidence")
    run_id = f"t24a-entity-{utcnow()}"

    t22 = {}
    for row in load_csv(Path(args.t22_evidence).expanduser()):
        cid = _s(row, "candidate_id")
        if cid:
            t22[cid] = row

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                select c.import_row_id::text as candidate_id, c.tenant_id::text as tenant_id,
                       c.owner_class, c.owner_condition, c.operator_queue,
                       coalesce(r.normalized ->> 'ownerName', '') as owner_name,
                       r.candidate_class
                  from public.candidate_contact_resolution c
                  join public.lead_import_rows r on r.row_id = c.import_row_id
                 where c.owner_class in ('LLC','CORPORATION','NONPROFIT','TRUST',
                                         'OTHER_ENTITY','GOVERNMENT')
            """)
            rows = cur.fetchall()

        groups = defaultdict(list)
        for row in rows:
            key = org_key(row["owner_name"]) or row["owner_name"].upper().strip()
            if key:
                groups[key].append(row)

        entities, links = [], []
        states = Counter()
        for key, members in sorted(groups.items()):
            tenant = members[0]["tenant_id"]
            legal = max((m["owner_name"] for m in members), key=len)
            ev = next((t22[m["candidate_id"]] for m in members
                       if m["candidate_id"] in t22
                       and _s(t22[m["candidate_id"]], "entity_match") == "YES"), None)
            fallback = next((t22[m["candidate_id"]] for m in members
                             if m["candidate_id"] in t22), {})
            src = ev or fallback
            has_registry = bool(ev) and _s(ev, "entity_id") != ""
            principal = _s(src, "principal_name")
            agent = _s(src, "registered_agent")
            street = _s(src, "entity_mailing_street")
            if has_registry and principal:
                state = "ENTITY_PRINCIPAL_FOUND"
                nxt, why = ("OPERATOR_CONTACT_ENTITY_PRINCIPAL",
                            "an official registry names a principal for this entity")
            elif has_registry:
                state = "ENTITY_REGISTRY_MATCHED"
                nxt, why = ("NC_SOS_ENTITY_DETAIL_MANUAL",
                            "registry record found; principals/officers still need the "
                            "SOS entity detail page, which prohibits automation")
            elif street:
                state = "ENTITY_OFFICIAL_ADDRESS_FOUND"
                nxt, why = ("NC_SOS_ENTITY_SEARCH_MANUAL",
                            "an official mailing address is known; the registry record "
                            "that names a human is not")
            elif members[0]["owner_class"] == "GOVERNMENT":
                state = "ENTITY_NOT_A_SELLER_CONTACT"
                nxt, why = ("", "a government owner is not a seller contact")
            else:
                state = "ENTITY_IDENTITY_REQUIRED"
                nxt, why = ("NC_SOS_ENTITY_SEARCH_MANUAL",
                            "no free automatable source has produced an official record "
                            "for this entity")
            states[state] += 1
            entities.append({
                "tenant_id": tenant, "normalized_name": key, "legal_name": legal,
                "entity_class": entity_class_for(legal, _s(src, "entity_type")),
                "registry_source": _s(src, "entity_id_type") or "NONE",
                "registry_id": _s(src, "entity_id"),
                "registry_status": _s(src, "entity_status"),
                "official_address": street,
                "official_city": _s(src, "entity_mailing_city"),
                "official_state": _s(src, "entity_mailing_state"),
                "official_zip": _s(src, "entity_mailing_zip"),
                "registered_agent": agent,
                "principal_name": principal,
                "principal_title": _s(src, "principal_role"),
                "resolution_state": state,
                "candidate_count": len(members),
                "next_source": nxt, "next_source_why": why,
            })
            for m in members:
                links.append({"tenant_id": tenant, "import_row_id": m["candidate_id"],
                              "normalized_name": key, "owner_name_raw": m["owner_name"]})

        fanout = sorted(((e["normalized_name"], e["candidate_count"]) for e in entities),
                        key=lambda x: -x[1])
        report = {
            "run_id": run_id, "generated_at": utcnow(),
            "CANDIDATE_ENTITY_ROWS": len(rows),
            "DISTINCT_ENTITIES": len(entities),
            "ENTITIES_RESOLVED": sum(states[s] for s in
                                     ("ENTITY_PRINCIPAL_FOUND", "ENTITY_REGISTRY_MATCHED",
                                      "ENTITY_OFFICIAL_ADDRESS_FOUND")),
            "CANDIDATES_UNLOCKED_BY_ENTITY_FANOUT": len(rows) - len(entities),
            "resolution_states": dict(states),
            "largest_entities": fanout[:15],
            "T22_EVIDENCE_ROWS": len(t22),
        }
        (out / "ENTITY_FANOUT.json").write_text(json.dumps(report, indent=1))
        os.chmod(out / "ENTITY_FANOUT.json", 0o600)
        print(json.dumps({k: v for k, v in report.items() if k != "largest_entities"}, indent=1))

        if not args.apply:
            print("DRY RUN. No entity row written.")
            return 0

        with conn.cursor() as cur:
            cur.execute("""
                create temp table _t24a_entity (
                  tenant_id uuid, normalized_name text, legal_name text, entity_class text,
                  registry_source text, registry_id text, registry_status text,
                  official_address text, official_city text, official_state text,
                  official_zip text, registered_agent text, principal_name text,
                  principal_title text, resolution_state text, candidate_count int,
                  next_source text, next_source_why text) on commit drop
            """)
            cur.execute("""
                create temp table _t24a_entity_link (
                  tenant_id uuid, import_row_id uuid, normalized_name text,
                  owner_name_raw text) on commit drop
            """)
            cols = ("tenant_id, normalized_name, legal_name, entity_class, registry_source, "
                    "registry_id, registry_status, official_address, official_city, "
                    "official_state, official_zip, registered_agent, principal_name, "
                    "principal_title, resolution_state, candidate_count, next_source, "
                    "next_source_why")
            with cur.copy(f"copy _t24a_entity ({cols}) from stdin") as cp:
                for e in entities:
                    cp.write_row(tuple(e[c.strip()] for c in cols.split(",")))
            with cur.copy("copy _t24a_entity_link (tenant_id, import_row_id, normalized_name, "
                          "owner_name_raw) from stdin") as cp:
                for l in links:
                    cp.write_row((l["tenant_id"], l["import_row_id"],
                                  l["normalized_name"], l["owner_name_raw"]))

            cur.execute(f"""
                insert into public.entity_identity ({cols})
                select {cols} from _t24a_entity
                on conflict (tenant_id, normalized_name) do update set
                  legal_name = excluded.legal_name,
                  entity_class = excluded.entity_class,
                  registry_source = excluded.registry_source,
                  registry_id = excluded.registry_id,
                  registry_status = excluded.registry_status,
                  official_address = excluded.official_address,
                  official_city = excluded.official_city,
                  official_state = excluded.official_state,
                  official_zip = excluded.official_zip,
                  registered_agent = excluded.registered_agent,
                  principal_name = excluded.principal_name,
                  principal_title = excluded.principal_title,
                  resolution_state = excluded.resolution_state,
                  candidate_count = excluded.candidate_count,
                  next_source = excluded.next_source,
                  next_source_why = excluded.next_source_why,
                  updated_at = now()
            """)
            cur.execute("""
                insert into public.candidate_entity_identity
                  (tenant_id, import_row_id, entity_identity_id, owner_name_raw)
                select l.tenant_id, l.import_row_id, e.entity_identity_id, l.owner_name_raw
                  from _t24a_entity_link l
                  join public.entity_identity e on e.tenant_id = l.tenant_id
                                               and e.normalized_name = l.normalized_name
                on conflict (import_row_id, entity_identity_id) do nothing
            """)
            cur.execute("""
                update public.candidate_contact_resolution c
                   set entity_identity_id = cei.entity_identity_id
                  from public.candidate_entity_identity cei
                 where cei.import_row_id = c.import_row_id
                   and c.entity_identity_id is distinct from cei.entity_identity_id
            """)
            linked = cur.rowcount
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("select count(*) n from public.entity_identity")
            n_entities = cur.fetchone()["n"]
            cur.execute("select count(*) n from public.candidate_entity_identity")
            n_links = cur.fetchone()["n"]
        receipt = {**report, "applied": True, "entity_rows": n_entities,
                   "candidate_links": n_links, "ccr_rows_linked": linked}
        (out / "ENTITY_FANOUT_RECEIPT.json").write_text(json.dumps(receipt, indent=1))
        os.chmod(out / "ENTITY_FANOUT_RECEIPT.json", 0o600)
        print(json.dumps({"entity_rows": n_entities, "candidate_links": n_links,
                          "ccr_rows_linked": linked}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

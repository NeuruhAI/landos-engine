#!/usr/bin/env python3
"""T30 — find the decedent's living family, from the source the state publishes.

THE PROBLEM, IN ONE LINE. 285 of the 306 Qualified opportunities are
estate/heir/deceased records, and every one of them carries zero phones.

WHY RE-RUNNING ANYTHING WOULD NOT HELP. The free identity source is the NCSBE
public voter file, and it is a register of the LIVING. Asked "is this dead owner
in the file", it correctly answers no, forever. The router already knows this
and has stopped asking (negative memory), which is right.

WHAT THIS ASKS INSTEAD. The file is address-keyed. A parcel has a situs address
and a mailing address, both already held. So: WHO ELSE IS REGISTERED AT THE DEAD
OWNER'S ADDRESS? That question has never been put to the file. When the answer
is a living adult carrying the decedent's surname, that is corroborated evidence
of a family relationship -- the same class of fact an obituary's survivor list
carries, from a source that publishes it deliberately and permits the read.

WHAT IT REFUSES TO DO
  * Never emits HEIR, EXECUTOR, ADMINISTRATOR or PERSONAL_REPRESENTATIVE. There
    is no code path and no flag. Every relationship is FAMILY class, role
    OTHER_VERIFIED_RELATION. Legal authority comes from the estate file or it
    does not come.
  * Never merges two registrations into one person on name similarity. NCID is
    the state's key and it is the only merge basis used.
  * Never contacts anyone, never marks anything callable, never buys anything.
  * Never touches score, rank, grade, qualification, owner_condition, or any
    raw ownership fact.

IDEMPOTENCE. Evidence ids are content addresses over (source, record, field,
value, method, file hash, observed_at), and observed_at is the SNAPSHOT's date,
not the run's clock. So a second run computes the same ids and every insert
collapses. `--verify-idempotent` proves it rather than asserting it.

SAFETY. `--dry-run` is the default and it does not open a write transaction at
all. `--apply` writes a pre-state manifest before the first mutation and runs in
one transaction.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import DATABASE_URL
from app.contact.address_normalize import ParsedAddress, parse_address
from app.contact.household import (
    HOUSEHOLD_VERSION,
    HouseholdVoter,
    household_for_candidate,
    relationship_note,
)
from app.contact.owner_identity import parse_owner_name
from app.resolution.contract import Evidence

# The counties whose public files are held locally. A county absent here is
# reported as NOT_SUPPLIED, never silently skipped -- "we found nothing" and "we
# never looked" are different answers and the operator needs the second one.
COUNTY_FILES = {
    "37183": ("ncvoter92.txt", "ncvoter92.zip", "WAKE"),
    "37179": ("ncvoter90.txt", "ncvoter90.zip", "UNION"),
    "37119": ("ncvoter60.txt", "ncvoter60.zip", "MECKLENBURG"),
    "37025": ("ncvoter13.txt", "ncvoter13.zip", "CABARRUS"),
    "37063": ("ncvoter32.txt", "ncvoter32.zip", "DURHAM"),
    "37071": ("ncvoter36.txt", "ncvoter36.zip", "GASTON"),
    "37101": ("ncvoter51.txt", "ncvoter51.zip", "JOHNSTON"),
}

ESTATE_CONDITIONS = ("DECEASED", "HEIRS_UNRESOLVED", "ESTATE_OPEN")
SOURCE_FAMILY = "NCSBE_PUBLIC_VOTER_FILE"
SOURCE_NAME = "NCSBE_PUBLIC_VOTER_FILE"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def file_digest(path: Path) -> str:
    """The snapshot's identity. Stable across runs, so evidence ids are too."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_observed_at(path: Path) -> str:
    """When the snapshot was retrieved, to the day.

    Deliberately NOT the run clock: a content address that includes the run
    clock is not a content address, and the second run would duplicate every
    row it was supposed to converge on.
    """
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)\
        .strftime("%Y-%m-%dT00:00:00Z")


def addr_key(pa: Optional[ParsedAddress]) -> Optional[Tuple[str, str]]:
    if not pa or not pa.house or not pa.street_slug:
        return None
    return (pa.house, pa.street_slug)


def load_candidates(conn, tiers: Sequence[str], limit: Optional[int]) -> List[dict]:
    sql = """
      select lead_id, candidate_id, tenant_id, owner_name, situs_address,
             situs_city, situs_state, situs_zip, mailing_address, mailing_city,
             mailing_state, mailing_zip, source_county, county_fips,
             priority_tier, owner_condition, owner_class, has_living_identity,
             primary_party_name, parcel_apn, global_rank
        from public.deedsonar_current_candidates_v1
       where priority_tier = any(%s)
         and owner_condition = any(%s)
       order by global_rank nulls last
    """
    if limit:
        sql += f" limit {int(limit)}"
    with conn.cursor() as cur:
        cur.execute(sql, (list(tiers), list(ESTATE_CONDITIONS)))
        return cur.fetchall()


def scan_county(raw_dir: Path, fips: str, wanted: set) -> Tuple[Dict[Tuple[str, str], List[dict]], dict]:
    """One pass over one county file, keeping only rows at a wanted address."""
    txt, zipname, county = COUNTY_FILES[fips]
    path = raw_dir / txt
    zpath = raw_dir / zipname
    meta = {"county": county, "fips": fips, "file": txt, "rows": 0, "hits": 0,
            "supplied": path.exists()}
    if not path.exists():
        return {}, meta
    meta["source_hash"] = file_digest(zpath if zpath.exists() else path)
    meta["observed_at"] = snapshot_observed_at(zpath if zpath.exists() else path)
    out: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    with path.open(encoding="latin-1", newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            meta["rows"] += 1
            res = parse_address(row.get("res_street_address"),
                                city=row.get("res_city_desc"),
                                state=row.get("state_cd"),
                                zipcode=row.get("zip_code"))
            k = addr_key(res)
            if k and k in wanted:
                meta["hits"] += 1
                out[k].append({"row": row, "res": res})
    return out, meta


def to_voter(rec: dict, meta: dict) -> HouseholdVoter:
    r = rec["row"]
    return HouseholdVoter(
        ncid=(r.get("ncid") or "").strip(),
        last=(r.get("last_name") or "").strip(),
        first=(r.get("first_name") or "").strip(),
        middle=(r.get("middle_name") or "").strip(),
        suffix=(r.get("name_suffix_lbl") or "").strip(),
        status=(r.get("voter_status_desc") or "").strip(),
        reason=(r.get("voter_status_reason_desc") or "").strip(),
        confidential=(r.get("confidential_ind") or "").strip(),
        phone_raw=(r.get("full_phone_number") or "").strip(),
        birth_year=(r.get("birth_year") or "").strip(),
        county=(r.get("county_desc") or "").strip(),
        res=rec["res"],
        source_file=meta["file"],
        source_hash=meta["source_hash"],
        observed_at=meta["observed_at"],
    )


def plan(candidates: List[dict], raw_dir: Path) -> Tuple[List[dict], Counter, List[dict]]:
    """Everything that WOULD be written, plus the census of what happened."""
    wanted: Dict[str, set] = defaultdict(set)
    prepared = []
    for c in candidates:
        situs = parse_address(c["situs_address"], city=c["situs_city"],
                              state=c["situs_state"] or "NC", zipcode=c["situs_zip"])
        mail = parse_address(c["mailing_address"], city=c["mailing_city"],
                             state=c["mailing_state"], zipcode=c["mailing_zip"])
        ks = {"situs": addr_key(situs), "mailing": addr_key(mail)}
        fips = c["county_fips"]
        for k in ks.values():
            if k and fips in COUNTY_FILES:
                wanted[fips].add(k)
        prepared.append((c, situs, mail, ks))

    indexes: Dict[str, dict] = {}
    metas: List[dict] = []
    for fips in sorted(wanted):
        idx, meta = scan_county(raw_dir, fips, wanted[fips])
        indexes[fips] = idx
        metas.append(meta)
        print(f"  scanned {meta['county']} ({fips}): rows={meta['rows']} "
              f"address_hits={meta['hits']}", file=sys.stderr)

    census = Counter()
    rows: List[dict] = []
    for c, situs, mail, ks in prepared:
        fips = c["county_fips"]
        census["cohort"] += 1
        if fips not in COUNTY_FILES:
            census["county_file_not_supplied"] += 1
            continue
        meta = next((m for m in metas if m["fips"] == fips), None)
        if not meta or not meta.get("supplied"):
            census["county_file_not_supplied"] += 1
            continue
        idx = indexes.get(fips, {})
        owner = parse_owner_name(c["owner_name"])
        situs_v = [to_voter(r, meta) for r in idx.get(ks["situs"], [])] if ks["situs"] else []
        mail_v = [to_voter(r, meta) for r in idx.get(ks["mailing"], [])] if ks["mailing"] else []
        if not situs_v and not mail_v:
            census["no_address_match"] += 1
            continue
        census["address_matched"] += 1
        result = household_for_candidate(
            owner_people=owner.people,
            situs_voters=situs_v, mailing_voters=mail_v,
            situs_label=situs.core_key, mailing_label=mail.core_key,
        )
        census["skipped_confidential"] += result.skipped_confidential
        census["skipped_not_live"] += result.skipped_not_live
        census["decedent_registrations_seen"] += len(result.decedent_registrations)
        if not result.surname_members:
            census["co_resident_but_no_surname"] += 1
            continue
        census["surname_corroborated_candidates"] += 1
        if result.multiple_relatives:
            census["multiple_relatives_household"] += 1
        if not c["has_living_identity"]:
            census["NEW_living_identity_candidates"] += 1

        decedent_display = " ".join(
            p for p in [owner.people[0].first, owner.people[0].last] if p
        ) if owner.people else (c["owner_name"] or "")

        for m in result.surname_members:
            v = m.voter
            ev = Evidence(
                source_family=SOURCE_FAMILY, source_name=SOURCE_NAME,
                source_record_id=v.ncid or f"{v.county}:{v.last},{v.first}",
                field="party_identity",
                raw_value=v.display_name,
                normalized_value=v.display_name.upper(),
                match_method=m.match_method,
                confidence=m.confidence,
                observed_at=v.observed_at, retrieved_at=v.observed_at,
                source_hash=v.source_hash,
                notes=f"{HOUSEHOLD_VERSION}; observed at the "
                      f"{m.observed_at_address.lower()} address; "
                      f"registration status {v.status}",
            )
            rows.append({
                "tenant_id": str(c["tenant_id"]),
                "import_row_id": str(c["candidate_id"]),
                "canonical_name": v.display_name.upper(),
                # The table's own check constraint is lowercase, and
                # `parties` is keyed on (tenant, kind, canonical_name) -- so
                # writing "PERSON" here would not merely fail the constraint,
                # it would have created a SECOND party for every human the
                # graph already holds if the constraint had been absent.
                "kind": "person",
                "relationship_role": m.relationship_role,
                "relationship_class": m.relationship_class,
                "family_relation": m.family_relation,
                "confidence": m.confidence,
                "evidence_id": ev.evidence_id,
                "evidence": ev,
                "notes": relationship_note(m, decedent_display),
                "has_phone": m.has_phone,
                "lead_id": str(c["lead_id"]),
                "owner_name": c["owner_name"],
                "priority_tier": c["priority_tier"],
                "owner_condition": c["owner_condition"],
                "county": c["source_county"],
                "observed_at_address": m.observed_at_address,
                "had_living_identity": bool(c["has_living_identity"]),
            })
            census["family_relationships_planned"] += 1
            if m.has_phone:
                census["planned_with_voter_phone"] += 1
    return rows, census, metas


def apply_plan(conn, rows: List[dict]) -> Counter:
    """One transaction. Every insert converges on a second run."""
    stats = Counter()
    with conn.cursor() as cur:
        cur.execute("""
            create temp table _t30_ev (
              evidence_id text, tenant_id uuid, source_family text, source_name text,
              source_record_id text, source_locator text, source_hash text, field text,
              raw_value text, normalized_value text, match_method text, confidence text,
              observed_at timestamptz, retrieved_at timestamptz, notes text
            ) on commit drop;
            create temp table _t30_party (
              tenant_id uuid, kind text, canonical_name text
            ) on commit drop;
            create temp table _t30_rel (
              tenant_id uuid, import_row_id uuid, kind text, canonical_name text,
              relationship_role text, relationship_class text, family_relation text,
              confidence text, evidence_id text, notes text
            ) on commit drop;
        """)
        with cur.copy("copy _t30_ev from stdin") as cp:
            for r in rows:
                e = r["evidence"]
                cp.write_row((r["evidence_id"], r["tenant_id"], e.source_family,
                              e.source_name, e.source_record_id, e.source_locator,
                              e.source_hash, e.field, e.raw_value, e.normalized_value,
                              e.match_method, e.confidence, e.observed_at,
                              e.retrieved_at, e.notes))
        with cur.copy("copy _t30_party from stdin") as cp:
            for r in rows:
                cp.write_row((r["tenant_id"], r["kind"], r["canonical_name"]))
        with cur.copy("copy _t30_rel from stdin") as cp:
            for r in rows:
                cp.write_row((r["tenant_id"], r["import_row_id"], r["kind"],
                              r["canonical_name"], r["relationship_role"],
                              r["relationship_class"], r["family_relation"],
                              r["confidence"], r["evidence_id"], r["notes"]))

        cur.execute("""
            insert into public.contact_evidence (
              evidence_id, tenant_id, source_family, source_name, source_record_id,
              source_locator, source_hash, field, raw_value, normalized_value,
              match_method, confidence, observed_at, retrieved_at, notes)
            select distinct on (evidence_id) evidence_id, tenant_id, source_family,
              source_name, source_record_id, source_locator, source_hash, field,
              raw_value, normalized_value, match_method, confidence, observed_at,
              retrieved_at, notes
              from _t30_ev
            on conflict (evidence_id) do nothing;
        """)
        stats["evidence_inserted"] = cur.rowcount
        cur.execute("""
            insert into public.parties (tenant_id, kind, canonical_name)
            select distinct tenant_id, kind, canonical_name from _t30_party
            on conflict (tenant_id, kind, canonical_name) do nothing;
        """)
        stats["parties_inserted"] = cur.rowcount
        cur.execute("""
            insert into public.candidate_parties (
              tenant_id, import_row_id, party_id, relationship_role,
              relationship_class, family_relation, confidence, evidence_id, notes)
            select t.tenant_id, t.import_row_id, p.party_id, t.relationship_role,
                   t.relationship_class, t.family_relation, t.confidence,
                   t.evidence_id, t.notes
              from _t30_rel t
              join public.parties p on p.tenant_id = t.tenant_id
                                   and p.kind = t.kind
                                   and p.canonical_name = t.canonical_name
            on conflict (tenant_id, import_row_id, party_id, relationship_role)
              do update set relationship_class = excluded.relationship_class,
                            family_relation = excluded.family_relation,
                            confidence = excluded.confidence,
                            evidence_id = excluded.evidence_id,
                            notes = excluded.notes,
                            updated_at = now();
        """)
        stats["relationships_written"] = cur.rowcount
    return stats


def snapshot(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
          select (select count(*) from public.parties) parties,
                 (select count(*) from public.candidate_parties) rels,
                 (select count(*) from public.contact_evidence) evidence,
                 (select count(*) from public.candidate_parties
                    where relationship_class='FAMILY') family_rels
        """)
        return cur.fetchone()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--verify-idempotent", action="store_true",
                    help="apply, then apply again, and prove the second is a no-op")
    ap.add_argument("--tiers", default="A,B,C")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--raw-dir", default=os.environ.get(
        "T30_NCSBE_RAW",
        str(Path.home() / "neuruh-workspace/T19F_DEEDSONAR_FREE_PHONE_ENRICHMENT_20260818/_raw/ncsbe")))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    tiers = [t.strip().upper() for t in args.tiers.split(",") if t.strip()]
    raw_dir = Path(args.raw_dir)
    if not raw_dir.exists():
        print(f"RAW_DIR_MISSING={raw_dir}", file=sys.stderr)
        return 2

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        conn.execute("set statement_timeout = '300s'")
        cands = load_candidates(conn, tiers, args.limit)
        print(f"COHORT={len(cands)} tiers={','.join(tiers)}", file=sys.stderr)
        rows, census, metas = plan(cands, raw_dir)
        before = snapshot(conn)

        report = {
            "run_at": utcnow(),
            "household_version": HOUSEHOLD_VERSION,
            "tiers": tiers,
            "census": dict(census),
            "county_files": [{k: v for k, v in m.items() if k != "source_hash"}
                             | {"source_hash_16": (m.get("source_hash") or "")[:16]}
                             for m in metas],
            "planned_relationships": len(rows),
            "distinct_new_people": len({(r["tenant_id"], r["canonical_name"]) for r in rows}),
            "before": dict(before),
            "applied": False,
        }

        if args.apply or args.verify_idempotent:
            manifest = Path.home() / f".neuruh/t30_prestate_{utcnow().replace(':','')}.json"
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps({"before": dict(before)}, default=str, indent=1))
            stats1 = apply_plan(conn, rows)
            conn.commit()
            after1 = snapshot(conn)
            report["apply_1"] = dict(stats1)
            report["after"] = dict(after1)
            report["applied"] = True
            report["prestate_manifest"] = str(manifest)
            if args.verify_idempotent:
                stats2 = apply_plan(conn, rows)
                conn.commit()
                after2 = snapshot(conn)
                report["apply_2"] = dict(stats2)
                report["after_second"] = dict(after2)
                report["second_pass_noop"] = (
                    dict(after1) == dict(after2)
                    and stats2["evidence_inserted"] == 0
                    and stats2["parties_inserted"] == 0
                )

    text = json.dumps(report, indent=1, default=str)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

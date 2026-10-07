#!/usr/bin/env python3
"""T20C — canonical $0 NC public-record contact pipeline.

Reads the CURRENT NC estate, official NCSBE county voter files, optional
lawfully obtained estate-case and SOS listing imports, and the IRS EO BMF.
Default is delta refresh: rematch only new or changed identities. Writes
T12-compatible append CSVs, resolution ledger, and recipe/county yield
reports outside Git.

Does not call paid providers, does not mutate production, does not dial,
does not run DNC, does not script the NC SOS interactive search.
PHONE PRESENT ≠ CALLABLE.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contact.address_normalize import parse_address, extract_zip5
from app.custody.locations import canonical_bundle, estate_export, run_dir
from app.custody.sync_guard import prepare_output_dir, secure_file
from app.contact.confidence import score_contact
from app.contact.delta_refresh import (
    DeltaPartition,
    infer_prior_estate_ids,
    partition_delta,
    refresh_receipt,
)
from app.contact.entity_public import (
    build_eo_index,
    extract_990_contact,
    load_eo_bmf,
    match_entity_to_eo,
    names_corroborate,
    org_key,
    parse_ico_people,
    source_id,
)
from app.contact.estate_fields import property_zip_for
from app.contact.estate_import import ESTATE_IMPORT_COLUMNS, load_estate_import_csv
from app.contact.estate_pdf_intake import intake_estate_pdfs
from app.contact.identity_ledger import (
    IDENTITY_LEDGER_COLUMNS,
    IDENTITY_LEDGER_VERSION,
    identity_rows,
)
from app.contact.identity_tuple import estate_revision, identity_tuple_hash, master_csv_paths
from app.contact.local_owned import sweep_local_owned
from app.contact.ncsbe_match import MatchDecision, VoterRecord, match_person_to_voters, voter_from_ncsbe_row
from app.contact.official_fetch import (
    CUSTODY_FILENAME,
    drifted_sources,
    fetch_official_bytes,
    load_custody,
    sha256_file,
    write_custody,
)
from app.contact.owner_class import owner_route_class, phone_role_for
from app.contact.owner_identity import parse_owner_name
from app.contact.phone_graph import analyze_phone_graph, fanout_for
from app.contact.phone_normalize import normalize_us_phone
from app.contact.product_export import write_product_suite
from app.contact.resolution import (
    RESOLUTION_COLUMNS,
    build_resolution_ledger,
    prioritize_estate,
)
from app.contact.t12_append import (
    COMBINED_COLUMNS,
    CONTACT_PROVENANCE_FIELDS,
    PhoneAppendIdentity,
    evaluate_phone_append,
    source_row_id_for,
)
from app.contact.yield_reports import (
    COUNTY_YIELD_COLUMNS,
    RECIPE_CONTACT_COLUMNS,
    RECIPE_OWNER_CLASS_COLUMNS,
    county_free_contact_yield,
    recipe_contact_yield,
    recipe_intelligence,
    recipe_owner_class_yield,
)

NCSBE_COUNTIES = {
    "13": {"name": "Cabarrus", "fips": "37025", "file": "ncvoter13"},
    "32": {"name": "Durham", "fips": "37063", "file": "ncvoter32"},
    "36": {"name": "Gaston", "fips": "37071", "file": "ncvoter36"},
    "51": {"name": "Johnston", "fips": "37101", "file": "ncvoter51"},
    "60": {"name": "Mecklenburg", "fips": "37119", "file": "ncvoter60"},
    "90": {"name": "Union", "fips": "37179", "file": "ncvoter90"},
    "92": {"name": "Wake", "fips": "37183", "file": "ncvoter92"},
}
NCSBE_BASE = "https://s3.amazonaws.com/dl.ncsbe.gov/data"
COUNTY_ORDER = ["Mecklenburg", "Wake", "Durham", "Johnston", "Union", "Cabarrus", "Gaston"]
# Contact output lands in custody, never on the Desktop. ~/Desktop on this host
# IS the iCloud Desktop -- same inodes -- so the old defaults published 922 real
# consumer phone numbers to Apple on every run. See app/custody/sync_guard.py.
T19F_PRODUCT = run_dir("DEEDSONAR_FREE_PHONE_APPEND_20260818")
CANONICAL_PRODUCT = canonical_bundle()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_estate(estate_dir: Path) -> List[dict]:
    rows: List[dict] = []
    for county in COUNTY_ORDER:
        chunk_files = sorted((estate_dir / county / "master").glob(f"{county.lower()}_master_*.csv"))
        for path in chunk_files:
            with path.open(newline="", encoding="utf-8") as fh:
                rows.extend(csv.DictReader(fh))
    return rows


def ncsbe_census(ncsbe_dir: Path) -> List[dict]:
    out = []
    for cid, meta in NCSBE_COUNTIES.items():
        txt = ncsbe_dir / f"{meta['file']}.txt"
        zpath = ncsbe_dir / f"{meta['file']}.zip"
        rows = phone = conf = active_phone = 0
        with txt.open("r", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            for row in reader:
                rows += 1
                pv = (row.get("full_phone_number") or "").strip()
                if pv and pv not in {"0", "0000000000"}:
                    phone += 1
                    status = (row.get("voter_status_desc") or "").strip().upper()
                    if status in {"ACTIVE", "INACTIVE", "TEMPORARY"}:
                        active_phone += 1
                if (row.get("confidential_ind") or "").strip().upper() in {"Y", "YES"}:
                    conf += 1
        out.append({
            "county_id": cid,
            "county": meta["name"],
            "fips": meta["fips"],
            "source_url": f"{NCSBE_BASE}/{meta['file']}.zip",
            "txt_path": str(txt),
            "zip_sha256": sha256_file(zpath) if zpath.exists() else "",
            "txt_sha256": "",
            "txt_mtime": datetime.fromtimestamp(txt.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "VOTER_ROWS": rows,
            "PHONE_PRESENT": phone,
            "PHONE_FILL_RATE": round(phone / rows, 6) if rows else 0,
            "ACTIVEISH_PHONE_PRESENT": active_phone,
            "CONFIDENTIAL": conf,
        })
    return out


def _estate_zips(row: dict) -> List[str]:
    zips = []
    for z in (
        property_zip_for(row) or extract_zip5(row.get("property_address_raw"), row.get("property_address_normalized")),
        extract_zip5(row.get("mailing_zip"), row.get("mailing_address")),
    ):
        if z and z not in zips:
            zips.append(z)
    return zips


def scan_ncsbe_for_lasts(
    ncsbe_dir: Path,
    needed_lasts: set[str],
    firsts_by_last: Dict[str, set],
    custody: dict,
) -> Dict[str, List[VoterRecord]]:
    index: Dict[str, List[VoterRecord]] = defaultdict(list)
    kept = 0
    scanned = 0
    for cid, meta in NCSBE_COUNTIES.items():
        txt = ncsbe_dir / f"{meta['file']}.txt"
        source_hash = custody[cid]["zip_sha256"]
        observed = custody[cid]["txt_mtime"]
        with txt.open("r", encoding="utf-8", errors="replace") as fh:
            reader = csv.DictReader(fh, delimiter="\t")
            for row in reader:
                scanned += 1
                last = (row.get("last_name") or "").strip().upper()
                if last not in needed_lasts:
                    continue
                wanted = firsts_by_last.get(last) or set()
                first = (row.get("first_name") or "").strip().upper()
                keep = False
                if first in wanted:
                    keep = True
                elif first and len(first) == 1 and any(f.startswith(first) for f in wanted):
                    keep = True
                elif any(len(f) == 1 and first.startswith(f) for f in wanted):
                    keep = True
                if not keep:
                    continue
                rec = voter_from_ncsbe_row(row, txt.name, source_hash, observed)
                index[last].append(rec)
                kept += 1
        print(f"  indexed {meta['name']}: running kept={kept:,} scanned={scanned:,}", flush=True)
    print(f"NCSBE_INDEX last_names={len(index)} voters_kept={kept:,} scanned={scanned:,}", flush=True)
    return index


def group_hypotheses(people) -> List[list]:
    by_seg: Dict[str, list] = defaultdict(list)
    for p in people:
        by_seg[p.raw_segment or p.full_normalized].append(p)
    return list(by_seg.values())


def choose_decision(hyps, prop, mail, county, index):
    accepted = []
    held = []
    for hyp in hyps:
        voters = index.get(hyp.last, [])
        decision = match_person_to_voters(hyp, prop, mail, county, voters)
        if decision.accepted:
            accepted.append((hyp, decision))
        elif decision.reject_class:
            held.append((hyp, decision))
    if accepted:
        ids = {d.voter.identity_hash for _, d in accepted}
        if len(ids) > 1:
            return None, None, ("AMBIGUOUS", held)
        accepted.sort(key=lambda x: (0 if x[1].tier == "TIER_A" else 1, 0 if len(x[0].first) > 1 else 1))
        return accepted[0][0], accepted[0][1], None
    if held:
        rank = {"TIER_C_HELD": 0, "AMBIGUOUS": 1, "LOW_CONFIDENCE": 2, "COMMON_NAME": 3}
        held.sort(key=lambda x: rank.get(x[1].reject_class, 9))
        return None, None, held[0]
    return None, None, None


def match_estate_to_ncsbe(estate: List[dict], index: Dict[str, List[VoterRecord]]) -> dict:
    identity_hits = []
    phone_rows = []
    held_rows = []
    stats = defaultdict(int)
    per_county = {
        c: {"matched": 0, "phones": 0, "held": 0, "ambiguous": 0}
        for c in COUNTY_ORDER
    }
    seen_ident = set()
    seen_phone = set()

    for i, row in enumerate(estate, 1):
        if i % 2000 == 0 or i == len(estate):
            print(
                f"  match progress {i}/{len(estate)} "
                f"identity={stats['identity_matches']} phones={stats['phone_matches']}",
                flush=True,
            )
        parsed = parse_owner_name(row.get("owner_name") or "")
        stats[f"kind_{parsed.kind}"] += 1
        # A TRUST is still not a person, but the roll may NAME its trustee — the
        # party who can actually convey the parcel. Those named people route
        # like any other named person; a trust with no named trustee does not.
        if parsed.kind == "TRUST" and any(
            p.role == "NAMED_TRUSTEE_ON_ROLL" for p in parsed.living_people
        ):
            stats["trust_with_named_trustee"] += 1
        elif parsed.kind in {"ENTITY", "TRUST", "EMPTY"}:
            stats["skipped_nonperson"] += 1
            continue
        living = parsed.living_people
        if not living:
            stats["no_living_person"] += 1
            continue
        prop = parse_address(
            row.get("property_address_raw") or row.get("property_address_normalized"),
            row.get("property_city"),
            row.get("property_state") or "NC",
            property_zip_for(row),
        )
        mail = parse_address(
            row.get("mailing_address"),
            row.get("mailing_city"),
            row.get("mailing_state"),
            row.get("mailing_zip"),
        )
        county = row.get("county") or ""
        for hyps in group_hypotheses(living):
            person, decision, held = choose_decision(hyps, prop, mail, county, index)
            if decision is None:
                if isinstance(held, tuple) and held and held[0] == "AMBIGUOUS":
                    stats["reject_AMBIGUOUS"] += 1
                    per_county[county]["ambiguous"] += 1
                elif isinstance(held, tuple) and len(held) == 2 and held[1] is not None:
                    stats[f"reject_{held[1].reject_class}"] += 1
                    per_county[county]["held"] += 1
                    held_rows.append({"row": row, "person": held[0], "decision": held[1]})
                continue
            cand = row["candidate_id"]
            ident_key = (cand, decision.voter.identity_hash)
            if ident_key not in seen_ident:
                seen_ident.add(ident_key)
                identity_hits.append((row, person, decision))
                per_county[county]["matched"] += 1
                stats["identity_matches"] += 1
            phone = decision.phone
            if not phone or phone.classification != "VALID_US" or not phone.e164:
                stats["identity_without_valid_phone"] += 1
                continue
            phone_key = (cand, phone.e164)
            if phone_key in seen_phone:
                stats["duplicate_candidate_phone"] += 1
                continue
            seen_phone.add(phone_key)
            per_county[county]["phones"] += 1
            stats["phone_matches"] += 1
            phone_rows.append({
                "row": row,
                "person": person,
                "decision": decision,
                "e164": phone.e164,
            })
    return {
        "identity_hits": identity_hits,
        "phone_rows": phone_rows,
        "held_rows": held_rows,
        "stats": dict(stats),
        "per_county": per_county,
    }


def append_record(item: dict, phone_source: str, extra: Optional[dict] = None) -> dict:
    row = item["row"]
    person = item["person"]
    decision = item["decision"]
    voter = decision.voter
    extra = extra or {}
    fips = row.get("fips") or ""
    rec = {
        "candidate_id": row["candidate_id"],
        "apn": row.get("apn") or "",
        "phone": item["e164"],
        "county": row.get("county") or "",
        "fips": fips,
        "source_row_id": source_row_id_for(fips, row.get("apn") or ""),
        "owner_name": row.get("owner_name") or "",
        "phone_source": phone_source,
        "provider_reference": extra.get("provider_reference", ""),
        "observed_at": voter.observed_at if voter else extra.get("observed_at", ""),
        "match_tier": decision.tier,
        "match_confidence": "STRONG_IDENTITY",
        "source_record_identity": extra.get("source_record_identity") or (voter.identity_hash if voter else ""),
        "source_observed_at": voter.observed_at if voter else extra.get("observed_at", ""),
        "source_hash": voter.source_hash if voter else extra.get("source_hash", ""),
        "phone_number_type": decision.phone.number_type if decision.phone else "",
        "numbering_plan_valid": "YES" if decision.phone and decision.phone.numbering_plan_valid else "NO",
        "currently_assigned": "UNKNOWN",
        "right_party": "UNKNOWN",
        "callable_claim": "UNKNOWN",
        "person_role": person.role,
        "phone_role": extra.get("phone_role", ""),
        "relationship": extra.get("relationship", person.role),
        "estate_case": extra.get("estate_case", ""),
        "heir_person_id": extra.get("heir_person_id", ""),
        "owner_class": extra.get("owner_class", ""),
        "confidence_category": extra.get("confidence_category", ""),
        "confidence_score": extra.get("confidence_score", ""),
        "source_count": extra.get("source_count", "1"),
        "source_list": extra.get("source_list", phone_source),
        "source_age_days": extra.get("source_age_days", ""),
        "fanout_candidates": extra.get("fanout_candidates", ""),
        "why_matched": extra.get("why_matched") or decision.reason or decision.tier,
        "matched_person": extra.get("matched_person") or " ".join(
            p for p in (getattr(person, "first", ""), getattr(person, "last", "")) if p
        ).strip() or (row.get("owner_name") or ""),
        "primary_recipe": row.get("primary_recipe") or "",
        "score": row.get("score") or "",
        "global_rank": row.get("global_rank") or "",
        "qualification_state": row.get("qualification_state") or "",
    }
    return rec


def validate_against_estate(rec: dict, estate_by_id: dict) -> dict:
    src = estate_by_id.get(rec["candidate_id"])
    if not src:
        return {"ok": False, "reason": "CANDIDATE_MISMATCH"}
    ident = PhoneAppendIdentity(
        candidate_id=src["candidate_id"],
        source_row_id=source_row_id_for(src.get("fips") or "", src.get("apn") or ""),
        apn=src.get("apn") or "",
        fips=src.get("fips") or "",
        county=src.get("county") or "",
        owner_name=src.get("owner_name") or "",
    )
    return evaluate_phone_append(ident, rec)


def write_csv(path: Path, rows: List[dict], columns: Iterable[str]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = list(columns)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in cols})
    return {"path": str(path), "rows": len(rows), "sha256": sha256_file(path)}


def overlay_county_lists(estate: List[dict], phones_by_cand: Dict[str, List[dict]], out_dir: Path) -> List[dict]:
    manifests = []
    cols = [
        "candidate_id", "county", "fips", "owner_name", "property_address_raw",
        "mailing_address", "mailing_city", "mailing_state", "mailing_zip",
        "apn", "score", "global_rank", "primary_recipe", "qualification_state",
        "phone_1", "phone_2", "phone_3", "phone_source", "phone_match_confidence",
        "dnc_status", "callability_status", "phone_present",
    ]
    by_county: Dict[str, List[dict]] = defaultdict(list)
    for row in estate:
        by_county[row["county"]].append(row)
    for county in COUNTY_ORDER:
        rows = by_county.get(county, [])
        # Preserve T11 ordering: already global_rank ASC, candidate_id ASC within county masters.
        out_rows = []
        for row in rows:
            recs = phones_by_cand.get(row["candidate_id"], [])
            e164s = []
            sources = []
            for rec in recs:
                if rec["phone"] not in e164s:
                    e164s.append(rec["phone"])
                    sources.append(rec["phone_source"])
            dnc = row.get("dnc_status") or "UNKNOWN_NOT_EVALUATED"
            if dnc in {"", "UNKNOWN"}:
                dnc = "UNKNOWN_NOT_EVALUATED"
            has = bool(e164s)
            out_rows.append({
                "candidate_id": row["candidate_id"],
                "county": row["county"],
                "fips": row["fips"],
                "owner_name": row.get("owner_name") or "",
                "property_address_raw": row.get("property_address_raw") or "",
                "mailing_address": row.get("mailing_address") or "",
                "mailing_city": row.get("mailing_city") or "",
                "mailing_state": row.get("mailing_state") or "",
                "mailing_zip": row.get("mailing_zip") or "",
                "apn": row.get("apn") or "",
                "score": row.get("score") or "",
                "global_rank": row.get("global_rank") or "",
                "primary_recipe": row.get("primary_recipe") or "",
                "qualification_state": row.get("qualification_state") or "",
                "phone_1": e164s[0] if len(e164s) > 0 else "",
                "phone_2": e164s[1] if len(e164s) > 1 else "",
                "phone_3": e164s[2] if len(e164s) > 2 else "",
                "phone_source": ";".join(dict.fromkeys(sources)),
                "phone_match_confidence": "STRONG_IDENTITY" if has else "",
                "dnc_status": dnc,
                "callability_status": "NOT_AUTHORIZED",
                "phone_present": "YES" if has else "NO",
            })
        path = out_dir / "county_lists" / f"{county.upper()}.csv"
        manifests.append(write_csv(path, out_rows, cols))
    return manifests


def fetch_bytes(url: str, timeout: int = 12) -> Optional[bytes]:
    return fetch_official_bytes(url, timeout=timeout)


def person_row_needs_index(row: dict, partition) -> bool:
    cid = row.get("candidate_id") or ""
    return cid in partition.new_ids or cid in partition.changed_ids


def load_csv_rows(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def stamp_estate_provenance(rec: dict, estate_row: dict) -> dict:
    out = dict(rec)
    out["primary_recipe"] = estate_row.get("primary_recipe") or out.get("primary_recipe") or ""
    out["score"] = estate_row.get("score") or out.get("score") or ""
    out["global_rank"] = estate_row.get("global_rank") or out.get("global_rank") or ""
    out["qualification_state"] = estate_row.get("qualification_state") or out.get("qualification_state") or ""
    out["owner_name"] = estate_row.get("owner_name") or out.get("owner_name") or ""
    out["why_matched"] = out.get("why_matched") or out.get("match_tier") or ""
    out["matched_person"] = out.get("matched_person") or out.get("owner_name") or ""
    out["currently_assigned"] = out.get("currently_assigned") or "UNKNOWN"
    out["right_party"] = out.get("right_party") or "UNKNOWN"
    out["callable_claim"] = "UNKNOWN"
    if not out.get("confidence_category"):
        out["confidence_category"] = "HIGH"
    return out


def load_990_index_for_eins(index_path: Path, eins: set[str]) -> dict:
    found: dict = {}
    if not index_path.exists() or not eins:
        return found
    with index_path.open(newline="", encoding="utf-8", errors="replace") as fh:
        for row in csv.DictReader(fh):
            ein = (row.get("EIN") or "").strip()
            if ein in eins:
                # keep latest tax period
                prev = found.get(ein)
                if not prev or str(row.get("TAX_PERIOD") or "") > str(prev.get("TAX_PERIOD") or ""):
                    found[ein] = row
    return found


def fetch_990_xml_for_filing(irs_dir: Path, filing: dict, year: str) -> Optional[Path]:
    """Fetch a single official Form 990 XML. Does not download TEOS batch zips."""
    oid = (filing.get("OBJECT_ID") or "").strip()
    if not oid:
        return None
    xml_dir = irs_dir / "xml"
    xml_dir.mkdir(parents=True, exist_ok=True)
    target = xml_dir / f"{oid}_public.xml"
    if target.exists() and target.stat().st_size > 0:
        return target
    urls = [
        f"https://apps.irs.gov/pub/epostcard/990/xml/{year}/{oid}_public.xml",
        f"https://apps.irs.gov/pub/epostcard/990/xml/{year}/{oid}.xml",
        f"https://irs-form-990.s3.amazonaws.com/{oid}_public.xml",
    ]
    for url in urls:
        data = fetch_bytes(url, timeout=8)
        if data and b"</" in data and (
            b"Return" in data or b"Form990" in data or b"IRS990" in data or b"PhoneNum" in data
        ):
            target.write_bytes(data)
            return target
    # Official TEOS files are published as large annual batch zips. Do not
    # download multi-hundred-MB batches for a single EIN in this $0 lane.
    return None


def download_needed_990_xml(irs_dir: Path, filings: dict, year: str) -> dict:
    extracted = {}
    for ein, row in filings.items():
        path = fetch_990_xml_for_filing(irs_dir, row, year)
        if path:
            extracted[row.get("OBJECT_ID") or ""] = path
            print(f"  990 xml EIN={ein} object={row.get('OBJECT_ID')}", flush=True)
    return extracted


def entity_phone_pass(estate: List[dict], irs_dir: Path):
    eo_path = irs_dir / "eo_nc.csv"
    if not eo_path.exists():
        return [], 0
    print("Loading IRS EO BMF (NC)…", flush=True)
    records = load_eo_bmf(eo_path)
    idx = build_eo_index(records)
    print(f"  EO records={len(records)}", flush=True)
    hits = []
    for row in estate:
        parsed = parse_owner_name(row.get("owner_name") or "")
        if parsed.kind != "ENTITY":
            continue
        rec = match_entity_to_eo(
            row.get("owner_name") or "",
            row.get("mailing_address") or "",
            row.get("mailing_city") or "",
            row.get("mailing_state") or "",
            row.get("mailing_zip") or "",
            row.get("property_address_raw") or "",
            idx,
        )
        if rec:
            hits.append((row, rec))
    print(f"  EO identity matches={len(hits)}", flush=True)
    eins = {rec.ein for _, rec in hits}
    filings = {}
    for year, fname in (("2026", "index_2026.csv"), ("2025", "index_2025.csv")):
        more = load_990_index_for_eins(irs_dir / fname, eins)
        for ein, row in more.items():
            if ein not in filings:
                filings[ein] = {**row, "index_year": year}
    print(f"  990 index hits={len(filings)}", flush=True)
    by_year: Dict[str, dict] = defaultdict(dict)
    for ein, row in filings.items():
        by_year[row["index_year"]][ein] = row
    xml_map = {}
    for year, subset in by_year.items():
        xml_map.update(download_needed_990_xml(irs_dir, subset, year))

    out = []
    eo_hash = sha256_file(eo_path)
    for row, rec in hits:
        filing = filings.get(rec.ein)
        if not filing:
            continue
        xml_path = xml_map.get(filing.get("OBJECT_ID") or "")
        if not xml_path or not Path(xml_path).exists():
            continue
        contact = extract_990_contact(Path(xml_path).read_bytes())
        if not names_corroborate(row["owner_name"], contact.get("name") or rec.name):
            continue
        phone = normalize_us_phone(contact.get("phone") or "")
        if phone.classification != "VALID_US":
            continue
        zip_ok = extract_zip5(rec.zip5, contact.get("zip")) and (
            extract_zip5(row.get("mailing_zip"), row.get("mailing_address"), row.get("property_zip"), row.get("property_address_raw"))
            in {extract_zip5(rec.zip5), extract_zip5(contact.get("zip")), extract_zip5(row.get("mailing_zip"), row.get("property_zip"))}
        )
        city_ok = (rec.city or "").upper() in {
            (row.get("mailing_city") or "").upper(),
            (row.get("property_city") or "").upper(),
            (contact.get("city") or "").upper(),
        }
        if not (zip_ok or city_ok):
            continue
        ident = PhoneAppendIdentity(
            candidate_id=row["candidate_id"],
            source_row_id=source_row_id_for(row.get("fips") or "", row.get("apn") or ""),
            apn=row.get("apn") or "",
            fips=row.get("fips") or "",
            county=row.get("county") or "",
            owner_name=row.get("owner_name") or "",
        )
        rec_out = {
            "candidate_id": row["candidate_id"],
            "apn": row.get("apn") or "",
            "phone": phone.e164,
            "county": row.get("county") or "",
            "fips": row.get("fips") or "",
            "source_row_id": ident.source_row_id,
            "owner_name": row.get("owner_name") or "",
            "phone_source": "IRS_FORM_990_PUBLIC",
            "provider_reference": rec.ein,
            "observed_at": utcnow(),
            "match_tier": "TIER_A",
            "match_confidence": "STRONG_IDENTITY",
            "source_record_identity": source_id(rec.ein, filing.get("OBJECT_ID") or ""),
            "source_observed_at": utcnow(),
            "source_hash": eo_hash,
            "phone_number_type": phone.number_type,
            "numbering_plan_valid": "YES",
            "currently_assigned": "UNKNOWN",
            "right_party": "UNKNOWN",
            "callable_claim": "UNKNOWN",
            "person_role": "ENTITY",
            "relationship": "PUBLIC_BUSINESS",
            "estate_case": "",
            "heir_person_id": "",
        }
        if evaluate_phone_append(ident, rec_out).get("ok"):
            out.append(rec_out)
    return out, len(hits)


def entity_ico_ncsbe_pass(estate: List[dict], index: Dict[str, List[VoterRecord]], irs_dir: Path) -> tuple[List[dict], int]:
    """Nonprofit ICO (in-care-of) person at the EO address → NCSBE. Not a registered-agent grab."""
    eo_path = irs_dir / "eo_nc.csv"
    if not eo_path.exists():
        return [], 0
    records = load_eo_bmf(eo_path)
    idx = build_eo_index(records)
    eo_hash = sha256_file(eo_path)
    out = []
    identities = 0
    for row in estate:
        parsed = parse_owner_name(row.get("owner_name") or "")
        if parsed.kind != "ENTITY":
            continue
        rec = match_entity_to_eo(
            row.get("owner_name") or "",
            row.get("mailing_address") or "",
            row.get("mailing_city") or "",
            row.get("mailing_state") or "",
            row.get("mailing_zip") or "",
            row.get("property_address_raw") or "",
            idx,
        )
        if not rec:
            continue
        identities += 1
        people = parse_ico_people(rec.ico)
        if not people:
            continue
        org_addr = parse_address(rec.street, rec.city, rec.state, rec.zip5)
        mail = parse_address(
            row.get("mailing_address"), row.get("mailing_city"),
            row.get("mailing_state"), row.get("mailing_zip"),
        )
        for person in people:
            decision = match_person_to_voters(
                person, org_addr, mail, row.get("county") or "", index.get(person.last, []),
            )
            if not decision.accepted or not decision.phone or decision.phone.classification != "VALID_US":
                continue
            ident = PhoneAppendIdentity(
                candidate_id=row["candidate_id"],
                source_row_id=source_row_id_for(row.get("fips") or "", row.get("apn") or ""),
                apn=row.get("apn") or "",
                fips=row.get("fips") or "",
                county=row.get("county") or "",
                owner_name=row.get("owner_name") or "",
            )
            rec_out = {
                "candidate_id": row["candidate_id"],
                "apn": row.get("apn") or "",
                "phone": decision.phone.e164,
                "county": row.get("county") or "",
                "fips": row.get("fips") or "",
                "source_row_id": ident.source_row_id,
                "owner_name": row.get("owner_name") or "",
                "phone_source": "IRS_EO_ICO_NCSBE",
                "provider_reference": rec.ein,
                "observed_at": decision.voter.observed_at if decision.voter else "",
                "match_tier": decision.tier,
                "match_confidence": "STRONG_IDENTITY",
                "source_record_identity": source_id(rec.ein, person.full_normalized),
                "source_observed_at": decision.voter.observed_at if decision.voter else "",
                "source_hash": eo_hash,
                "phone_number_type": decision.phone.number_type,
                "numbering_plan_valid": "YES",
                "currently_assigned": "UNKNOWN",
                "right_party": "UNKNOWN",
                "callable_claim": "UNKNOWN",
                "person_role": "ENTITY",
                "phone_role": "BUSINESS_OFFICE",
                "relationship": "EO_ICO",
                "estate_case": "",
                "heir_person_id": "",
            }
            if evaluate_phone_append(ident, rec_out).get("ok"):
                out.append(rec_out)
    return out, identities


def quality_sample(phone_rows: List[dict], out_path: Path, n: int = 60) -> dict:
    random.seed(20260818)
    accepted = [x for x in phone_rows if x.get("person") is not None and x.get("decision") is not None]
    random.shuffle(accepted)
    sample = accepted[: min(n, len(accepted))]
    cols = [
        "candidate_id", "county", "owner_name", "person_full", "person_role",
        "property_address_raw", "mailing_address", "voter_last", "voter_first",
        "voter_res", "voter_mail", "tier", "reason", "phone",
    ]
    rows = []
    for item in sample:
        row, person, decision = item["row"], item["person"], item["decision"]
        v = decision.voter
        e164 = item.get("e164") or (decision.phone.e164 if decision and decision.phone else "")
        rows.append({
            "candidate_id": row["candidate_id"],
            "county": row["county"],
            "owner_name": row.get("owner_name"),
            "person_full": person.full_normalized,
            "person_role": person.role,
            "property_address_raw": row.get("property_address_raw"),
            "mailing_address": row.get("mailing_address"),
            "voter_last": v.last_name if v else "",
            "voter_first": v.first_name if v else "",
            "voter_res": f"{v.res.house} {v.res.street_slug} {v.res.zip5}" if v else "",
            "voter_mail": f"{v.mail.house} {v.mail.street_slug} {v.mail.zip5}" if v else "",
            "tier": decision.tier,
            "reason": decision.reason,
            "phone": e164,
        })
    write_csv(out_path, rows, cols)
    # Automated court: last-name agreement + house agreement on the matched side.
    auto_fail = 0
    for r in rows:
        owner_ok = r["voter_last"] and r["voter_last"] in (r["person_full"] or "")
        house_prop = (r["property_address_raw"] or "").split(" ")[0]
        house_mail = (r["mailing_address"] or "").split(" ")[0]
        vres_house = (r["voter_res"] or "").split(" ")[0]
        vmail_house = (r["voter_mail"] or "").split(" ")[0]
        addr_ok = (vres_house and vres_house in {house_prop, house_mail, vres_house} and (
            vres_house == house_prop or vres_house == house_mail or vmail_house == house_prop or vmail_house == house_mail
        ))
        if not (owner_ok and addr_ok):
            auto_fail += 1
    return {"sampled": len(rows), "auto_fail": auto_fail, "path": str(out_path)}


def write_templates(raw_dir: Path) -> None:
    estate = raw_dir / "estate" / "estate_import_template.csv"
    estate.parent.mkdir(parents=True, exist_ok=True)
    if not estate.exists():
        with estate.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=list(ESTATE_IMPORT_COLUMNS)).writeheader()
    sos = raw_dir / "sos" / "sos_import_template.csv"
    sos.parent.mkdir(parents=True, exist_ok=True)
    from app.contact.entity_public import SOS_IMPORT_COLUMNS
    if not sos.exists():
        with sos.open("w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=list(SOS_IMPORT_COLUMNS)).writeheader()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--estate-dir", default=str(estate_export()))
    p.add_argument("--ncsbe-dir", default=str(Path.home() / "neuruh-workspace/T19F_DEEDSONAR_FREE_PHONE_ENRICHMENT_20260818/_raw/ncsbe"))
    p.add_argument("--irs-dir", default=str(Path.home() / "neuruh-workspace/T19F_DEEDSONAR_FREE_PHONE_ENRICHMENT_20260818/_raw/irs"))
    p.add_argument("--raw-dir", default=str(Path.home() / "neuruh-workspace/T19F_DEEDSONAR_FREE_PHONE_ENRICHMENT_20260818/_raw"))
    p.add_argument("--out-dir", default=str(CANONICAL_PRODUCT))
    p.add_argument("--evidence-dir", default=str(Path.home() / "neuruh-workspace/T20C_DEEDSONAR_FREE_CONTACT_CANONICAL"))
    p.add_argument("--prior-append", default="")
    p.add_argument("--prior-missing", default="")
    p.add_argument("--prior-resolution", default="")
    p.add_argument("--skip-census", action="store_true")
    p.add_argument("--skip-entities", action="store_true")
    p.add_argument("--skip-local", action="store_true")
    p.add_argument("--full-rematch", action="store_true")
    args = p.parse_args()

    estate_dir = Path(args.estate_dir)
    ncsbe_dir = Path(args.ncsbe_dir)
    irs_dir = Path(args.irs_dir)
    raw_dir = Path(args.raw_dir)
    out_dir = Path(args.out_dir)
    evidence_dir = Path(args.evidence_dir)
    # SENSITIVE_EXPORT_TO_SYNCED_PATH = BLOCK. Both of these receive owner names,
    # property addresses and phone numbers, so both are proven off every cloud
    # sync root before a single byte is written.
    out_dir = prepare_output_dir(out_dir, what="contact enrichment product")
    evidence_dir = prepare_output_dir(evidence_dir, what="contact enrichment evidence")
    write_templates(raw_dir)

    run_started_at = utcnow()
    print("Loading CURRENT estate…", flush=True)
    estate = load_estate(estate_dir)
    print(f"CURRENT_TOTAL={len(estate)}", flush=True)
    revision = estate_revision(master_csv_paths(estate_dir))
    print(f"ESTATE_REVISION={revision}", flush=True)

    prior_append_path = Path(args.prior_append) if args.prior_append else (
        out_dir / "phone_append_combined.csv" if (out_dir / "phone_append_combined.csv").exists()
        else T19F_PRODUCT / "phone_append_combined.csv"
    )
    prior_missing_path = Path(args.prior_missing) if args.prior_missing else (
        prior_append_path.parent / "CURRENT_STILL_MISSING_PHONE.csv"
    )
    prior_resolution_path = Path(args.prior_resolution) if args.prior_resolution else (
        prior_append_path.parent / "CANDIDATE_CONTACT_RESOLUTION.csv"
    )
    prior_append = load_csv_rows(prior_append_path)
    prior_missing = load_csv_rows(prior_missing_path)
    prior_resolution = load_csv_rows(prior_resolution_path)
    prior_hashes = {
        r["candidate_id"]: r["identity_tuple_hash"]
        for r in prior_resolution
        if r.get("candidate_id") and r.get("identity_tuple_hash")
    }
    prior_reasons = {}
    for r in prior_resolution + prior_missing:
        cid = r.get("candidate_id") or ""
        reason = r.get("contact_resolution_state") or r.get("unresolved_reason") or ""
        if cid and reason:
            prior_reasons.setdefault(cid, reason)
    prior_estate_ids = (
        {r["candidate_id"] for r in prior_resolution if r.get("candidate_id")}
        or infer_prior_estate_ids(estate, prior_append, prior_missing)
    )
    prior_receipt_path = prior_append_path.parent / "refresh_receipt.json"
    prior_estate_revision = ""
    if prior_receipt_path.exists():
        try:
            prior_estate_revision = str(json.loads(prior_receipt_path.read_text()).get("estate_revision") or "")
        except json.JSONDecodeError:
            prior_estate_revision = ""

    if args.full_rematch:
        # A full rematch recomputes every decision from source. Inheriting the
        # previous run's resolution reasons would let a stale reason outlive the
        # rule that produced it, so they are dropped here rather than carried.
        prior_reasons = {}
        partition = DeltaPartition(
            estate_revision=revision,
            new_ids={r["candidate_id"] for r in estate},
            rematch_rows=list(estate),
        )
    else:
        partition = partition_delta(
            estate,
            estate_revision=revision,
            prior_append=prior_append,
            prior_hashes=prior_hashes,
            prior_estate_ids=prior_estate_ids,
            prior_estate_revision=prior_estate_revision,
        )
    print(
        f"DELTA unchanged={len(partition.unchanged_ids)} changed={len(partition.changed_ids)} "
        f"new={len(partition.new_ids)} dropped={len(partition.dropped_ids)} "
        f"rematch={len(partition.rematch_rows)} keep_high={len(partition.keep_rows)}",
        flush=True,
    )

    # Hash-pin every official free input before it is read. Cache wins; a
    # changed digest is recorded as SOURCE_DRIFT, never silently adopted.
    custody_path = evidence_dir / CUSTODY_FILENAME
    source_custody = load_custody(custody_path)
    free_sources_seen = defaultdict(int)
    for family, folder, pattern in (
        ("NCSBE_PUBLIC_VOTER_FILE", ncsbe_dir, "ncvoter*.zip"),
        ("IRS_EO_ICO_NCSBE", irs_dir, "eo_nc.csv"),
        ("IRS_FORM_990_PUBLIC", irs_dir, "*990*"),
        ("ESTATE_IMPORT", raw_dir / "estate", "*.csv"),
        ("SOS_IMPORT", raw_dir / "sos", "*.csv"),
    ):
        for path in sorted(Path(folder).glob(pattern)):
            if not path.is_file() or path.name.lower().startswith("template"):
                continue
            prior = (source_custody.get(path.name) or {}).get("sha256") or ""
            sha = sha256_file(path)
            source_custody[path.name] = {
                "family": family,
                "path": str(path),
                "url": "",
                "sha256": sha,
                "prior_sha256": prior,
                "bytes": path.stat().st_size,
                "state": "SOURCE_DRIFT" if (prior and prior != sha) else (
                    "CACHED_UNCHANGED" if prior else "CACHED_UNPINNED"
                ),
                "observed_at": utcnow(),
                "downloaded": False,
            }
            free_sources_seen[family] += 1
    source_drift = drifted_sources(source_custody)
    write_custody(custody_path, source_custody)
    print(
        f"FREE_SOURCE_CUSTODY files={len(source_custody)} "
        f"families={dict(free_sources_seen)} drift={source_drift}",
        flush=True,
    )

    census_path = evidence_dir / "03_COUNTY_PHONE_CENSUS.csv"
    t19f_census = Path.home() / "neuruh-workspace/T19F_DEEDSONAR_FREE_PHONE_ENRICHMENT_20260818/03_COUNTY_PHONE_CENSUS.csv"
    if args.skip_census and not census_path.exists() and t19f_census.exists():
        census_path = t19f_census
    if args.skip_census and census_path.exists():
        with census_path.open(newline="", encoding="utf-8") as fh:
            census = list(csv.DictReader(fh))
        # restore numeric
        for c in census:
            c["VOTER_ROWS"] = int(c["VOTER_ROWS"])
            c["PHONE_PRESENT"] = int(c["PHONE_PRESENT"])
    else:
        print("Censusing NCSBE files (this is the per-county proof)…", flush=True)
        census = ncsbe_census(ncsbe_dir)
        write_csv(census_path, census, census[0].keys())
    custody = {c["county_id"]: c for c in census}
    for c in census:
        print(
            f"  {c['county']}: VOTER_ROWS={c['VOTER_ROWS']} "
            f"PHONE_PRESENT={c['PHONE_PRESENT']} PHONE_FILL_RATE={c['PHONE_FILL_RATE']}",
            flush=True,
        )

    print("Parsing owners for last-name index keys…", flush=True)
    needed_lasts = set()
    firsts_by_last: Dict[str, set] = defaultdict(set)
    kind_counts = defaultdict(int)
    living_count = 0
    named_heirs = 0
    rematch_required = partition.rematch_required
    for row in estate:
        parsed = parse_owner_name(row.get("owner_name") or "")
        kind_counts[parsed.kind] += 1
        for person in parsed.living_people:
            living_count += 1
            if person.role == "NAMED_HEIR_ON_ROLL":
                named_heirs += 1
            if rematch_required and person_row_needs_index(row, partition):
                needed_lasts.add(person.last)
                firsts_by_last[person.last].add(person.first)
    print(f"  kinds={dict(kind_counts)} living_hypotheses={living_count} unique_lasts={len(needed_lasts)} named_heirs_on_roll={named_heirs}", flush=True)

    if rematch_required and not args.skip_entities:
        eo_path = irs_dir / "eo_nc.csv"
        if eo_path.exists():
            print("Pre-indexing EO ICO last names into the NCSBE filter…", flush=True)
            eo_idx = build_eo_index(load_eo_bmf(eo_path))
            ico_added = 0
            for row in partition.rematch_rows:
                parsed = parse_owner_name(row.get("owner_name") or "")
                if parsed.kind != "ENTITY":
                    continue
                rec = match_entity_to_eo(
                    row.get("owner_name") or "",
                    row.get("mailing_address") or "",
                    row.get("mailing_city") or "",
                    row.get("mailing_state") or "",
                    row.get("mailing_zip") or "",
                    row.get("property_address_raw") or "",
                    eo_idx,
                )
                if not rec:
                    continue
                for person in parse_ico_people(rec.ico):
                    needed_lasts.add(person.last)
                    firsts_by_last[person.last].add(person.first)
                    ico_added += 1
            print(f"  ico_people_added_to_filter={ico_added} unique_lasts={len(needed_lasts)}", flush=True)

    empty_matched = {
        "identity_hits": [],
        "phone_rows": [],
        "held_rows": [],
        "stats": {},
        "per_county": {c: {"matched": 0, "phones": 0, "held": 0, "ambiguous": 0} for c in COUNTY_ORDER},
    }
    index: Dict[str, List[VoterRecord]] = {}
    if rematch_required:
        print("Scanning NCSBE into last-name index…", flush=True)
        t0 = time.time()
        index = scan_ncsbe_for_lasts(ncsbe_dir, needed_lasts, firsts_by_last, custody)
        print(f"  index_seconds={time.time()-t0:.1f}", flush=True)

        print("Matching rematch subset (Qualified / high-rank first)…", flush=True)
        t1 = time.time()
        matched = match_estate_to_ncsbe(prioritize_estate(partition.rematch_rows), index)
        print(f"  match_seconds={time.time()-t1:.1f} stats={matched['stats']}", flush=True)
    else:
        print("DELTA rematch=0 — skipping NCSBE index/match; keeping prior HIGH rows.", flush=True)
        matched = empty_matched

    ncsbe_rows = []
    heir_rows = []
    for item in matched["phone_rows"]:
        source = "NCSBE_PUBLIC_VOTER_FILE"
        extra = {}
        if item["person"].role == "NAMED_HEIR_ON_ROLL":
            extra = {
                "relationship": "NAMED_HEIR_OR_COOWNER_ON_ROLL",
                "heir_person_id": source_id(item["row"]["candidate_id"], item["person"].full_normalized),
            }
            heir_rows.append(append_record(item, source, extra))
        else:
            ncsbe_rows.append(append_record(item, source, extra))

    estate_by_id = {r["candidate_id"]: r for r in estate}
    for rec in partition.keep_rows:
        src = estate_by_id.get(rec.get("candidate_id") or "")
        if not src:
            continue
        stamped = stamp_estate_provenance(rec, src)
        if stamped.get("phone_role") in {"HEIR_PERSONAL", "COOWNER_PERSONAL"} or stamped.get("person_role") == "NAMED_HEIR_ON_ROLL":
            heir_rows.append(stamped)
        else:
            ncsbe_rows.append(stamped)

    # Lawfully obtained estate-case import (optional; empty unless operator dropped a file).
    # 2026-09-02: AOC-E-201 / AOC-E-505 PDFs dropped under _raw/estate/pdf/<county>/ are
    # parsed into the importer CSV first, with a receipt per PDF (never a phone).
    try:
        pdf_receipts = intake_estate_pdfs(raw_dir)
        print(f"ESTATE_PDF_INTAKE={len(pdf_receipts)} rows_written={sum(r.people_written for r in pdf_receipts)}", flush=True)
    except Exception as exc:  # the intake never blocks the run
        print(f"ESTATE_PDF_INTAKE=ERROR {exc!r}", flush=True)
    estate_import_people = []
    for csv_path in (raw_dir / "estate").glob("*.csv"):
        if csv_path.name.lower().endswith("template.csv"):
            continue
        estate_import_people.extend(load_estate_import_csv(csv_path))
    print(f"ESTATE_IMPORT_PEOPLE={len(estate_import_people)}", flush=True)
    estate_by_id = {r["candidate_id"]: r for r in estate}
    for ep in estate_import_people:
        src = estate_by_id.get(ep.property_candidate_id)
        if not src:
            continue
        prop = parse_address(src.get("property_address_raw"), src.get("property_city"), src.get("property_state") or "NC", property_zip_for(src))
        mail = parse_address(ep.mailing_address, ep.mailing_city, ep.mailing_state, ep.mailing_zip)
        voters = index.get(ep.person.last, [])
        decision = match_person_to_voters(ep.person, prop, mail, src.get("county") or "", voters)
        if not decision.accepted or not decision.phone or decision.phone.classification != "VALID_US":
            continue
        item = {"row": src, "person": ep.person, "decision": decision, "e164": decision.phone.e164}
        heir_rows.append(append_record(item, "NCSBE_PUBLIC_VOTER_FILE", {
            "relationship": ep.relationship,
            "estate_case": ep.estate_case,
            "heir_person_id": ep.heir_person_id,
        }))

    entity_rows = []
    entity_identities = 0
    if rematch_required and not args.skip_entities:
        print("Entity public-business pass (IRS EO + 990, no SOS search)…", flush=True)
        try:
            entity_rows, entity_identities = entity_phone_pass(estate, irs_dir)
        except Exception as exc:
            print(f"  entity pass error (non-fatal): {exc!r}", flush=True)
            entity_rows, entity_identities = [], 0
        print("Entity ICO → NCSBE pass…", flush=True)
        try:
            ico_rows, ico_identities = entity_ico_ncsbe_pass(estate, index, irs_dir)
            entity_identities = max(entity_identities, ico_identities)
            entity_rows.extend(ico_rows)
            print(f"  ICO_NCSBE_PHONES={len(ico_rows)} EO_IDENTITIES={ico_identities}", flush=True)
        except Exception as exc:
            print(f"  ico pass error (non-fatal): {exc!r}", flush=True)
    print(f"ENTITY_PHONES={len(entity_rows)} ENTITY_IDENTITIES={entity_identities}", flush=True)

    # Owner route-class for every CURRENT row.
    owner_class: Dict[str, str] = {}
    class_counts = defaultdict(int)
    parsed_by_id = {}
    for row in estate:
        parsed = parse_owner_name(row.get("owner_name") or "")
        parsed_by_id[row["candidate_id"]] = parsed
        oc = owner_route_class(row.get("owner_name") or "", parsed)
        owner_class[row["candidate_id"]] = oc
        class_counts[oc] += 1
    print(f"OWNER_CLASS_COUNTS={dict(class_counts)}", flush=True)

    print("Local owned-data identity-safe sweep…", flush=True)
    if args.skip_local:
        local = {
            "LOCAL_PHONE_RECORDS_SEEN": 0,
            "LOCAL_IDENTITY_SAFE_MATCHES": 0,
            "LOCAL_REJECTED_AMBIGUOUS": 0,
            "rows": [],
        }
        print("  skipped (--skip-local); identity-safe local attach remains in the rematch path", flush=True)
    else:
        local = sweep_local_owned(estate, [
            estate_export(),
            Path.home() / "neuruh-workspace/T11_DEEDSONAR_CONTACT_RECOVERY_20260817",
            Path.home() / "neuruh-workspace/T12_DEEDSONAR_BACKEND_20260817",
            Path.home() / "neuruh-workspace/T16_DEEDSONAR_PHONE_ENRICHMENT_20260817",
            Path.home() / "neuruh-workspace/T19F_DEEDSONAR_FREE_PHONE_ENRICHMENT_20260818",
            ROOT / "data",
        ])
    print(
        f"  LOCAL_PHONE_RECORDS_SEEN={local['LOCAL_PHONE_RECORDS_SEEN']} "
        f"LOCAL_IDENTITY_SAFE_MATCHES={local['LOCAL_IDENTITY_SAFE_MATCHES']} "
        f"LOCAL_REJECTED_AMBIGUOUS={local['LOCAL_REJECTED_AMBIGUOUS']}",
        flush=True,
    )
    local_rows = local["rows"]

    # T12 validate every discovered row (schema/identity). Confidence filter is later.
    combined = []
    rejected = defaultdict(int)
    for rec in ncsbe_rows + heir_rows + entity_rows + local_rows:
        oc = owner_class.get(rec["candidate_id"], "")
        rec["owner_class"] = oc
        rec["phone_role"] = rec.get("phone_role") or phone_role_for(
            rec.get("person_role") or "", oc, rec.get("phone_source") or "",
        )
        if rec.get("phone_role") in {"REGISTERED_AGENT", "ATTORNEY"}:
            rejected["ENTITY_MISMATCH"] += 1
            continue
        verdict = validate_against_estate(rec, estate_by_id)
        if verdict.get("ok"):
            rec["phone"] = verdict["e164"]
            combined.append(rec)
        else:
            rejected[verdict.get("reason") or "UNKNOWN"] += 1
    uniq = {}
    for rec in combined:
        uniq[(rec["candidate_id"], rec["phone"])] = rec
    combined = list(uniq.values())

    graph = analyze_phone_graph(combined)
    sources_by_phone_cand = defaultdict(set)
    for rec in combined:
        sources_by_phone_cand[(rec["candidate_id"], rec["phone"])].add(rec.get("phone_source") or "")

    high_rows, medium_rows, review_rows = [], [], []
    for rec in combined:
        cand_n, owner_n = fanout_for(rec["phone"], graph)
        srcs = sorted(sources_by_phone_cand[(rec["candidate_id"], rec["phone"])])
        parsed = parsed_by_id.get(rec["candidate_id"])
        deceased_target = bool(parsed and rec.get("person_role") == "DECEDENT")
        conf = score_contact(
            tier=rec.get("match_tier") or "",
            strong_name=True,
            common_name=bool(parsed and any(p.is_common_name for p in parsed.living_people)),
            source=rec.get("phone_source") or "",
            observed_at=rec.get("observed_at") or rec.get("source_observed_at") or "",
            source_count=len(srcs),
            fanout_candidates=cand_n,
            fanout_distinct_owners=owner_n,
            numbering_plan_valid=rec.get("numbering_plan_valid") == "YES",
            deceased_target=deceased_target,
        )
        rec["confidence_category"] = conf.category
        rec["confidence_score"] = str(conf.score)
        rec["source_count"] = str(len(srcs))
        rec["source_list"] = ";".join(srcs)
        rec["fanout_candidates"] = str(cand_n)
        rec["source_age_days"] = str(conf.components.get("source_age_days") or "")
        rec["match_confidence"] = "STRONG_IDENTITY" if conf.category == "HIGH" else conf.category
        if conf.category == "HIGH":
            high_rows.append(rec)
        elif conf.category == "MEDIUM":
            medium_rows.append(rec)
            rejected["LOW_CONFIDENCE"] += 1
        else:
            review_rows.append(rec)
            rejected["LOW_CONFIDENCE"] += 1

    # T12 append candidate set is HIGH only.
    t12_accepted, t12_rejected = [], 0
    for rec in high_rows:
        verdict = validate_against_estate(rec, estate_by_id)
        if verdict.get("ok"):
            t12_accepted.append(rec)
        else:
            t12_rejected += 1
            rejected[verdict.get("reason") or "T12"] += 1

    ncsbe_out = write_csv(
        out_dir / "phone_append_ncsbe.csv",
        [r for r in t12_accepted if r.get("phone_source") == "NCSBE_PUBLIC_VOTER_FILE" and r.get("phone_role") not in {"HEIR_PERSONAL", "COOWNER_PERSONAL"}],
        COMBINED_COLUMNS,
    )
    heirs_out = write_csv(
        out_dir / "phone_append_heirs.csv",
        [r for r in t12_accepted if r.get("phone_role") in {"HEIR_PERSONAL", "COOWNER_PERSONAL"} or r.get("person_role") == "NAMED_HEIR_ON_ROLL"],
        COMBINED_COLUMNS,
    )
    ent_out = write_csv(
        out_dir / "phone_append_entities.csv",
        [r for r in t12_accepted if r.get("phone_source") in {"IRS_FORM_990_PUBLIC", "IRS_EO_ICO_NCSBE", "PUBLIC_BUSINESS_WEB"}],
        COMBINED_COLUMNS,
    )
    comb_out = write_csv(out_dir / "phone_append_combined.csv", t12_accepted, COMBINED_COLUMNS)
    write_csv(out_dir / "validation" / "medium_review.csv", medium_rows, COMBINED_COLUMNS)
    write_csv(out_dir / "validation" / "review_held.csv", review_rows, COMBINED_COLUMNS)

    phones_by_cand: Dict[str, List[dict]] = defaultdict(list)
    for rec in t12_accepted:
        phones_by_cand[rec["candidate_id"]].append(rec)
    county_manifests = overlay_county_lists(estate, phones_by_cand, out_dir)

    identity_cands = {h[0]["candidate_id"] for h in matched["identity_hits"]}
    identity_cands |= {
        cid for cid, reason in prior_reasons.items()
        if reason == "IDENTITY_MATCHED_VOTER_PHONE_ABSENT"
    }
    identity_cands |= set(phones_by_cand)
    held_tier_c = {
        item["row"]["candidate_id"]
        for item in (matched.get("held_rows") or [])
        if (item.get("decision") and item["decision"].tier == "TIER_C")
        or (item.get("decision") and item["decision"].reject_class == "TIER_C_HELD")
    }
    held_tier_c |= {cid for cid, reason in prior_reasons.items() if reason == "TIER_C_HELD_NOT_APPENDED"}
    ambiguous_ids = {
        cid for cid, reason in prior_reasons.items() if reason == "AMBIGUOUS_REJECTED"
    }
    ledger = build_resolution_ledger(
        estate,
        owner_class,
        phones_by_cand,
        identity_tuple_hash,
        identity_cands=identity_cands,
        held_tier_c=held_tier_c,
        ambiguous=ambiguous_ids,
        prior_reasons=prior_reasons,
    )
    resolution_by_cand = {r["candidate_id"]: r["contact_resolution_state"] for r in ledger}
    assert len(ledger) == len(estate), "every CURRENT candidate must be classified"
    assert all(r.get("callability_status") == "NOT_AUTHORIZED" for r in ledger)
    attempted_default = ";".join(sorted(free_sources_seen)) or "NCSBE_PUBLIC_VOTER_FILE"
    product_files = write_product_suite(
        estate, phones_by_cand, owner_class, identity_cands, out_dir, write_csv,
        resolution_by_cand=resolution_by_cand,
        attempted_default=attempted_default,
        last_attempted_at=run_started_at,
    )
    resolution_out = write_csv(out_dir / "CANDIDATE_CONTACT_RESOLUTION.csv", ledger, RESOLUTION_COLUMNS)
    # Every PROVEN identity, including the ones whose voter record carries no
    # phone. Additive export only: nothing above this line is re-run, re-matched,
    # or re-thresholded by it. On a delta run with rematch=0 there are no fresh
    # identity_hits, so the file is written empty rather than half-populated --
    # a partial identity ledger would be worse than an honest empty one.
    identity_ledger = identity_rows(matched.get("identity_hits") or [])
    identity_out = write_csv(
        out_dir / "CANDIDATE_IDENTITY_LEDGER.csv", identity_ledger, IDENTITY_LEDGER_COLUMNS)
    print(f"IDENTITY_LEDGER rows={len(identity_ledger)} "
          f"with_phone={sum(1 for r in identity_ledger if r['phone_present'] == 'YES')} "
          f"named_no_phone={sum(1 for r in identity_ledger if r['phone_present'] == 'NO')} "
          f"-> CANDIDATE_IDENTITY_LEDGER.csv")
    recipe_yield = recipe_contact_yield(ledger)
    class_yield = recipe_owner_class_yield(ledger)
    county_yield_rows = county_free_contact_yield(ledger)
    recipe_intel = recipe_intelligence(recipe_yield, class_yield)
    yield_files = {
        "RECIPE_CONTACT_YIELD": write_csv(out_dir / "RECIPE_CONTACT_YIELD.csv", recipe_yield, RECIPE_CONTACT_COLUMNS),
        "RECIPE_OWNER_CLASS_YIELD": write_csv(out_dir / "RECIPE_OWNER_CLASS_YIELD.csv", class_yield, RECIPE_OWNER_CLASS_COLUMNS),
        "COUNTY_FREE_CONTACT_YIELD": write_csv(out_dir / "COUNTY_FREE_CONTACT_YIELD.csv", county_yield_rows, COUNTY_YIELD_COLUMNS),
    }
    for rec in t12_accepted:
        missing_prov = [f for f in CONTACT_PROVENANCE_FIELDS if not str(rec.get(f) or "").strip()]
        if missing_prov:
            raise SystemExit(f"contact missing provenance {missing_prov} candidate={rec.get('candidate_id')}")

    sample_info = quality_sample(
        matched["phone_rows"],
        out_dir / "validation" / "quality_court_sample.csv",
    )
    # Per-tier sample files (no extra PII in markdown).
    by_tier = defaultdict(list)
    for item in matched["phone_rows"]:
        by_tier[item["decision"].tier].append(item)
    for item in matched.get("held_rows") or []:
        by_tier[item["decision"].tier or item["decision"].reject_class].append(item)
    tier_samples = {}
    for tier, items in by_tier.items():
        n = 40 if tier != "TIER_B" else min(40, len(items))
        tier_samples[tier] = quality_sample(items, out_dir / "validation" / f"sample_{tier}.csv", n=n)

    qualified_direct = 0
    qualified_heir_phone = 0
    for item in matched["phone_rows"]:
        q = (item["row"].get("qualification_state") or "")
        if not q.startswith("QUALIFIED"):
            continue
        if item["person"].role == "NAMED_HEIR_ON_ROLL":
            qualified_heir_phone += 1
        else:
            qualified_direct += 1

    heir_identities_all = sum(
        1 for row in estate
        for person in parse_owner_name(row.get("owner_name") or "").living_people
        if person.role == "NAMED_HEIR_ON_ROLL"
    )
    q_ids = {r["candidate_id"] for r in estate if (r.get("qualification_state") or "").startswith("QUALIFIED")}
    q_class = defaultdict(int)
    for cid in q_ids:
        q_class[owner_class.get(cid, "UNKNOWN")] += 1
    q_phone = q_ids & set(phones_by_cand)
    missing_n = sum(1 for r in estate if r["candidate_id"] not in phones_by_cand)

    county_yield = {}
    for county in COUNTY_ORDER:
        rows_c = [r for r in estate if r["county"] == county]
        ids = {r["candidate_id"] for r in rows_c}
        high_c = {r["candidate_id"] for r in t12_accepted if r["county"] == county}
        med_c = {r["candidate_id"] for r in medium_rows if r["county"] == county}
        county_yield[county] = {
            "TARGETS": len(rows_c),
            "MATCHED_IDENTITIES": len(ids & identity_cands),
            "PHONE_PRESENT": len(ids & {item["row"]["candidate_id"] for item in matched["phone_rows"]}),
            "HIGH_CONFIDENCE": len(high_c),
            "MEDIUM": len(med_c),
            "REJECTED": matched["per_county"].get(county, {}).get("held", 0)
            + matched["per_county"].get(county, {}).get("ambiguous", 0),
        }

    waterfall = {
        "START": {"candidates": len(estate), "phones": 0},
        "LOCAL_OWNED_DATA": {"plus_candidates": len({r["candidate_id"] for r in local_rows if r["candidate_id"] in phones_by_cand and r.get("phone_source") == "LOCAL_OWNED_IDENTITY"})},
        "NCSBE": {"plus_candidates": len({r["candidate_id"] for r in t12_accepted if r.get("phone_source") == "NCSBE_PUBLIC_VOTER_FILE"})},
        "ESTATE_HEIR": {"plus_candidates": len({r["candidate_id"] for r in t12_accepted if r.get("phone_role") == "HEIR_PERSONAL"})},
        "ENTITY_BUSINESS": {"plus_candidates": len({r["candidate_id"] for r in t12_accepted if r.get("phone_source") in {"IRS_FORM_990_PUBLIC", "IRS_EO_ICO_NCSBE"}})},
        "TOTAL_UNIQUE": len(phones_by_cand),
        "UNRESOLVED": len(estate) - len(phones_by_cand),
    }

    reused_cands = {r.get("candidate_id") for r in partition.keep_rows if r.get("candidate_id")}
    receipt = refresh_receipt(
        partition,
        ncsbe_zip_sha256=[c.get("zip_sha256") or "" for c in census],
        high_appended=len(phones_by_cand),
        held_tier_c=len(held_tier_c),
        local_safe_matches=local["LOCAL_IDENTITY_SAFE_MATCHES"],
        classified_total=len(ledger),
        previous_high_reused=len(reused_cands & set(phones_by_cand)),
        new_high_appended=len(set(phones_by_cand) - reused_cands),
        medium_held=sum(1 for r in ledger if r.get("confidence_category") == "MEDIUM"),
        review_held=len(held_tier_c),
        source_drift=source_drift,
    )
    (out_dir / "refresh_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    (evidence_dir / "refresh_receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")

    summary = {
        "CURRENT_TOTAL": len(estate),
        "OWNER_CLASS_COUNTS": dict(class_counts),
        "LOCAL_PHONE_RECORDS_SEEN": local["LOCAL_PHONE_RECORDS_SEEN"],
        "LOCAL_SAFE_PHONE_MATCHES": local["LOCAL_IDENTITY_SAFE_MATCHES"],
        "LOCAL_REJECTED_AMBIGUOUS": local["LOCAL_REJECTED_AMBIGUOUS"],
        "NCSBE_MATCHED_IDENTITIES": len(identity_cands),
        "NCSBE_PHONE_PRESENT": len({r["candidate_id"] for r in t12_accepted if r.get("phone_source") == "NCSBE_PUBLIC_VOTER_FILE"}),
        "NCSBE_ACCEPTED_PHONES": len({r["candidate_id"] for r in t12_accepted if r.get("phone_source") == "NCSBE_PUBLIC_VOTER_FILE"}),
        "BY_COUNTY": matched["per_county"],
        "COUNTY_YIELD": county_yield,
        "QUALIFIED_306_DIRECT_PHONE_MATCHES": qualified_direct,
        "QUALIFIED_CLASS": dict(q_class),
        "QUALIFIED_WITH_FREE_PHONE": len(q_phone),
        "HEIR_IDENTITIES_RESOLVED": heir_identities_all,
        "HEIR_NCSBE_PHONE_MATCHES": len({r["candidate_id"] for r in heir_rows}),
        "ENTITY_IDENTITIES": entity_identities,
        "ENTITY_PUBLIC_BUSINESS_PHONES": len({r["candidate_id"] for r in t12_accepted if r.get("phone_source") in {"IRS_FORM_990_PUBLIC", "IRS_EO_ICO_NCSBE"}}),
        "TOTAL_PHONE_ROWS_DISCOVERED": len(combined),
        "TOTAL_UNIQUE_PHONES": graph["UNIQUE_PHONES"],
        "TOTAL_UNIQUE_CANDIDATES_WITH_NEW_FREE_PHONE": len(phones_by_cand),
        "HIGH_CONFIDENCE": len(t12_accepted),
        "MEDIUM_CONFIDENCE": len(medium_rows),
        "REVIEW": len(review_rows),
        "CURRENT_STILL_MISSING_PHONE": missing_n,
        "T12_APPEND_ACCEPTED_ROWS": len(t12_accepted),
        "T12_APPEND_REJECTED_ROWS": t12_rejected + sum(rejected.values()),
        "T12_REJECT_REASONS": dict(rejected),
        "PHONE_GRAPH": {k: graph[k] for k in ("UNIQUE_PHONES", "DUPLICATE_PHONE_ROWS", "MULTI_PROPERTY_PHONES", "HIGH_FANOUT_PHONES")},
        "WATERFALL": waterfall,
        "TIER_SAMPLES": {k: {sk: sv for sk, sv in v.items() if sk != "path"} for k, v in tier_samples.items()},
        "KIND_COUNTS": dict(kind_counts),
        "NCSBE_CENSUS": [
            {k: c[k] for k in ("county", "county_id", "VOTER_ROWS", "PHONE_PRESENT", "PHONE_FILL_RATE", "zip_sha256", "source_url", "txt_mtime")}
            for c in census
        ],
        "FILES": {
            "ncsbe": ncsbe_out, "heirs": heirs_out, "entities": ent_out, "combined": comb_out,
            "county_lists": county_manifests, "product": product_files,
            "resolution": resolution_out, "yields": yield_files,
        },
        "QUALITY_SAMPLE": sample_info,
        "REFRESH_RECEIPT": receipt,
        "EVERY_CURRENT_NC_CANDIDATE_CLASSIFIED": "YES" if len(ledger) == len(estate) else "NO",
        "RECIPE_INTELLIGENCE": recipe_intel,
        "FREE_SOURCES_RUN": dict(free_sources_seen),
        "FREE_SOURCE_DRIFT": source_drift,
        "FREE_CONTACT_PIPELINE_CANONICAL": "YES",
        "PAID_PROVIDER_CALLS": 0,
        "PAID_API_CALLS": 0,
        "CREDIT_USAGE": 0,
        "SUBSCRIPTIONS_CREATED": 0,
        "PAYMENT_METHOD_USED": "NO",
        "PROVIDER_SPEND": "$0.00",
        "SELLER_CONTACT": 0,
        "DNC_RUNS": 0,
        "PRODUCTION_MUTATED": "NO",
        "generated_at": utcnow(),
    }
    (out_dir / "manifest.json").write_text(json.dumps(summary, indent=2) + "\n")
    (evidence_dir / "run_summary.json").write_text(json.dumps({
        k: summary[k] for k in summary if k != "FILES"
    }, indent=2) + "\n")
    for name in ("RECIPE_CONTACT_YIELD.csv", "RECIPE_OWNER_CLASS_YIELD.csv", "COUNTY_FREE_CONTACT_YIELD.csv"):
        src = out_dir / name
        if src.exists():
            (evidence_dir / name).write_bytes(src.read_bytes())
    print(json.dumps({k: summary[k] for k in (
        "CURRENT_TOTAL", "OWNER_CLASS_COUNTS", "NCSBE_MATCHED_IDENTITIES", "NCSBE_PHONE_PRESENT",
        "HIGH_CONFIDENCE", "MEDIUM_CONFIDENCE", "ENTITY_IDENTITIES", "ENTITY_PUBLIC_BUSINESS_PHONES",
        "QUALIFIED_WITH_FREE_PHONE", "TOTAL_UNIQUE_CANDIDATES_WITH_NEW_FREE_PHONE",
        "CURRENT_STILL_MISSING_PHONE", "LOCAL_SAFE_PHONE_MATCHES",
    )}, indent=2))
    print("WROTE", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

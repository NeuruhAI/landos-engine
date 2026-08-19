#!/usr/bin/env python3
"""Run the $0 contact cascade on the people T22 proved from official records.

T22 named living parties from county parcel rolls and the IRS exempt-organisation
file. Those are IDENTITIES, not contacts. This asks the one free source that
carries a phone -- the NCSBE public voter file -- whether it knows any of them,
under exactly the rules the existing matcher already enforces:

  * the person's name must be compatible, not merely similar;
  * the voter's address must corroborate the property or the mailing address --
    NAME-ONLY IS NEVER A MATCH;
  * a confidential voter is skipped, an inactive registration is skipped;
  * two plausible different people, or one name at two conflicting houses, is
    AMBIGUOUS and yields nothing.

No paid provider is contacted. No number is dialled, and every phone this finds
lands NOT_AUTHORIZED like every other one.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contact.address_normalize import parse_address
from app.contact.ncsbe_match import match_person_to_voters, voter_from_ncsbe_row
from app.contact.owner_identity import parse_owner_name
from app.contact.phone_normalize import normalize_us_phone
from app.custody.sync_guard import prepare_output_dir
from app.resolution.official_import import (
    canonical_person, entity_relationships, estate_relationships, load_import_csv,
    normalize_person, parse_source_document, recover_entity_confidence,
    recover_estate_confidence,
)

NCSBE_COUNTIES = {
    "13": "Cabarrus", "32": "Durham", "36": "Gaston", "51": "Johnston",
    "60": "Mecklenburg", "90": "Union", "92": "Wake",
}
COUNTY_TO_FILE = {v: f"ncvoter{k}" for k, v in NCSBE_COUNTIES.items()}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_voters(ncsbe_dir: Path, county: str):
    stem = COUNTY_TO_FILE.get(county)
    if not stem:
        return [], "", ""
    path = ncsbe_dir / f"{stem}.txt"
    if not path.exists():
        return [], "", ""
    digest = sha256_file(path)
    observed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    voters = []
    with path.open(newline="", encoding="latin-1") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            voters.append(voter_from_ncsbe_row(row, path.name, digest, observed))
    return voters, digest, observed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--t22-dir", default=str(Path.home() / "neuruh-workspace"
                                             / "T22_PUBLIC_RECORD_DATA_SUPPLY_20260819"
                                             / "05_IMPORT_READY"))
    ap.add_argument("--ncsbe-dir", default=str(Path.home() / "neuruh-workspace"
                                               / "T19F_DEEDSONAR_FREE_PHONE_ENRICHMENT_20260818"
                                               / "_raw" / "ncsbe"))
    ap.add_argument("--out-dir", default=str(Path.home() / "neuruh-private" / "contact-intelligence"
                                             / "runs" / "T24A_T22_CONTACT_CASCADE"))
    ap.add_argument("--t24c-dir", default="",
                    help="T24C package ROOT. Confidence is recovered from its "
                         "04_NORMALIZED evidence, never taken from the import file.")
    ap.add_argument("--subjects-filter", default="",
                    help="CSV of candidate_id,person. Restricts the cascade to "
                         "those subjects -- the newly proven identities -- instead "
                         "of re-asking the voter file about people already asked.")
    args = ap.parse_args()

    out = prepare_output_dir(args.out_dir, what="T22 contact cascade output")
    t22 = Path(args.t22_dir).expanduser()
    ncsbe = Path(args.ncsbe_dir).expanduser()

    estate_rows = [("T22", r, None)
                   for r in load_import_csv(t22 / "NC_ESTATE_IDENTITY_IMPORT.csv")]
    entity_rows = [("T22", r, None)
                   for r in load_import_csv(t22 / "NC_ENTITY_IDENTITY_IMPORT.csv")]
    if args.t24c_dir:
        t24c = Path(args.t24c_dir).expanduser()
        imp, norm = t24c / "05_IMPORT_READY", t24c / "04_NORMALIZED"
        est_conf = recover_estate_confidence(norm / "ESTATE_RESOLUTION_EVIDENCE.csv")
        ent_conf = recover_entity_confidence(norm / "ENTITY_RESOLUTION_EVIDENCE.csv")
        estate_rows += [("T24C", r, est_conf)
                        for r in load_import_csv(imp / "NC_ESTATE_IDENTITY_IMPORT.csv")]
        entity_rows += [("T24C", r, ent_conf)
                        for r in load_import_csv(imp / "NC_ENTITY_IDENTITY_IMPORT.csv")]

    # Only the identities this run actually proved, when a filter is supplied.
    wanted = None
    if args.subjects_filter:
        wanted = set()
        for row in load_import_csv(Path(args.subjects_filter).expanduser()):
            wanted.add(((row.get("candidate_id") or "").strip(),
                        normalize_person(row.get("person") or "")))

    def admitted(lane, row, conf_map, person) -> bool:
        """Same gate the importer uses: stated confidence, or recovered from
        evidence, and never defaulted."""
        meta = parse_source_document(row.get("source_document") or "")
        confidence = meta["confidence"]
        if not confidence and conf_map is not None:
            got = conf_map.get(((row.get("candidate_id") or row.get("property_candidate_id")
                                 or "").strip(), normalize_person(person)))
            confidence = (got.confidence if got else "").upper()
        return confidence in {"EXACT", "HIGH"}

    # Each subject keeps the county and mailing address its own source stated;
    # that is what makes an address-corroborated match possible at all.
    subjects = []
    seen_subject = set()
    for lane, row, conf_map in estate_rows:
        person = ((row.get("personal_representative_name") or "").strip()
                  or (row.get("heir_name") or "").strip())
        if not person or not admitted(lane, row, conf_map, person):
            continue
        cid = (row.get("property_candidate_id") or "").strip()
        key = (cid, normalize_person(person))
        if wanted is not None and key not in wanted:
            continue
        if key in seen_subject:
            continue
        seen_subject.add(key)
        subjects.append({
            "candidate_id": cid,
            "person": canonical_person(person),
            "county": (row.get("county") or "").strip(),
            "mailing_address": (row.get("mailing_address") or "").strip(),
            "mailing_city": (row.get("mailing_city") or "").strip(),
            "mailing_state": (row.get("mailing_state") or "").strip(),
            "mailing_zip": (row.get("mailing_zip") or "").strip(),
            "origin": f"{lane}_ESTATE",
        })
    for lane, row, conf_map in entity_rows:
        person = (row.get("official_name") or "").strip()
        if not person or not admitted(lane, row, conf_map, person):
            continue
        cid = (row.get("property_candidate_id") or "").strip()
        key = (cid, normalize_person(person))
        if wanted is not None and key not in wanted:
            continue
        if key in seen_subject:
            continue
        seen_subject.add(key)
        subjects.append({
            "candidate_id": cid,
            "person": canonical_person(person),
            "county": (row.get("county") or "").strip(),
            "mailing_address": (row.get("mailing_office") or "").strip(),
            "mailing_city": "", "mailing_state": "", "mailing_zip": "",
            "origin": f"{lane}_ENTITY",
        })

    by_county = {}
    for s in subjects:
        by_county.setdefault(s["county"], []).append(s)

    verdicts = Counter()
    accepted, rejected = [], []
    for county, group in sorted(by_county.items()):
        voters, digest, observed = load_voters(ncsbe, county)
        if not voters:
            for s in group:
                verdicts["HELD_NO_VOTER_FILE"] += 1
                rejected.append({**s, "verdict": "HELD", "why": f"no voter file for {county}"})
            continue
        index = {}
        for v in voters:
            index.setdefault(v.last_name, []).append(v)
        for s in group:
            # A raw owner string can parse more than one way ("CLYDE R FREEMAN
            # JR" is western OR last-first). Both readings are tried and the
            # matcher's own ambiguity rules decide; a reading that finds nothing
            # simply finds nothing.
            parsed = parse_owner_name(s["person"])
            mailing = parse_address(s["mailing_address"], s["mailing_city"],
                                    s["mailing_state"], s["mailing_zip"])
            empty = parse_address("", "", "", "")
            decision = None
            for person in parsed.people:
                pool = index.get((person.last or "").upper(), [])
                if not pool:
                    continue
                d = match_person_to_voters(person, empty, mailing, county, pool)
                if d.accepted:
                    decision = d
                    break
                decision = decision or d
            if decision is None:
                verdicts["NO_PHONE"] += 1
                rejected.append({**s, "verdict": "NO_MATCH",
                                 "why": "surname absent from county roll"})
                continue
            if not decision.accepted:
                verdicts["NO_PHONE"] += 1
                rejected.append({**s, "verdict": "NO_MATCH", "why": decision.reason})
                continue
            normalized = normalize_us_phone(decision.phone
                                            or getattr(decision.voter, "phone_raw", "") or "")
            phone = getattr(normalized, "e164", "") if normalized else ""
            if not phone:
                verdicts["IDENTITY_ONLY_NO_PHONE_ON_RECORD"] += 1
                rejected.append({**s, "verdict": "IDENTITY_CORROBORATED_NO_PHONE",
                                 "why": "voter record carries no phone"})
                continue
            verdicts["HIGH_PHONE"] += 1
            accepted.append({**s, "verdict": "HIGH", "tier": decision.tier,
                             "confidence": decision.confidence, "why": decision.reason,
                             "phone": phone,
                             "voter_identity": getattr(decision.voter, "identity_hash", ""),
                             "source_hash": digest, "observed_at": observed})

    report = {
        "generated_at": utcnow(),
        "PARTIES_ACCEPTED": len(subjects),
        "SUBJECTS_FILTERED_TO": (len(wanted) if wanted is not None else None),
        "PARTIES_CONTACT_ATTEMPTED": len(subjects),
        "HIGH_PHONE": verdicts.get("HIGH_PHONE", 0),
        "CONTACT_ABSENT": verdicts.get("NO_PHONE", 0)
                              + verdicts.get("IDENTITY_ONLY_NO_PHONE_ON_RECORD", 0),
        "HELD": verdicts.get("HELD_NO_VOTER_FILE", 0),
        "EMAIL": 0,
        "PROVIDER_SPEND_USD": "0.00",
        "verdicts": dict(verdicts),
        "counties": {c: len(g) for c, g in sorted(by_county.items())},
    }
    (out / "T22_CONTACT_CASCADE.json").write_text(json.dumps(report, indent=1))
    (out / "_accepted.json").write_text(json.dumps(accepted, indent=1))
    (out / "_rejected.json").write_text(json.dumps(rejected, indent=1))
    for n in ("T22_CONTACT_CASCADE.json", "_accepted.json", "_rejected.json"):
        os.chmod(out / n, 0o600)
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

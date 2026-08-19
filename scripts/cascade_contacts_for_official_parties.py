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
    entity_relationships, estate_relationships, load_import_csv, parse_source_document,
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
    args = ap.parse_args()

    out = prepare_output_dir(args.out_dir, what="T22 contact cascade output")
    t22 = Path(args.t22_dir).expanduser()
    ncsbe = Path(args.ncsbe_dir).expanduser()

    estate_rows = load_import_csv(t22 / "NC_ESTATE_IDENTITY_IMPORT.csv")
    entity_rows = load_import_csv(t22 / "NC_ENTITY_IDENTITY_IMPORT.csv")

    # Each subject keeps the county and mailing address its own source stated;
    # that is what makes an address-corroborated match possible at all.
    subjects = []
    for row in estate_rows:
        meta = parse_source_document(row.get("source_document") or "")
        if meta["confidence"] not in {"EXACT", "HIGH"}:
            continue
        person = ((row.get("personal_representative_name") or "").strip()
                  or (row.get("heir_name") or "").strip())
        if not person:
            continue
        subjects.append({
            "candidate_id": (row.get("property_candidate_id") or "").strip(),
            "person": person,
            "county": (row.get("county") or "").strip(),
            "mailing_address": (row.get("mailing_address") or "").strip(),
            "mailing_city": (row.get("mailing_city") or "").strip(),
            "mailing_state": (row.get("mailing_state") or "").strip(),
            "mailing_zip": (row.get("mailing_zip") or "").strip(),
            "origin": "T22_ESTATE",
        })
    for row in entity_rows:
        meta = parse_source_document(row.get("source_document") or "")
        if meta["confidence"] not in {"EXACT", "HIGH"}:
            continue
        person = (row.get("official_name") or "").strip()
        if not person:
            continue
        subjects.append({
            "candidate_id": (row.get("property_candidate_id") or "").strip(),
            "person": person,
            "county": (row.get("county") or "").strip(),
            "mailing_address": (row.get("mailing_office") or "").strip(),
            "mailing_city": "", "mailing_state": "", "mailing_zip": "",
            "origin": "T22_ENTITY",
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
        "T22_PARTIES_ACCEPTED": len(subjects),
        "T22_PARTIES_CONTACT_ATTEMPTED": len(subjects),
        "T22_HIGH_PHONE": verdicts.get("HIGH_PHONE", 0),
        "T22_CONTACT_ABSENT": verdicts.get("NO_PHONE", 0)
                              + verdicts.get("IDENTITY_ONLY_NO_PHONE_ON_RECORD", 0),
        "T22_HELD": verdicts.get("HELD_NO_VOTER_FILE", 0),
        "T22_EMAIL": 0,
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

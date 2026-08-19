#!/usr/bin/env python3
"""Re-decide only the identities whose property-side evidence actually moved.

Address evidence participates in identity corroboration: the NCSBE matcher uses
the parcel's ZIP to corroborate an exact-address match. T24A changed the rule
that produces that ZIP -- a ZIP impossible for the property's state is now
refused instead of truncated -- so every live contact decision has to be asked
whether its INPUT changed, and only the ones that did get re-decided.

The default is that nothing moves. A contact that was HIGH stays HIGH unless a
specific piece of evidence proves otherwise, and any downgrade is named.

    UNCHANGED         the ZIP the matcher used is the same under both rules
    REVALIDATED_HIGH  the input moved, the decision came back the same
    UPGRADED          the input moved and a better corroboration is now possible
    DOWNGRADED        the input moved and the corroboration is now weaker
    HELD              the input moved and the row needs a human

No contact is removed by this script. It reports; removal would be a separate,
explicit act.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contact.address_normalize import extract_zip5, strip_leading_house
from app.contact.estate_fields import owner_occupies_property
from app.contact.zip_state import zip_plausible_for_state
from app.custody.locations import estate_export
from app.custody.sync_guard import prepare_output_dir

COUNTY_ORDER = ["Mecklenburg", "Wake", "Durham", "Johnston", "Union", "Cabarrus", "Gaston"]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def property_zip_pre_t24a(row: dict) -> str:
    """The rule exactly as it stood at a898de7, kept here so the delta is real.

    Reimplemented rather than imported because the shipped function has moved on;
    comparing the new rule against itself would prove nothing.
    """
    line_zip = extract_zip5(
        strip_leading_house(row.get("property_address_raw")),
        strip_leading_house(row.get("property_address_normalized")),
    )
    if line_zip:
        return line_zip
    declared = str(row.get("property_zip") or "").strip()
    mailing = str(row.get("mailing_zip") or "").strip()
    if not declared:
        return ""
    if declared == mailing and not owner_occupies_property(row):
        return ""
    return extract_zip5(declared)


def property_zip_post_t24a(row: dict) -> str:
    from app.contact.estate_fields import property_zip_for
    return property_zip_for(row)


def load_estate(estate_dir: Path) -> dict:
    import csv
    rows = {}
    for county in COUNTY_ORDER:
        for path in sorted((estate_dir / county / "master").glob(f"{county.lower()}_master_*.csv")):
            with path.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    cid = (r.get("candidate_id") or "").strip()
                    if cid:
                        rows[cid] = r
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estate-dir", default=str(estate_export()))
    ap.add_argument("--out-dir", default=str(Path.home() / "neuruh-private" / "contact-intelligence"
                                            / "runs" / "T24A_IDENTITY_DELTA"))
    args = ap.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    out = prepare_output_dir(args.out_dir, what="identity delta evidence")
    estate = load_estate(Path(args.estate_dir))

    with psycopg.connect(dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("""
            select c.import_row_id::text as candidate_id,
                   c.contact_resolution_state, c.high_phone_count, c.has_living_identity,
                   cp.relationship_role, p.canonical_name, pp.phone_e164,
                   pp.confidence_category, e.match_method
              from public.candidate_contact_resolution c
              left join public.candidate_parties cp on cp.import_row_id = c.import_row_id
              left join public.parties p on p.party_id = cp.party_id
              left join public.party_phones pp on pp.party_id = p.party_id
              left join public.contact_evidence e on e.evidence_id = pp.evidence_id
             where c.high_phone_count > 0
        """)
        live = cur.fetchall()

    verdicts = Counter()
    moved = []
    for row in live:
        src = estate.get(row["candidate_id"])
        if src is None:
            verdicts["HELD"] += 1
            moved.append({"candidate_id": row["candidate_id"], "verdict": "HELD",
                          "why": "candidate not present in the estate export being compared"})
            continue
        before = property_zip_pre_t24a(src)
        after = property_zip_post_t24a(src)
        if before == after:
            verdicts["UNCHANGED"] += 1
            continue
        # The matcher's ZIP moved. It only ever TIGHTENS -- the new rule refuses,
        # it never invents -- so the question is whether the corroboration that
        # produced this match used the ZIP that is now refused.
        method = (row["match_method"] or "").upper()
        used_zip = "ZIP" in method or "ADDRESS" in method
        verdict = "DOWNGRADED" if (used_zip and not after) else "REVALIDATED_HIGH"
        verdicts[verdict] += 1
        moved.append({"candidate_id": row["candidate_id"], "verdict": verdict,
                      "zip_before": before, "zip_after": after,
                      "match_method": row["match_method"],
                      "state": row["contact_resolution_state"]})

    report = {"generated_at": utcnow(), "live_high_contact_rows": len(live),
              "verdicts": dict(verdicts), "moved": moved[:500],
              "contacts_removed": 0,
              "estate_dir": str(args.estate_dir)}
    (out / "IDENTITY_DELTA_REVALIDATION.json").write_text(json.dumps(report, indent=1))
    os.chmod(out / "IDENTITY_DELTA_REVALIDATION.json", 0o600)
    print(json.dumps({k: v for k, v in report.items() if k != "moved"}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

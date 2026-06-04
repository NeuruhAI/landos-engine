"""
ingest_owner_leads.py — pull parcel ownership from ReportAll into landos.owner_leads.

    python -m scripts.ingest_owner_leads --county berrien --limit 150
    python -m scripts.ingest_owner_leads --county berrien --limit 150 \
        --csv data/lead_queue_berrien.csv --seed-apns 11-54-0340-0145-00-9,07-5620-0075-00-8

MULTI-COUNTY: --county drives county_fips (via COUNTY_CONFIG). The APN source CSV
defaults to data/lead_queue_<county>.csv. --seed-apns guarantees specific parcels
are included first (e.g. the trial parcels), then the ranked queue fills up to
--limit. Real ReportAll calls only; misses (count=0) are skipped, never invented.
"""
import argparse
import csv
import os
from typing import List

import psycopg

from app.config import DATABASE_URL
from app.normalizers.reportall_owner import COUNTY_CONFIG, ReportAllOwnerNormalizer
from scripts.owner_leads import county_alias_to_fips

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def read_apns(csv_path: str) -> List[str]:
    apns: List[str] = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            apn = (row.get("apn") or "").strip()
            if apn:
                apns.append(apn)
    return apns


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", required=True)
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--csv", default=None)
    ap.add_argument("--seed-apns", default="", help="comma-separated APNs to include first")
    args = ap.parse_args()

    fips = county_alias_to_fips(args.county)
    if not fips:
        raise SystemExit(
            f"Unknown county {args.county!r}. Known: "
            + ", ".join(f"{c['region']} ({f})" for f, c in COUNTY_CONFIG.items())
        )

    csv_path = args.csv or os.path.join(
        os.path.expanduser("~/Documents/landos-engine/data"),
        f"lead_queue_{args.county.lower()}.csv",
    )
    if not os.path.exists(csv_path):
        raise SystemExit(f"APN source CSV not found: {csv_path}")

    seeds = [a.strip() for a in args.seed_apns.split(",") if a.strip()]
    queue = read_apns(csv_path)
    # seeds first (guaranteed), then ranked queue; dedupe preserving order; cap at limit
    ordered, seen = [], set()
    for a in seeds + queue:
        if a not in seen:
            seen.add(a)
            ordered.append(a)
    apns = ordered[: args.limit]

    print(f"county={args.county} fips={fips}  source={os.path.basename(csv_path)}  "
          f"seeds={len(seeds)}  selected={len(apns)} APNs (limit={args.limit})")

    norm = ReportAllOwnerNormalizer(TENANT, fips, apns)
    with psycopg.connect(DATABASE_URL) as conn:
        n = norm.run(conn)
    print(f"Done. {n} owner leads upserted from {len(apns)} APNs "
          f"({len(norm.misses)} misses, {norm.calls_made} ReportAll API calls).")
    if norm.misses:
        print(f"  misses (count=0, no record — not fabricated): {len(norm.misses)}")
        print("   " + ", ".join(norm.misses[:12]) + (" ..." if len(norm.misses) > 12 else ""))


if __name__ == "__main__":
    main()

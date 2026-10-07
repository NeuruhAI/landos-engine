#!/usr/bin/env python3
"""T21 — build the NC Resolution Factory artifacts from existing validated output.

Reads the CURRENT estate and the T20D validated contact bundle, assembles one
ResolutionRecord per candidate, and writes the operator-facing artifacts.

Re-matches nothing. Calls no provider. Mutates no production row. Writes only
outside Git.

    python scripts/run_nc_resolution_factory.py \
        --estate-dir ~/neuruh-private/contact-intelligence/estate/DEEDSONAR_CONTACT_EXPORT_PREVIEW_20260817 \
        --bundle-dir ~/neuruh-private/contact-intelligence/CANONICAL \
        --out-dir ~/neuruh-workspace/T21_NC_RESOLUTION_FACTORY_V2_20260819
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.custody.locations import canonical_bundle, estate_export
from app.contact.identity_tuple import estate_revision, master_csv_paths
from app.resolution.factory import (
    FACTORY_VERSION,
    QUALIFIED_BOARD_COLUMNS,
    build_records,
    load_csv,
    to_next_source_rows,
    to_qualified_board,
    to_yield_records,
)
from app.resolution.quality import QUALITY_COLUMNS, run_quality_court
from app.resolution.router import NEXT_SOURCE_COLUMNS, OPERATOR_QUEUES, QUEUE_CONTACT_STATUS
from app.resolution.sources import (
    SOURCE_MATRIX_COLUMNS,
    SOURCE_REGISTRY_VERSION,
    all_sources,
    entity_sources,
    estate_sources,
)
from app.resolution.tier_c import current_status as tier_c_status
from app.resolution.yield_engine import (
    RECIPE_INTEL_COLUMNS,
    YIELD_COLUMNS,
    build_yield,
    recipe_contact_intelligence,
)

COUNTY_ORDER = ["Mecklenburg", "Wake", "Durham", "Johnston", "Union", "Cabarrus", "Gaston"]

# Which source families need an operator rather than a cron.
MANUAL_SOURCES = {
    "ESTATE_IMPORT": "MANUAL",
    "SOS_IMPORT": "MANUAL",
    "SOS_IMPORT (Non-Profits in each county)": "MANUAL",
    "ESTATE_IMPORT / RECORDED_DEED": "MANUAL",
    "PRECISION_COURT": "MANUAL",
    "PRECISION_COURT (certified, not armed)": "MANUAL",
    "TRIAGE": "MANUAL",
    "NOT_A_SELLER": "MANUAL",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_csv(path: Path, rows, columns) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def load_estate(estate_dir: Path):
    rows = []
    for county in COUNTY_ORDER:
        for path in sorted((estate_dir / county / "master").glob(f"{county.lower()}_master_*.csv")):
            rows.extend(load_csv(path))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--estate-dir", default=str(estate_export()))
    ap.add_argument("--bundle-dir", default=str(canonical_bundle()))
    ap.add_argument("--tier-c-ids", default="")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    estate_dir = Path(args.estate_dir).expanduser()
    bundle_dir = Path(args.bundle_dir).expanduser()
    out = Path(args.out_dir).expanduser()
    work = out / "_work"
    priv = out / "_private"
    for d in (out, work, priv):
        d.mkdir(parents=True, exist_ok=True)

    print(f"Loading CURRENT estate from {estate_dir}")
    estate = load_estate(estate_dir)
    revision = estate_revision(master_csv_paths(estate_dir))
    print(f"  CURRENT_TOTAL={len(estate)}  ESTATE_REVISION={revision}")

    append_rows = load_csv(bundle_dir / "phone_append_combined.csv")
    ledger = load_csv(bundle_dir / "CANDIDATE_CONTACT_RESOLUTION.csv")
    identity_path = bundle_dir / "CANDIDATE_IDENTITY_LEDGER.csv"
    identity_ledger = load_csv(identity_path) if identity_path.exists() else []
    if not identity_ledger:
        print(f"  WARNING: no identity ledger at {identity_path}. Living parties will be "
              f"limited to phone-bearing matches, which understates identity coverage.")
    print(f"  bundle rows={len(append_rows)} candidates={len({r['candidate_id'] for r in append_rows})} "
          f"unique_phones={len({r['phone'] for r in append_rows if r.get('phone')})}")
    print(f"  ledger rows={len(ledger)}")

    certified = []
    if args.tier_c_ids and Path(args.tier_c_ids).expanduser().exists():
        certified = [l.strip() for l in Path(args.tier_c_ids).expanduser().read_text().splitlines() if l.strip()]
    print(f"  tier_c certified ids={len(certified)}")

    print("Assembling resolution records…")
    records = build_records(estate, append_rows, ledger, revision,
                            tier_c_certified_ids=certified,
                            identity_ledger=identity_ledger)

    # ---- artifacts -------------------------------------------------------
    ns_rows = to_next_source_rows(records)
    write_csv(out / "NEXT_BEST_SOURCE_MATRIX.csv", ns_rows, NEXT_SOURCE_COLUMNS)

    board = to_qualified_board(records)
    write_csv(out / "QUALIFIED_RESOLUTION_BOARD.csv", board, QUALIFIED_BOARD_COLUMNS)

    write_csv(out / "SOURCE_REGISTRY.csv", [s.to_row() for s in all_sources()], SOURCE_MATRIX_COLUMNS)
    write_csv(out / "ESTATE_SOURCE_MATRIX.csv", [s.to_row() for s in estate_sources()], SOURCE_MATRIX_COLUMNS)
    write_csv(out / "ENTITY_SOURCE_MATRIX.csv", [s.to_row() for s in entity_sources()], SOURCE_MATRIX_COLUMNS)

    yrecs = to_yield_records(records)
    slices = build_yield(yrecs, manual_sources=MANUAL_SOURCES)
    write_csv(out / "SOURCE_YIELD_BY_OWNER_CLASS.csv", slices.get("OWNER_CLASS", []), YIELD_COLUMNS)
    write_csv(out / "SOURCE_YIELD_BY_COUNTY.csv", slices.get("COUNTY", []), YIELD_COLUMNS)
    write_csv(out / "SOURCE_YIELD_BY_RECIPE.csv", slices.get("RECIPE", []), YIELD_COLUMNS)
    write_csv(out / "RECIPE_CONTACT_INTELLIGENCE.csv", recipe_contact_intelligence(yrecs), RECIPE_INTEL_COLUMNS)

    findings = run_quality_court(estate)
    write_csv(out / "DATA_QUALITY_COURT.csv",
              [{**f.to_dict(), "sample": " | ".join(f.sample)} for f in findings], QUALITY_COLUMNS)

    # Operator queues: counts + the per-queue worklists (PII stays private).
    queue_counts = Counter(r.operator_queue for r in records)
    q_rows = [{
        "operator_queue": q,
        "contact_status": QUEUE_CONTACT_STATUS.get(q, ""),
        "candidates": queue_counts.get(q, 0),
        "qualified": sum(1 for r in records
                         if r.operator_queue == q and r.qualification_state.startswith("QUALIFIED")),
    } for q in OPERATOR_QUEUES]
    write_csv(out / "OPERATOR_QUEUES.csv", q_rows,
              ["operator_queue", "contact_status", "candidates", "qualified"])
    for q in OPERATOR_QUEUES:
        rows = [r for r in ns_rows if r["operator_queue"] == q]
        if rows:
            write_csv(priv / f"QUEUE_{q}.csv", rows, NEXT_SOURCE_COLUMNS)

    # ---- summary ---------------------------------------------------------
    qualified = [r for r in records if r.qualification_state.startswith("QUALIFIED")]
    summary = {
        "generated_at": utcnow(),
        "factory_version": FACTORY_VERSION,
        "source_registry_version": SOURCE_REGISTRY_VERSION,
        "estate_revision": revision,
        "estate_dir": str(estate_dir),
        "bundle_dir": str(bundle_dir),
        "NC_CURRENT": len(records),
        "NC_QUALIFIED": len(qualified),
        "BUNDLE_ROWS": len(append_rows),
        "BUNDLE_CANDIDATES": len({r["candidate_id"] for r in append_rows}),
        "BUNDLE_UNIQUE_PHONES": len({r["phone"] for r in append_rows if r.get("phone")}),
        "CANDIDATES_WITH_HIGH_PHONE": sum(1 for r in records if r.high_phones),
        "IDENTITY_LEDGER_ROWS": len(identity_ledger),
        "CANDIDATES_WITH_LIVING_IDENTITY": sum(1 for r in records if r.has_living_identity),
        "CANDIDATES_WITH_NAMED_LIVING_PARTY_NO_PHONE": sum(
            1 for r in records if r.has_living_identity and not r.high_phones),
        "QUALIFIED_WITH_LIVING_IDENTITY": sum(1 for r in qualified if r.has_living_identity),
        "QUALIFIED_WITH_CONTACT": sum(1 for r in qualified if r.high_phones),
        "OWNER_CLASS_COUNTS": dict(Counter(r.owner_class for r in records)),
        "OWNER_CONDITION_COUNTS": dict(Counter(r.owner_condition for r in records)),
        "OPERATOR_QUEUE_COUNTS": dict(queue_counts),
        "ESTATE_ROWS": sum(1 for r in records
                           if r.owner_class in {"DECEASED", "ESTATE", "HEIRS"}),
        "ESTATE_QUALIFIED": sum(1 for r in qualified
                                if r.owner_class in {"DECEASED", "ESTATE", "HEIRS"}),
        "ENTITY_ROWS": sum(1 for r in records
                           if r.owner_class in {"LLC", "CORPORATION", "OTHER_ENTITY", "NONPROFIT"}),
        "ENTITY_QUALIFIED": sum(1 for r in qualified
                                if r.owner_class in {"LLC", "CORPORATION", "OTHER_ENTITY", "NONPROFIT"}),
        "TRUST_ROWS": sum(1 for r in records if r.owner_class == "TRUST"),
        "TRUSTEES_RESOLVED": sum(1 for r in records
                                 if any(rel.role == "TRUSTEE" for rel in r.relationships)),
        "TIER_C": tier_c_status(),
        "DATA_QUALITY": [f.to_dict() for f in findings],
        "PROVIDER_SPEND_USD": "0.00",
        "PRODUCTION_MUTATED": "NO",
        "CALLABILITY_STATUS": "NOT_AUTHORIZED",
    }
    (out / "RESOLUTION_FACTORY_SUMMARY.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    print(json.dumps({k: v for k, v in summary.items()
                      if k not in {"DATA_QUALITY", "TIER_C"}}, indent=2, sort_keys=True))
    print(f"WROTE {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

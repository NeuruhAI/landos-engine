#!/usr/bin/env python3
"""Dry-run the estate-import CSV contract. Never imports.

    python scripts/validate_estate_import.py --file tests/fixtures/estate/wake_estate_2026-08-28.csv
    python scripts/validate_estate_import.py --file /path/to/wake_estate_YYYY-MM-DD.csv --expect-placement
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contact.estate_import_validate import render_validator_report, validate_estate_import_file


def main() -> int:
    p = argparse.ArgumentParser(description="Dry-run estate import CSV against the importer contract. Does not import.")
    p.add_argument("--file", required=True, type=Path)
    p.add_argument(
        "--expect-placement",
        action="store_true",
        help="FAIL unless the file sits under _raw/estate/",
    )
    p.add_argument("--json", action="store_true", help="print JSON instead of the column table")
    p.add_argument("--report", type=Path, help="optional JSON report path")
    args = p.parse_args()

    report = validate_estate_import_file(args.file, expect_placement=args.expect_placement)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render_validator_report(report), end="")
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

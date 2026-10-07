#!/usr/bin/env python3
"""Parse AOC-E-201 / AOC-E-505 PDFs under `_raw/estate/pdf/<county>/` into the
importer CSV (`_raw/estate/<county>_estate_<YYYY-MM-DD>.csv`) with a receipt per PDF.

    python scripts/intake_estate_pdfs.py --raw-dir ~/.neuruh/private/deedsonar/_raw
    python scripts/intake_estate_pdfs.py --raw-dir ... --worklist ~/neuruh-workspace/ESTATE_WORKLIST_WAKE_20260902.csv

The worklist (global_rank, decedent_name, situs_address, apn, candidate_id, …)
binds a parsed decedent to its estate row by name; without it the CSV row is
written with an empty property_candidate_id and the receipt says so.
Never contacts a court. Never writes a phone. Idempotent per PDF sha256.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contact.estate_pdf_intake import intake_estate_pdfs  # noqa: E402


def lookup_from_worklist(path: Path | None) -> dict[str, tuple[str, str]]:
    if not path or not path.exists():
        return {}
    out: dict[str, tuple[str, str]] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            name = re.sub(r"[^A-Z]", "", (row.get("decedent_name") or "").upper().replace("HEIRS", ""))
            if name and row.get("candidate_id"):
                out[name] = (row["candidate_id"], row.get("apn") or "")
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="AOC estate PDF intake (receipt per PDF; never writes a phone).")
    p.add_argument("--raw-dir", required=True, type=Path)
    p.add_argument("--worklist", type=Path, default=None)
    p.add_argument("--json", action="store_true")
    args = p.parse_args()
    receipts = intake_estate_pdfs(args.raw_dir, candidate_lookup=lookup_from_worklist(args.worklist))
    if args.json:
        print(json.dumps([r.__dict__ for r in receipts], indent=2, default=str))
    else:
        for r in receipts:
            print(f"{r.state:34} {Path(r.pdf).name}  form={r.form or '-'} case={r.case_number or '-'} people={r.people_written} attorney_ignored={r.attorney_names_ignored} phones_refused={r.phones_seen_and_refused}")
        print(f"ESTATE_PDF_INTAKE={len(receipts)} rows_written={sum(r.people_written for r in receipts)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

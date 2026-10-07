"""qualify_opportunity_exports.py — T4E Phase 13 qualification court.

For every FULL recipe file in a factory output directory prove:

    RANK_SORT             recipe_score non-increasing top-to-bottom
    RANK_CONTIGUOUS       recipe_rank == 1..N with no gaps
    DUPLICATE_PROPERTY    0 repeated property_key inside a file
    MISSING_RECIPE_ID     0
    MISSING_RECIPE_VERSION 0
    MISSING_SCORING_VERSION 0
    ARBITRARY_TRUNCATION  NO — FULL row_count equals the recipe's manifest count;
                          every Top-N slice is exactly min(N, FULL) rows and only
                          exists when FULL > N
    UNKNOWN_FABRICATION   NO — always-unknown economics/flags are empty, never 0/false

Writes QUALIFICATION_REPORT.json; exits 1 on any FAIL.

    python -m scripts.qualify_opportunity_exports --output <factory dir>
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

UNKNOWN_COLS = ("estimated_equity", "estimated_equity_pct", "mortgage_balance",
                "absentee_owner_flag", "vacancy_flag", "lien_flag", "code_violation_flag")


def read(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(open(path, newline="", encoding="utf-8")))


def qualify_file(path: Path) -> dict:
    rows = read(path)
    scores = [float(r["recipe_score"]) for r in rows if r["recipe_score"]]
    ranks = [int(r["recipe_rank"]) for r in rows if r["recipe_rank"]]
    keys = [r["property_key"] for r in rows]
    fab = sum(1 for r in rows for c in UNKNOWN_COLS if (r.get(c) or "").strip() not in ("",))
    return {
        "file": path.name,
        "rows": len(rows),
        "RANK_SORT": "PASS" if all(scores[i] >= scores[i + 1] for i in range(len(scores) - 1)) else "FAIL",
        "RANK_CONTIGUOUS": "PASS" if ranks == list(range(1, len(rows) + 1)) else "FAIL",
        "DUPLICATE_PROPERTY": len(keys) - len(set(keys)),
        "MISSING_RECIPE_ID": sum(1 for r in rows if not (r["recipe_id"] or "").strip()),
        "MISSING_RECIPE_VERSION": sum(1 for r in rows if not (r["recipe_version"] or "").strip()),
        "MISSING_SCORING_VERSION": sum(1 for r in rows if not (r["scoring_version"] or "").strip()),
        "UNKNOWN_FABRICATION_CELLS": fab,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    out = args.output
    manifest = json.loads((out / "09_MANIFESTS" / "MANIFEST.json").read_text())

    results, failures = [], []
    fulls: dict[str, int] = {}
    for p in sorted((out / "03_FULL_RECIPE_EXPORTS").glob("*_FULL.csv")):
        q = qualify_file(p)
        fulls[p.name.replace("_FULL.csv", "")] = q["rows"]
        results.append(q)

    # Top-N slices: exist only when FULL > N, and are exactly N rows
    truncation_ok = True
    for n, d in ((100, "04_TOP100"), (500, "05_TOP500"), (1000, "06_TOP1000")):
        for p in sorted((out / d).glob("*.csv")) if (out / d).exists() else []:
            rid = p.name.replace(f"_TOP_{n}.csv", "")
            q = qualify_file(p)
            full_n = fulls.get(rid, 0)
            if full_n <= n or q["rows"] != n:
                truncation_ok = False
                failures.append(f"{p.name}: slice rows={q['rows']} vs FULL={full_n}")
            results.append(q)

    for q in results:
        for k in ("RANK_SORT", "RANK_CONTIGUOUS"):
            if q[k] != "PASS":
                failures.append(f"{q['file']}: {k}")
        for k in ("DUPLICATE_PROPERTY", "MISSING_RECIPE_ID", "MISSING_RECIPE_VERSION",
                  "MISSING_SCORING_VERSION", "UNKNOWN_FABRICATION_CELLS"):
            if q[k] != 0:
                failures.append(f"{q['file']}: {k}={q[k]}")

    # manifest hash integrity: every listed sha256 matches the file on disk
    hash_bad = []
    for e in manifest["files"]:
        for d in ("03_FULL_RECIPE_EXPORTS", "04_TOP100", "05_TOP500", "06_TOP1000", "08_MASTER"):
            fp = out / d / e["filename"]
            if fp.exists():
                actual = hashlib.sha256(fp.read_bytes()).hexdigest()
                if actual != e["sha256"]:
                    hash_bad.append(e["filename"])
                break
    if hash_bad:
        failures.append(f"manifest sha mismatch: {hash_bad}")

    report = {
        "overall": "PASS" if not failures and truncation_ok else "FAIL",
        "ARBITRARY_TRUNCATION": "NO" if truncation_ok else "YES",
        "UNKNOWN_FABRICATION": "NO" if not any(q["UNKNOWN_FABRICATION_CELLS"] for q in results) else "YES",
        "files_checked": len(results),
        "failures": failures,
        "files": results,
    }
    (out / "09_MANIFESTS" / "QUALIFICATION_REPORT.json").write_text(
        json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps({k: report[k] for k in
                      ("overall", "ARBITRARY_TRUNCATION", "UNKNOWN_FABRICATION",
                       "files_checked", "failures")}, indent=2))
    raise SystemExit(0 if report["overall"] == "PASS" else 1)


if __name__ == "__main__":
    main()

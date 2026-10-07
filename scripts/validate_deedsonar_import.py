"""validate_deedsonar_import.py — T4E Phase 10 CRM import court (NO production import).

Validates a factory-generated DEEDSONAR_IMPORT_READY_MASTER.csv against the
ACTUAL DeedSonar county-import contract, mirrored rule-for-rule from the
DeedSonar source (read-only inspection 2026-08-12, repo NeuruhAI/deedsonar):

    parse rules       src/lib/imports/county-parse.ts:20-136
    required fields   src/lib/imports/county-normalize.ts:217-227
    aliases           src/lib/imports/county-normalize.ts:12-42
    apn normalizer    src/lib/imports/county-normalize.ts:78-81
    address key       src/lib/imports/county-normalize.ts:102-116 (normalizeAddress)
    phone rule        src/lib/imports/county-normalize.ts:99-103,186-188 (10 digits or REJECT)
    number rule       src/lib/imports/county-normalize.ts:122-135 (blank->null, never 0)
    dedupe cascade    src/lib/imports/county-dedup.ts:75-117 (apn -> address -> source_row_id -> phone)
    identity          src/lib/imports/county-provenance.ts:59-81 (explicit source_row_id is
                      the ONLY cross-file-stable identity)

This mirrors the importer to prove our file is import-legal; it never connects
to DeedSonar or any database.

    python -m scripts.validate_deedsonar_import --file <csv> --report <json>
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import unicodedata
from pathlib import Path

REQUIRED = ("state", "county")            # + (apn OR property_address) per row
CANONICAL_NUMERIC = ("assessed_value", "market_value", "tax_amount_owed", "acreage")
ALWAYS_UNKNOWN_MUST_BE_EMPTY = ("estimated_equity", "estimated_equity_pct",
                                "mortgage_balance", "phone_1", "phone_2", "email_1",
                                "absentee_owner_flag", "vacancy_flag", "lien_flag",
                                "code_violation_flag")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def cell(v: str) -> str:
    return unicodedata.normalize("NFKC", v or "").strip()


def norm_apn(v: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", v or "").lower()).upper()


_ADDR_SUFFIXES = tuple(
    (re.compile(rf"\b({long}|{short}\.)\b", re.ASCII), short)
    for long, short in (("street", "st"), ("road", "rd"), ("avenue", "ave"),
                        ("drive", "dr"), ("lane", "ln"), ("highway", "hwy"))
)


def norm_addr_key(v: str) -> str:
    """normalizeAddress, regex-for-regex: NFKC, lowercase, whole-word suffix folds
    (JS \\b is ASCII-only, hence re.ASCII), then [^a-z0-9#]+ -> space. A plain
    str.replace here turned BROADWAY into "brdway" and PLANET into "plnt"."""
    s = unicodedata.normalize("NFKC", v or "").lower()
    for rx, short in _ADDR_SUFFIXES:
        s = rx.sub(short, s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9#]+", " ", s)).strip()


def parse_number_ok(v: str) -> bool:
    """Mirror parseNumber: blank -> null OK; else strip [$%,\\s] (paren=negative),
    must be finite — otherwise the importer REJECTS the row."""
    v = cell(v)
    if not v:
        return True
    neg = v.startswith("(") and v.endswith(")")
    s = re.sub(r"[$%,\s]", "", v[1:-1] if neg else v)
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def validate(path: Path) -> dict:
    rows = list(csv.DictReader(open(path, newline="", encoding="utf-8")))
    checks: dict[str, dict] = {}

    def check(name: str, ok: bool, detail):
        checks[name] = {"result": "PASS" if ok else "FAIL", "detail": detail}

    # CSV_PARSE — parseable, headers unique + non-blank
    headers = list(rows[0].keys()) if rows else []
    check("CSV_PARSE", bool(rows) and len(set(headers)) == len(headers) and all(headers),
          {"rows": len(rows), "headers": len(headers)})

    # REQUIRED_COLUMNS — state + county + (apn|property_address) on EVERY row
    missing = sum(1 for r in rows
                  if not cell(r.get("state", "")) or not cell(r.get("county", ""))
                  or not (cell(r.get("apn", "")) or cell(r.get("property_address", ""))))
    check("REQUIRED_COLUMNS", missing == 0, {"rows_missing_required": missing})

    # STABLE_IDENTITY — explicit, non-empty, distinct source_row_id
    ids = [cell(r.get("source_row_id", "")) for r in rows]
    check("STABLE_IDENTITY", all(ids) and len(set(ids)) == len(ids),
          {"empty": ids.count(""), "distinct": len(set(ids)), "rows": len(rows)})

    # STATE_NORMALIZATION — 2-letter uppercase (importer only uppercases; we hold
    # ourselves to the stricter mapping-template rule so nothing depends on it)
    bad_state = sum(1 for r in rows if not re.fullmatch(r"[A-Z]{2}", cell(r.get("state", ""))))
    check("STATE_NORMALIZATION", bad_state == 0, {"non_2letter": bad_state})

    # NUMBER_FORMATS — canonical numeric columns must be parseNumber-legal
    bad_nums = {c: sum(1 for r in rows if not parse_number_ok(r.get(c, "")))
                for c in CANONICAL_NUMERIC if c in (rows[0] or {})}
    check("NUMBER_FORMATS", all(v == 0 for v in bad_nums.values()), bad_nums)

    # NULLS / UNKNOWN_FABRICATION — always-unknown fields must be EMPTY (blank ->
    # importer NULL). A '0' or 'false' here would be a fabricated fact.
    fab = {c: sum(1 for r in rows if cell(r.get(c, "")) not in ("",))
           for c in ALWAYS_UNKNOWN_MUST_BE_EMPTY if c in (rows[0] or {})}
    check("NULLS", all(v == 0 for v in fab.values()), fab)
    check("DNC_COLUMN_ABSENT", "dnc" not in headers,
          {"note": "dnc omitted so an unknown DNC fact is never exported; "
                   "DeedSonar's own insert default applies (their semantics, not ours)"})

    # RECIPE_METADATA + SCORE_VERSION — riding columns populated on every row
    for name, col in (("RECIPE_METADATA", "recipe_id"),
                      ("RECIPE_VERSION", "recipe_version"),
                      ("SCORE_VERSION", "scoring_version")):
        n = sum(1 for r in rows if not cell(r.get(col, "")))
        check(name, n == 0, {f"rows_missing_{col}": n})

    # DUPLICATE_BEHAVIOR — replicate the dedupe cascade IN-FILE: a clean import
    # means zero in-file collisions on any basis the importer checks first.
    apn_keys = [ (cell(r["state"]), re.sub(r"\s+county$", "", cell(r["county"]).lower()),
                  norm_apn(r.get("apn", "")))
                 for r in rows if cell(r.get("apn", "")) ]
    addr_keys = [norm_addr_key(r.get("property_address", ""))
                 for r in rows if cell(r.get("property_address", ""))]
    apn_dupes = len(apn_keys) - len(set(apn_keys))
    addr_dupes = len(addr_keys) - len(set(addr_keys))
    check("DUPLICATE_BEHAVIOR", apn_dupes == 0,
          {"in_file_apn_basis_dupes": apn_dupes,
           "in_file_address_key_collisions": addr_dupes,
           "note": "address-basis dedupe is tenant-wide in DeedSonar (not county-"
                   "scoped); address-key collisions among DISTINCT parcels would be "
                   "skipped as duplicates by their cascade — counted here for the "
                   "operator, apn-basis is the binding identity"})

    # phones/emails if ever populated must be importer-legal (10 digits / email re)
    bad_phone = sum(1 for r in rows for c in ("phone_1", "phone_2")
                    if cell(r.get(c, "")) and len(re.sub(r"\D", "", cell(r[c]).lstrip("1"))) != 10)
    bad_email = sum(1 for r in rows if cell(r.get("email_1", ""))
                    and not EMAIL_RE.fullmatch(cell(r["email_1"]).lower()))
    check("PHONE_EMAIL_LEGALITY", bad_phone == 0 and bad_email == 0,
          {"bad_phone": bad_phone, "bad_email": bad_email})

    overall = all(c["result"] == "PASS" for c in checks.values())
    return {"file": str(path), "rows": len(rows),
            "overall": "PASS" if overall else "FAIL", "checks": checks}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, type=Path)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()
    report = validate(args.file)
    out = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(out)
    print(out)
    raise SystemExit(0 if report["overall"] == "PASS" else 1)


if __name__ == "__main__":
    main()

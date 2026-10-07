"""Dry-run validator for the estate-import CSV contract.

Checks a candidate file against `app.contact.estate_import` and reports
column-level pass/fail. It never writes product files, never mutates DNC,
never sets a phone callable, never fetches a URL, and never contacts a
court system. `parse_estate_import_rows` is used only as an in-memory
preview of who would be emitted.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .estate_import import (
    ESTATE_IMPORT_COLUMNS,
    ESTATE_IMPORT_COUNTY_FIPS,
    ESTATE_IMPORT_DATE_FORMAT,
    ESTATE_IMPORT_DELIMITER,
    ESTATE_IMPORT_ENCODING,
    ESTATE_IMPORT_FILENAME_RE,
    ESTATE_IMPORT_PATH_MARKER,
    ESTATE_IMPORT_TEMPLATE_NAME,
    FAMILY_RELATIONSHIPS,
    FORBIDDEN_RELATIONSHIPS,
    LEGAL_AUTHORITY_RELATIONSHIP,
    LIVING_PERSON_FIELDS,
    parse_estate_import_rows,
)

PHONEISH_COLUMNS = {
    "phone", "phone_1", "phone_2", "phone_3", "dnc", "dnc_status",
    "callability_status", "email", "email_1",
}
ZIP_RE = re.compile(r"^\d{5}(?:-\d{4})?$")
FIPS_RE = re.compile(r"^\d{5}$")
STATE_RE = re.compile(r"^[A-Z]{2}$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COUNTY_SLUGS = {name.lower(): name for name in ESTATE_IMPORT_COUNTY_FIPS}


def _check(result: str, detail: Any) -> dict:
    return {"result": result, "detail": detail}


def _cell(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


def _is_under_raw_estate(path: Path) -> bool:
    parts = [p.lower() for p in path.resolve().parts]
    try:
        idx = parts.index("estate")
    except ValueError:
        return False
    return idx > 0 and parts[idx - 1] == "_raw"


def filename_convention_status(name: str) -> Tuple[str, dict]:
    lower = name.lower()
    if lower == ESTATE_IMPORT_TEMPLATE_NAME or lower.endswith("template.csv") or lower.startswith("template"):
        return "FAIL", {
            "filename": name,
            "reason": "template files are never data; the runner skips *template.csv",
        }
    m = ESTATE_IMPORT_FILENAME_RE.match(name)
    if not m:
        return "FAIL", {
            "filename": name,
            "expected": "{county_slug}_estate_YYYY-MM-DD.csv",
            "example": "wake_estate_2026-08-28.csv",
        }
    slug = m.group("county")
    date_s = m.group("date")
    try:
        datetime.strptime(date_s, ESTATE_IMPORT_DATE_FORMAT)
    except ValueError:
        return "FAIL", {"filename": name, "reason": f"date {date_s!r} is not a real calendar day"}
    if slug not in COUNTY_SLUGS:
        return "FAIL", {
            "filename": name,
            "reason": f"county slug {slug!r} is not a CURRENT NC estate county",
            "allowed": sorted(COUNTY_SLUGS),
        }
    return "PASS", {"filename": name, "county": COUNTY_SLUGS[slug], "transcribed_on": date_s}


def _decode_utf8(path: Path) -> Tuple[Optional[str], Optional[str]]:
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return None, "UTF-8 BOM present; DictReader would rename property_candidate_id"
    try:
        return raw.decode(ESTATE_IMPORT_ENCODING), None
    except UnicodeDecodeError as exc:
        return None, f"not {ESTATE_IMPORT_ENCODING}: {exc}"


def _delimiter_ok(header_line: str) -> Tuple[bool, dict]:
    comma_n = header_line.count(ESTATE_IMPORT_DELIMITER)
    tab_n = header_line.count("\t")
    if tab_n > comma_n and tab_n >= 2:
        return False, {"delimiter": "tab", "expected": "comma"}
    if comma_n < len(ESTATE_IMPORT_COLUMNS) - 1:
        return False, {
            "comma_count": comma_n,
            "expected_min": len(ESTATE_IMPORT_COLUMNS) - 1,
            "delimiter": "not_comma_or_truncated_header",
        }
    return True, {"delimiter": "comma", "comma_count": comma_n}


def _column_format_issue(col: str, value: str) -> Optional[str]:
    if not value:
        return None
    if col == "source_date":
        if not ISO_DATE_RE.match(value):
            return "not_YYYY-MM-DD"
        try:
            datetime.strptime(value, ESTATE_IMPORT_DATE_FORMAT)
        except ValueError:
            return "invalid_calendar_date"
        return None
    if col == "fips":
        return None if FIPS_RE.match(value) else "not_5_digit_fips"
    if col == "mailing_state":
        return None if STATE_RE.match(value) else "not_2_letter_uppercase_state"
    if col == "mailing_zip":
        return None if ZIP_RE.match(value) else "not_ZIP5_or_ZIP+4"
    return None


def validate_estate_import_file(
    path: str | Path,
    *,
    expect_placement: bool = False,
) -> dict:
    """Column-level dry-run. Never imports."""
    p = Path(path)
    checks: Dict[str, dict] = {}
    columns: Dict[str, dict] = {
        col: {
            "header": "FAIL",
            "populated": 0,
            "empty": 0,
            "format": "PASS",
            "format_failures": 0,
        }
        for col in ESTATE_IMPORT_COLUMNS
    }

    report: dict = {
        "file": str(p),
        "imported": False,
        "rows": 0,
        "preview_people": 0,
        "overall": "FAIL",
        "checks": checks,
        "columns": columns,
    }

    if not p.exists() or not p.is_file():
        checks["FILE_EXISTS"] = _check("FAIL", {"path": str(p)})
        return report
    checks["FILE_EXISTS"] = _check("PASS", {"bytes": p.stat().st_size})

    fn_result, fn_detail = filename_convention_status(p.name)
    checks["FILENAME"] = _check(fn_result, fn_detail)

    if expect_placement:
        placed = _is_under_raw_estate(p)
        checks["PATH"] = _check(
            "PASS" if placed else "FAIL",
            {"required": ESTATE_IMPORT_PATH_MARKER, "under_raw_estate": placed},
        )
    else:
        checks["PATH"] = _check(
            "SKIP",
            {"required": ESTATE_IMPORT_PATH_MARKER, "note": "placement not required for file-shape dry-run"},
        )

    text, enc_err = _decode_utf8(p)
    if enc_err:
        checks["ENCODING"] = _check("FAIL", {"encoding": ESTATE_IMPORT_ENCODING, "reason": enc_err})
        checks["DELIMITER"] = _check("FAIL", {"reason": "file not decoded"})
        checks["HEADER"] = _check("FAIL", {"reason": "file not decoded"})
        return report
    checks["ENCODING"] = _check("PASS", {"encoding": ESTATE_IMPORT_ENCODING, "bom": False})

    lines = text.splitlines()
    if not lines:
        checks["DELIMITER"] = _check("FAIL", {"reason": "empty file"})
        checks["HEADER"] = _check("FAIL", {"reason": "empty file"})
        return report

    delim_ok, delim_detail = _delimiter_ok(lines[0])
    checks["DELIMITER"] = _check("PASS" if delim_ok else "FAIL", delim_detail)
    if not delim_ok:
        checks["HEADER"] = _check("FAIL", {"reason": "delimiter failed; header not scored"})
        return report

    reader = csv.DictReader(io.StringIO(text), delimiter=ESTATE_IMPORT_DELIMITER)
    headers = list(reader.fieldnames or [])
    if any(h is None or h.strip() == "" for h in headers):
        checks["HEADER"] = _check("FAIL", {"reason": "blank header name", "headers": headers})
        return report
    if len(headers) != len(set(headers)):
        checks["HEADER"] = _check("FAIL", {"reason": "duplicate header names", "headers": headers})
        return report

    missing = [c for c in ESTATE_IMPORT_COLUMNS if c not in headers]
    extra = [h for h in headers if h not in ESTATE_IMPORT_COLUMNS]
    order_ok = headers[: len(ESTATE_IMPORT_COLUMNS)] == list(ESTATE_IMPORT_COLUMNS)
    header_ok = not missing
    checks["HEADER"] = _check(
        "PASS" if header_ok else "FAIL",
        {
            "header_is_first_row": True,
            "missing": missing,
            "extra": extra,
            "order_matches_contract": order_ok,
        },
    )
    for col in ESTATE_IMPORT_COLUMNS:
        columns[col]["header"] = "PASS" if col in headers else "FAIL"

    forbidden_extra = [h for h in extra if h.strip().lower() in PHONEISH_COLUMNS]
    checks["NO_PHONE_OR_DNC_COLUMNS"] = _check(
        "FAIL" if forbidden_extra else "PASS",
        {"forbidden_extra": forbidden_extra, "other_extra": [h for h in extra if h not in forbidden_extra]},
    )

    rows = list(reader)
    report["rows"] = len(rows)
    checks["DATA_ROWS"] = _check(
        "PASS" if rows else "FAIL",
        {"rows": len(rows), "note": "header-only files are templates, not imports"},
    )

    date_failures = 0
    court_missing = 0
    family_without_case = 0
    authority_claimed = 0
    forbidden_rel = 0
    decedent_as_target = 0
    no_living_person = 0
    fips_mismatch = 0
    mailing_incomplete = 0

    for row in rows:
        for col in ESTATE_IMPORT_COLUMNS:
            value = _cell(row, col)
            if value:
                columns[col]["populated"] += 1
            else:
                columns[col]["empty"] += 1
            issue = _column_format_issue(col, value)
            if issue:
                columns[col]["format"] = "FAIL"
                columns[col]["format_failures"] += 1
                if col == "source_date":
                    date_failures += 1

        county = _cell(row, "county")
        fips = _cell(row, "fips")
        expected_fips = ESTATE_IMPORT_COUNTY_FIPS.get(county)
        if expected_fips and fips and fips != expected_fips:
            fips_mismatch += 1
            columns["fips"]["format"] = "FAIL"
            columns["fips"]["format_failures"] += 1

        case_no = _cell(row, "estate_case_number")
        rel = _cell(row, "relationship").upper()
        decedent = _cell(row, "decedent_name")
        living_filled = [f for f in LIVING_PERSON_FIELDS if _cell(row, f)]

        if not living_filled:
            no_living_person += 1
        if living_filled and not case_no:
            court_missing += 1
        if living_filled and not all(
            _cell(row, k) for k in ("mailing_address", "mailing_city", "mailing_state", "mailing_zip")
        ):
            mailing_incomplete += 1
        if rel in FORBIDDEN_RELATIONSHIPS:
            forbidden_rel += 1
        if rel == LEGAL_AUTHORITY_RELATIONSHIP:
            authority_claimed += 1
        if rel in FAMILY_RELATIONSHIPS and not case_no:
            family_without_case += 1
        for field_name in living_filled:
            raw_name = _cell(row, field_name)
            if decedent and raw_name.upper() == decedent.upper():
                decedent_as_target += 1

    checks["DATE_FORMAT"] = _check(
        "PASS" if date_failures == 0 else "FAIL",
        {"format": "YYYY-MM-DD", "rows_failing": date_failures},
    )
    checks["COUNTY_FIPS"] = _check(
        "PASS" if fips_mismatch == 0 else "FAIL",
        {"mismatched_rows": fips_mismatch, "map": ESTATE_IMPORT_COUNTY_FIPS},
    )
    checks["COURT_RECORD_PRESENT"] = _check(
        "PASS" if court_missing == 0 else "FAIL",
        {
            "rows_with_living_person_and_no_case_number": court_missing,
            "note": "a living person without estate_case_number is not a court record",
        },
    )
    checks["FAMILY_IS_NOT_AUTHORITY"] = _check(
        "PASS" if family_without_case == 0 else "FAIL",
        {
            "family_rows_without_case_number": family_without_case,
            "note": "family labels without a court case never become HEIR / EXECUTOR / LEGAL_AUTHORITY",
        },
    )
    checks["LEGAL_AUTHORITY_NOT_ASSERTED"] = _check(
        "PASS" if authority_claimed == 0 else "FAIL",
        {
            "rows_claiming_LEGAL_AUTHORITY": authority_claimed,
            "note": "this importer does not write LEGAL_AUTHORITY; use import_packages/estate for court-adjudicated authority",
        },
    )
    checks["FORBIDDEN_RELATIONSHIP"] = _check(
        "PASS" if forbidden_rel == 0 else "FAIL",
        {"rows_skipped_as_attorney_counsel_lawyer_esq": forbidden_rel},
    )
    checks["DECEDENT_NOT_A_TARGET"] = _check(
        "PASS" if decedent_as_target == 0 else "FAIL",
        {"rows_copying_decedent_into_a_living_name_field": decedent_as_target},
    )
    checks["LIVING_PERSON_PRESENT"] = _check(
        "PASS" if no_living_person == 0 else "FAIL",
        {
            "rows_with_no_applicant_pr_or_heir": no_living_person,
            "living_fields": list(LIVING_PERSON_FIELDS),
        },
    )
    checks["HEIR_MAILING_PRESENT"] = _check(
        "PASS" if mailing_incomplete == 0 else "FAIL",
        {
            "living_rows_missing_mailing_line_city_state_or_zip": mailing_incomplete,
            "note": "Wake NCSBE phone fill is 0%; the heir mailing is the cross-county match key",
        },
    )

    for col, spec in columns.items():
        if spec["format_failures"] and spec["format"] != "FAIL":
            spec["format"] = "FAIL"

    preview_people = []
    if header_ok:
        preview_people = parse_estate_import_rows(rows)
    report["preview_people"] = len(preview_people)
    checks["PARSE_PREVIEW"] = _check(
        "PASS" if header_ok and preview_people else "FAIL",
        {
            "people": len(preview_people),
            "imported": False,
            "heir_person_ids": [ep.heir_person_id for ep in preview_people],
            "relationships": sorted({ep.relationship for ep in preview_people}),
            "note": "in-memory parse only; no product write, no NCSBE match, no DeedSonar projection",
        },
    )

    failing = [name for name, item in checks.items() if item["result"] == "FAIL"]
    col_header_fail = [c for c, spec in columns.items() if spec["header"] == "FAIL"]
    col_format_fail = [c for c, spec in columns.items() if spec["format"] == "FAIL"]
    report["overall"] = "PASS" if not failing and not col_header_fail and not col_format_fail else "FAIL"
    report["failing_checks"] = failing
    report["failing_column_headers"] = col_header_fail
    report["failing_column_formats"] = col_format_fail
    return report


def render_validator_report(report: dict) -> str:
    lines = [
        "ESTATE IMPORT DRY-RUN VALIDATOR",
        f"file: {report['file']}",
        f"imported: NO",
        f"rows: {report['rows']}",
        f"preview_people: {report['preview_people']}",
        "",
        "COLUMNS",
    ]
    for col in ESTATE_IMPORT_COLUMNS:
        spec = report["columns"][col]
        lines.append(
            f"  {col:32} header={spec['header']:4} populated={spec['populated']:<4} "
            f"empty={spec['empty']:<4} format={spec['format']}"
        )
    lines.append("")
    lines.append("CHECKS")
    for name, item in report["checks"].items():
        lines.append(f"  {name:32} {item['result']}")
    lines.append("")
    lines.append(f"OVERALL {report['overall']}")
    return "\n".join(lines) + "\n"

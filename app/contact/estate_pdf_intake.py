"""AOC-E-201 / AOC-E-505 PDF intake for the estate-import lane.

Founder instruction (2026-09-02): any AOC-E-201 (Application for Probate and
Letters) or AOC-E-505 (Affidavit for Collection of Personal Property of
Decedent) PDF dropped under `_raw/estate/pdf/<county>/` is parsed — decedent,
personal representative / applicant, heirs and devisees with addresses, case
number — into the importer CSV (`_raw/estate/<county>_estate_<YYYY-MM-DD>.csv`,
the exact `ESTATE_IMPORT_COLUMNS` contract) on the next run, with a receipt per
PDF. Attorney names are provenance only; attorney phones are never accepted;
no phone of any kind is written (the contract has no phone column and the
validator refuses one).

Text comes from `pdftotext -layout` (poppler) so no Python dependency is added.
A scanned PDF with no text layer yields PARSE_TEXT_EMPTY and nothing is written.
Idempotent: a PDF whose receipt already records the same sha256 is skipped.
Nothing here writes LEGAL_AUTHORITY, mutates DNC, or contacts a court system.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .estate_import import (
    ESTATE_IMPORT_COLUMNS,
    ESTATE_IMPORT_COUNTY_FIPS,
    ESTATE_IMPORT_DIRNAME,
    FORBIDDEN_RELATIONSHIPS,
)

PDF_DIRNAME = "pdf"
RECEIPT_SUFFIX = ".receipt.json"
SOURCE_DOCUMENT_BY_FORM = {
    "AOC-E-201": "NC_AOC_E_201_APPLICATION_FOR_PROBATE_AND_LETTERS",
    "AOC-E-505": "NC_AOC_E_505_AFFIDAVIT_COLLECTION_PERSONAL_PROPERTY",
}
FORM_MARKERS = {
    "AOC-E-201": ("AOC-E-201", "APPLICATION FOR PROBATE"),
    "AOC-E-505": ("AOC-E-505", "AFFIDAVIT FOR COLLECTION OF PERSONAL PROPERTY"),
}
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
CASE_RE = re.compile(r"\b(\d{2}\s?E\s?\d{3,6}(?:-\d{3})?)\b", re.IGNORECASE)
STATE_ZIP_RE = re.compile(r"^(?P<city>.+?)[,\s]+(?P<state>[A-Z]{2})[\s,]+(?P<zip>\d{5}(?:-\d{4})?)\s*$")
RELATIONSHIP_WORDS = (
    "SPOUSE", "WIFE", "HUSBAND", "WIDOW", "WIDOWER", "SON", "DAUGHTER", "CHILD",
    "BROTHER", "SISTER", "SIBLING", "MOTHER", "FATHER", "PARENT", "GRANDCHILD",
    "GRANDSON", "GRANDDAUGHTER", "NIECE", "NEPHEW", "HEIR", "DEVISEE", "BENEFICIARY",
)


@dataclass
class PdfIntakeReceipt:
    pdf: str
    sha256: str
    form: str
    parser: str
    state: str
    case_number: str = ""
    decedent_name: str = ""
    county: str = ""
    fields_found: List[str] = field(default_factory=list)
    fields_missing: List[str] = field(default_factory=list)
    people_written: int = 0
    attorney_names_ignored: int = 0
    phones_seen_and_refused: int = 0
    rows: List[Dict[str, str]] = field(default_factory=list)
    csv_path: str = ""
    intake_at: str = ""
    note: str = ""


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pdf_text(path: Path) -> Tuple[str, str]:
    """(text, parser). Empty text when poppler is missing or the PDF has no text layer."""
    exe = shutil.which("pdftotext")
    if not exe:
        return "", "PARSER_UNAVAILABLE(pdftotext)"
    try:
        out = subprocess.run([exe, "-layout", "-enc", "UTF-8", str(path), "-"], capture_output=True, text=True, timeout=120)
    except (subprocess.SubprocessError, OSError):
        return "", "PARSER_ERROR(pdftotext)"
    if out.returncode != 0:
        return "", f"PARSER_ERROR(pdftotext rc={out.returncode})"
    return out.stdout, "pdftotext-layout"


def detect_form(text: str) -> str:
    upper = text.upper()
    for form, markers in FORM_MARKERS.items():
        if any(m in upper for m in markers):
            return form
    return ""


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" :,")


def _after_label(text: str, labels: Iterable[str]) -> str:
    """The value on the line after a label (forms print the label, then the answer)."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        u = line.upper()
        for label in labels:
            if label in u:
                rest = _clean(line.split(":", 1)[1]) if ":" in line and u.index(label) < line.index(":") else ""
                if rest and not rest.upper().startswith(("NAME", "FILE", "COUNTY")):
                    return rest
                for j in range(i + 1, min(i + 3, len(lines))):
                    cand = _clean(lines[j])
                    if cand and not any(k in cand.upper() for k in ("NAME OF", "FILE NO", "COUNTY", "STATE OF NORTH CAROLINA", "IN THE MATTER OF")):
                        return cand
    return ""


def _split_address(raw: str) -> Dict[str, str]:
    """'300 FIXTURE AVE, CHARLOTTE, NC 28205' -> address/city/state/zip. Unknown parts stay ''."""
    parts = [p.strip() for p in re.split(r",", raw) if p.strip()]
    out = {"mailing_address": "", "mailing_city": "", "mailing_state": "", "mailing_zip": ""}
    if not parts:
        return out
    tail = parts[-1]
    m_state_zip = re.match(r"^(?P<state>[A-Z]{2})[\s,]+(?P<zip>\d{5}(?:-\d{4})?)\s*$", tail)
    if m_state_zip:
        out["mailing_state"] = m_state_zip.group("state")
        out["mailing_zip"] = m_state_zip.group("zip")
        out["mailing_city"] = parts[-2] if len(parts) >= 2 else ""
        out["mailing_address"] = ", ".join(parts[:-2]) if len(parts) > 2 else ""
        return out
    m = STATE_ZIP_RE.match(tail)
    if m:
        out["mailing_state"] = m.group("state")
        out["mailing_zip"] = m.group("zip")
        out["mailing_city"] = _clean(m.group("city"))
        out["mailing_address"] = ", ".join(parts[:-1])
        return out
    out["mailing_address"] = ", ".join(parts[:-1]) if len(parts) > 1 else parts[0]
    out["mailing_city"] = parts[-1] if len(parts) > 1 else ""
    return out


def _relationship_in(line: str) -> str:
    u = line.upper()
    for word in RELATIONSHIP_WORDS:
        if re.search(rf"\b{word}\b", u):
            return {"HEIR": "HEIR", "DEVISEE": "HEIR", "BENEFICIARY": "HEIR", "WIFE": "SPOUSE", "HUSBAND": "SPOUSE"}.get(word, word)
    return ""


def parse_heirs_block(text: str) -> List[Dict[str, str]]:
    """Rows of the 'Name, Age, Relationship, Address' table (E-201) or the
    'Name and Address of Heirs/Devisees' block (E-505). Each line that carries a
    person and a mailing address becomes one heir row; attorney lines are skipped."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        u = line.upper()
        if ("HEIR" in u or "DEVISEE" in u) and ("NAME" in u or "ADDRESS" in u) and ("AGE" in u or "RELATIONSHIP" in u or "ADDRESS" in u):
            start = i + 1
            break
    if start is None:
        return []
    people: List[Dict[str, str]] = []
    rel_words = "|".join(RELATIONSHIP_WORDS)
    # an age is 1–3 digits ONLY when a relationship word follows it or it stands alone;
    # "300 FIXTURE AVE" is a street number, not an age
    prefix = re.compile(rf"^\s*(?:(?P<age>\d{{1,3}})(?=\s*(?:{rel_words})\b|\s*$))?\s*(?:(?P<rel>{rel_words})\b)?\s*(?P<rest>.*)$", re.IGNORECASE)
    for raw in lines[start:start + 40]:
        line = raw.rstrip()
        if not line.strip():
            if people:
                break
            continue
        u = line.upper()
        if "ATTORNEY" in u or "COUNSEL" in u:
            continue  # provenance only; never a person, never a phone
        if any(stop in u for stop in ("SIGNATURE", "SWORN", "NOTARY", "AOC-E-", "PAGE ")):
            break
        line = PHONE_RE.sub(" ", line)  # a number on the line is never carried
        # columns are separated by 2+ spaces in -layout mode; split BEFORE cleaning
        cols = [_clean(c) for c in re.split(r"\s{2,}", line.strip()) if _clean(c)]
        if len(cols) < 2:
            continue
        name = cols[0]
        if not re.search(r"[A-Za-z]{2,}", name) or name.upper().startswith(("NAME", "N/A", "NONE")):
            continue
        rel = ""
        addr_parts: List[str] = []
        for c in cols[1:]:
            m = prefix.match(c)
            if m and m.group("rel") and not rel:
                rel = _relationship_in(m.group("rel"))
            rest = (m.group("rest") if m else c).strip(" ,")
            if rest and not re.fullmatch(r"\d{1,3}", rest):
                addr_parts.append(rest)
        address = _split_address(", ".join(addr_parts))
        people.append({"heir_name": name, "relationship": rel or "HEIR", **address})
    return people


def parse_form(text: str, form: str) -> Dict[str, object]:
    """Fields we can defend from the form's own labels. Missing stays ''."""
    decedent = _after_label(text, ("NAME OF DECEDENT", "IN THE MATTER OF THE ESTATE OF", "ESTATE OF"))
    case_no = ""
    m = CASE_RE.search(text)
    if m:
        case_no = re.sub(r"\s+", "", m.group(1)).upper()
    county = _after_label(text, ("COUNTY",))
    county = county.split()[0].title() if county else ""
    applicant = ""
    pr = ""
    if form == "AOC-E-201":
        applicant = _after_label(text, ("NAME AND ADDRESS OF APPLICANT", "NAME OF APPLICANT"))
        pr = _after_label(text, ("PERSONAL REPRESENTATIVE",))
    else:
        applicant = _after_label(text, ("NAME AND ADDRESS OF AFFIANT", "NAME OF AFFIANT", "AFFIANT"))
    attorney_lines = [l for l in text.splitlines() if "ATTORNEY" in l.upper() or "COUNSEL" in l.upper()]
    phones = PHONE_RE.findall(text)
    heirs = parse_heirs_block(text)
    return {
        "decedent_name": decedent,
        "estate_case_number": case_no,
        "county": county,
        "applicant_name": PHONE_RE.sub("", applicant).strip(" ,") if applicant else "",
        "personal_representative_name": PHONE_RE.sub("", pr).strip(" ,") if pr else "",
        "heirs": heirs,
        "attorney_names_ignored": len(attorney_lines),
        "phones_seen_and_refused": len(phones),
    }


def _person_and_address(raw: str) -> Tuple[str, Dict[str, str]]:
    """'MAPLEWOOD, TESSA J, 100 EXAMPLE LN, CHARLOTTE, NC 28202' -> name + mailing parts.
    The form prints name and address in one cell; the name is 'LAST, FIRST M' (two comma
    parts) when the third part starts with a street number or a PO box."""
    empty = {"mailing_address": "", "mailing_city": "", "mailing_state": "", "mailing_zip": ""}
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return "", empty
    cut = None
    for i, part in enumerate(parts):
        if i >= 1 and re.match(r"^(\d+\s|P\.?O\.?\s*BOX)", part, re.IGNORECASE):
            cut = i
            break
    if cut is None:
        return ", ".join(parts[:2]) if len(parts) >= 2 else parts[0], empty
    return ", ".join(parts[:cut]), _split_address(", ".join(parts[cut:]))


def rows_for(parsed: Dict[str, object], county_slug: str, candidate_id: str, apn: str, form: str, source_date: str) -> List[Dict[str, str]]:
    county = ESTATE_IMPORT_COUNTY_FIPS and next((k for k in ESTATE_IMPORT_COUNTY_FIPS if k.lower() == county_slug.lower()), county_slug.title())
    fips = ESTATE_IMPORT_COUNTY_FIPS.get(county, "")
    base = {c: "" for c in ESTATE_IMPORT_COLUMNS}
    base.update({
        "property_candidate_id": candidate_id,
        "apn": apn,
        "county": county,
        "fips": fips,
        "decedent_name": str(parsed.get("decedent_name") or ""),
        "estate_case_number": str(parsed.get("estate_case_number") or ""),
        "source_document": SOURCE_DOCUMENT_BY_FORM.get(form, form or "NC_AOC_FORM"),
        "source_date": source_date,
    })
    rows: List[Dict[str, str]] = []
    applicant_name, applicant_addr = _person_and_address(str(parsed.get("applicant_name") or ""))
    pr_name, pr_addr = _person_and_address(str(parsed.get("personal_representative_name") or ""))
    if applicant_name:
        rows.append({**base, "applicant_name": applicant_name, "relationship": "APPLICANT", **applicant_addr})
    if pr_name and pr_name.upper() != applicant_name.upper():
        rows.append({**base, "personal_representative_name": pr_name, "relationship": "PERSONAL_REPRESENTATIVE", **pr_addr})
    for heir in parsed.get("heirs") or []:
        rel = str(heir.get("relationship") or "HEIR").upper()
        if rel in FORBIDDEN_RELATIONSHIPS:
            continue
        rows.append({**base, "heir_name": heir["heir_name"], "relationship": rel,
                     "mailing_address": heir.get("mailing_address", ""), "mailing_city": heir.get("mailing_city", ""),
                     "mailing_state": heir.get("mailing_state", ""), "mailing_zip": heir.get("mailing_zip", "")})
    return rows


def _existing_receipt_matches(receipt_path: Path, sha: str) -> bool:
    if not receipt_path.exists():
        return False
    try:
        return json.loads(receipt_path.read_text(encoding="utf-8")).get("sha256") == sha
    except (ValueError, OSError):
        return False


def _append_rows(csv_path: Path, rows: List[Dict[str, str]]) -> None:
    new_file = not csv_path.exists() or csv_path.stat().st_size == 0
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(ESTATE_IMPORT_COLUMNS), lineterminator="\n")
        if new_file:
            w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in ESTATE_IMPORT_COLUMNS})


def intake_estate_pdfs(raw_dir: Path, *, today: Optional[date] = None, candidate_lookup: Optional[Dict[str, Tuple[str, str]]] = None) -> List[PdfIntakeReceipt]:
    """Parse every `<raw_dir>/estate/pdf/<county>/*.pdf` without a matching receipt.

    candidate_lookup maps a normalised decedent name -> (property_candidate_id, apn)
    when the caller can supply it; otherwise the CSV row carries an empty
    candidate id and the operator binds it (the validator reports it).
    """
    pdf_root = Path(raw_dir) / ESTATE_IMPORT_DIRNAME / PDF_DIRNAME
    receipts: List[PdfIntakeReceipt] = []
    if not pdf_root.exists():
        return receipts
    stamp = (today or date.today()).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    for county_dir in sorted(p for p in pdf_root.iterdir() if p.is_dir()):
        county_slug = county_dir.name.lower()
        csv_path = Path(raw_dir) / ESTATE_IMPORT_DIRNAME / f"{county_slug}_estate_{stamp}.csv"
        for pdf in sorted(county_dir.glob("*.pdf")):
            sha = sha256_of(pdf)
            receipt_path = pdf.with_name(pdf.name + RECEIPT_SUFFIX)
            if _existing_receipt_matches(receipt_path, sha):
                continue
            text, parser = pdf_text(pdf)
            receipt = PdfIntakeReceipt(pdf=str(pdf), sha256=sha, form="", parser=parser, state="", intake_at=now)
            if not text.strip():
                receipt.state = "PARSE_TEXT_EMPTY" if parser.startswith("pdftotext") else parser
                receipt.note = "no text layer (scanned?) — transcribe by hand into the CSV" if receipt.state == "PARSE_TEXT_EMPTY" else "install poppler (brew install poppler) and re-run"
                receipt_path.write_text(json.dumps(asdict(receipt), indent=2) + "\n", encoding="utf-8")
                receipts.append(receipt)
                continue
            form = detect_form(text)
            receipt.form = form
            if not form:
                receipt.state = "FORM_NOT_RECOGNISED"
                receipt.note = "expected AOC-E-201 or AOC-E-505 markers in the text"
                receipt_path.write_text(json.dumps(asdict(receipt), indent=2) + "\n", encoding="utf-8")
                receipts.append(receipt)
                continue
            parsed = parse_form(text, form)
            key = re.sub(r"[^A-Z]", "", str(parsed["decedent_name"]).upper())
            cand_id, apn = ("", "")
            if candidate_lookup and key in candidate_lookup:
                cand_id, apn = candidate_lookup[key]
            rows = rows_for(parsed, county_slug, cand_id, apn, form, stamp)
            receipt.case_number = str(parsed["estate_case_number"])
            receipt.decedent_name = str(parsed["decedent_name"])
            receipt.county = str(parsed["county"])
            receipt.attorney_names_ignored = int(parsed["attorney_names_ignored"])
            receipt.phones_seen_and_refused = int(parsed["phones_seen_and_refused"])
            for name in ("decedent_name", "estate_case_number", "applicant_name", "personal_representative_name"):
                (receipt.fields_found if parsed.get(name) else receipt.fields_missing).append(name)
            (receipt.fields_found if parsed.get("heirs") else receipt.fields_missing).append("heirs")
            if rows:
                _append_rows(csv_path, rows)
                receipt.csv_path = str(csv_path)
                receipt.rows = rows
                receipt.people_written = len(rows)
                receipt.state = "ROWS_WRITTEN" if cand_id else "ROWS_WRITTEN_CANDIDATE_UNBOUND"
                if not cand_id:
                    receipt.note = "property_candidate_id is empty: bind the decedent to the estate row (worklist candidate_id) before the runner joins it"
            else:
                receipt.state = "NO_LIVING_PERSON_FOUND"
                receipt.note = "the form text names no applicant, personal representative or heir — transcribe by hand"
            receipt_path.write_text(json.dumps(asdict(receipt), indent=2) + "\n", encoding="utf-8")
            receipts.append(receipt)
    return receipts

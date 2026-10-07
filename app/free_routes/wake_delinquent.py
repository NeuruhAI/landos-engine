"""T8 (Wake): parse the daily delinquent real-estate extract
(services.wake.gov/collection_extracts/real_estate_delq853_MMDDYYYY.zip), a
853-character fixed-width file per Wake's "Real Estate Tax Bill File Layout".

Yields, per account: REID, PIN, owners, C/O, mailing address, situs, deed
book/page/date, values, total due — a tax-delinquency SIGNAL and a mailing-
address refresh with the tape date as source_as_of. Never a phone."""
from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, asdict
from datetime import date
from typing import Dict, Iterable, Iterator, List, Optional

SOURCE = "WAKE_TAX_DELQ853"
PROVENANCE_URL = "https://services.wake.gov/collection_extracts/"
RECORD_SIZE = 853

#: (name, start, end) 1-based inclusive positions from the layout (2020-10 edition)
LAYOUT = [
    ("tape_date", 1, 8), ("account_number", 9, 18), ("tax_year", 19, 22), ("year_for", 23, 26),
    ("billing_type", 27, 32), ("account_type", 33, 35), ("description", 36, 75), ("billing_class", 76, 77),
    ("billing_date", 78, 85), ("city", 86, 98), ("fire_district", 99, 111), ("township", 121, 122),
    ("reid", 123, 129), ("acres", 130, 137), ("map_name", 138, 143), ("pin", 144, 153), ("pin_ext", 154, 156),
    ("primary_owner", 167, 226), ("secondary_owner", 227, 286), ("care_of", 287, 321), ("trade_name", 322, 361),
    ("mailing_1", 362, 396), ("mailing_2", 397, 431), ("mailing_3", 432, 466),
    ("street_prefix", 467, 468), ("street_number", 469, 474), ("street_name", 475, 499), ("street_type", 500, 503),
    ("street_suffix", 504, 505), ("zip", 506, 510), ("deed_book", 519, 524), ("deed_page", 525, 529),
    ("deed_date", 530, 537), ("exempt_status", 558, 560), ("building_value", 599, 609), ("land_value", 610, 620),
    ("total_real_value", 663, 673), ("net_billable_value", 685, 695), ("total_billed", 794, 805),
    ("total_principal_due", 806, 817), ("interest_due", 818, 829), ("cost_due", 830, 841), ("total_due", 842, 853),
]
MONEY_FIELDS = {"total_billed", "total_principal_due", "interest_due", "cost_due", "total_due"}
INT_FIELDS = {"building_value", "land_value", "total_real_value", "net_billable_value"}


def _mmddyyyy(v: str) -> Optional[str]:
    v = (v or "").strip()
    if not re.fullmatch(r"\d{8}", v) or v == "00000000":
        return None
    return f"{v[4:8]}-{v[0:2]}-{v[2:4]}"


def _money(v: str) -> Optional[float]:
    """Money fields print as xxxxxxxxx.xx (decimal point present); a digits-only
    field is read as cents. Blank stays None."""
    v = (v or "").strip()
    if not v:
        return None
    if "." in v:
        try:
            return round(float(v), 2)
        except ValueError:
            return None
    return round(int(v) / 100, 2) if v.isdigit() else None


@dataclass
class DelinquentAccount:
    account_number: str
    tax_year: str
    reid: str
    pin: str
    pin_ext: str
    primary_owner: str
    secondary_owner: str
    care_of: str
    mailing_address: str
    situs_address: str
    zip: str
    deed_book: str
    deed_page: str
    deed_date: Optional[str]
    total_real_value: Optional[int]
    total_due: Optional[float]
    total_principal_due: Optional[float]
    source_as_of: Optional[str]

    def to_dict(self) -> Dict:
        return asdict(self)


def parse_line(line: str) -> Dict[str, object]:
    if len(line) < RECORD_SIZE - 12:   # tolerate trailing whitespace trim on the last money field
        raise ValueError(f"record too short: {len(line)} chars")
    line = line.ljust(RECORD_SIZE)
    row: Dict[str, object] = {}
    for name, a, b in LAYOUT:
        raw = line[a - 1:b]
        if name in MONEY_FIELDS:
            row[name] = _money(raw)
        elif name in INT_FIELDS:
            s = raw.strip()
            row[name] = int(s) if s.isdigit() else None
        elif name in ("tape_date", "billing_date", "deed_date"):
            row[name] = _mmddyyyy(raw)
        else:
            row[name] = " ".join(raw.split())
    return row


def account_from_row(row: Dict[str, object]) -> DelinquentAccount:
    situs = " ".join(str(row.get(k) or "") for k in ("street_prefix", "street_number", "street_name", "street_type", "street_suffix")).strip()
    situs = " ".join(situs.split())
    mailing = ", ".join(str(row.get(k)) for k in ("mailing_1", "mailing_2", "mailing_3") if row.get(k))
    return DelinquentAccount(
        account_number=str(row["account_number"]), tax_year=str(row["tax_year"]), reid=str(row["reid"]),
        pin=str(row["pin"]), pin_ext=str(row["pin_ext"]), primary_owner=str(row["primary_owner"]),
        secondary_owner=str(row["secondary_owner"]), care_of=str(row["care_of"]), mailing_address=mailing,
        situs_address=situs, zip=str(row["zip"]), deed_book=str(row["deed_book"]).lstrip("0"),
        deed_page=str(row["deed_page"]).lstrip("0"), deed_date=row.get("deed_date"),
        total_real_value=row.get("total_real_value"), total_due=row.get("total_due"),
        total_principal_due=row.get("total_principal_due"), source_as_of=row.get("tape_date"),
    )


def iter_accounts(text_lines: Iterable[str]) -> Iterator[DelinquentAccount]:
    for line in text_lines:
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        yield account_from_row(parse_line(line))


def read_extract(path: str) -> List[DelinquentAccount]:
    """Read the .zip (single .txt inside) or the .txt directly."""
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".txt"))
            with z.open(name) as f:
                return list(iter_accounts(io.TextIOWrapper(f, encoding="latin-1")))
    with open(path, encoding="latin-1") as f:
        return list(iter_accounts(f))


def summarize(accounts: List[DelinquentAccount]) -> Dict[str, object]:
    by_year: Dict[str, int] = {}
    for a in accounts:
        by_year[a.tax_year] = by_year.get(a.tax_year, 0) + 1
    pins = {a.pin for a in accounts if a.pin}
    return {"rows": len(accounts), "distinct_pins": len(pins), "distinct_reids": len({a.reid for a in accounts}),
            "tax_years": dict(sorted(by_year.items())), "total_due_sum": round(sum(a.total_due or 0 for a in accounts), 2),
            "source_as_of": next((a.source_as_of for a in accounts if a.source_as_of), None),
            "with_deed_ref": sum(1 for a in accounts if a.deed_book and a.deed_page)}


def index_by_pin(accounts: List[DelinquentAccount]) -> Dict[str, List[DelinquentAccount]]:
    out: Dict[str, List[DelinquentAccount]] = {}
    for a in accounts:
        if a.pin:
            out.setdefault(a.pin, []).append(a)
    return out

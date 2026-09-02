"""The estate rows the adapters run against: the CURRENT export (per-county master
CSVs) with the columns every route needs. Loaded once, filtered per route."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, Iterable, List, Optional

COUNTY_ORDER = ["Mecklenburg", "Wake", "Durham", "Johnston", "Union", "Cabarrus", "Gaston"]
FIPS = {"Cabarrus": "37025", "Durham": "37063", "Gaston": "37071", "Johnston": "37101", "Mecklenburg": "37119", "Union": "37179", "Wake": "37183"}


def load_estate_rows(estate_dir: Path, counties: Optional[Iterable[str]] = None) -> List[dict]:
    rows: List[dict] = []
    for county in (list(counties) if counties else COUNTY_ORDER):
        for path in sorted((estate_dir / county / "master").glob(f"{county.lower()}_master_*.csv")):
            with path.open(newline="", encoding="utf-8") as fh:
                for r in csv.DictReader(fh):
                    r.setdefault("county", county); r.setdefault("fips", FIPS.get(county, ""))
                    rows.append(r)
    return rows


def norm_pin(v: str) -> str:
    """Wake PIN / APN normalisation: digits only, first 10 (the extension is separate)."""
    d = "".join(ch for ch in (v or "") if ch.isdigit())
    return d[:10] if len(d) >= 10 else d


def index_by_pin(rows: Iterable[dict]) -> Dict[str, List[dict]]:
    out: Dict[str, List[dict]] = {}
    for r in rows:
        k = norm_pin(r.get("apn") or "")
        if k:
            out.setdefault(k, []).append(r)
    return out


def is_probate(row: dict) -> bool:
    cond = (row.get("owner_condition") or row.get("qualification_state") or "").upper()
    return any(t in cond for t in ("DECEASED", "HEIR", "ESTATE", "PROBATE"))


def is_entity_owner(row: dict) -> bool:
    name = (row.get("owner_name") or "").upper()
    return any(t in name.split() or t in name for t in ("LLC", "L.L.C.", "INC", "CORP", "TRUST", "TRUSTEE", "COMPANY", "HOLDINGS", "PROPERTIES", "PARTNERSHIP", "ASSOCIATION", "CHURCH", "BANK", " LP", " LLP"))


def is_out_of_state(row: dict) -> bool:
    st = (row.get("mailing_state") or "").strip().upper()
    return bool(st) and st != "NC"

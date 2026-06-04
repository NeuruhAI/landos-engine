"""
tax_sale_info.py — Tier-1 normalizer: Michigan tax-foreclosure auction parcels
from Title Check LLC's PUBLIC catalog at tax-sale.info.

PUBLIC RECORDS. CSV endpoint: https://www.tax-sale.info/catalog/getCsv/id/{id}
(no login; robots.txt has no Disallow). One catalog id = one county/auction list.

CSV columns observed (schema-drift tolerant, matched case-insensitively):
  Lot Number, Minimum Bid, Current Taxes, Parcel Id, County, Address, Local Unit,
  Section, Township, Range, SEV, Legal Description, CIS, Comment 2,
  Latitude, Longitude, Alt Latitude, Alt Longitude
"""
from __future__ import annotations

import csv
import io
import urllib.request
from datetime import date
from typing import Any, List, Optional

from .base import Normalizer, CandidateSignal

CSV_URL = "https://www.tax-sale.info/catalog/getCsv/id/{cid}"


def _num(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _coord(v: Any) -> Optional[float]:
    """Parse a tax-sale.info coordinate. They prefix a hemisphere letter instead
    of a sign, e.g. longitude 'W86.517387' -> -86.517387, 'N42.057' -> 42.057."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    sign = 1.0
    if s[0].upper() in "NSEW":
        if s[0].upper() in "SW":
            sign = -1.0
        s = s[1:].strip()
    try:
        return sign * float(s.replace(",", ""))
    except ValueError:
        return None


def _col(row: dict, *names: str) -> Optional[str]:
    """Case-insensitive column access (tolerates header drift)."""
    lowered = {(k or "").strip().lower(): v for k, v in row.items()}
    for n in names:
        if n.lower() in lowered:
            return lowered[n.lower()]
    return None


class TaxSaleInfoNormalizer(Normalizer):
    source_type = "tax_delinquent"
    fetch_method = "csv_http"
    schedule = "weekly"

    def __init__(self, tenant_id: str, catalog_id: int, label: str, signal_year: int,
                 county: str = "Berrien"):
        super().__init__(tenant_id)
        self.catalog_id = catalog_id
        self.signal_year = signal_year
        self.county = county
        self.source_id = f"taxsale_{county.lower()}_{catalog_id}"
        self.name = f"tax-sale.info {county} catalog {catalog_id} ({label})"
        self.provenance = CSV_URL.format(cid=catalog_id)

    def fetch(self) -> str:
        req = urllib.request.Request(self.provenance, headers={"User-Agent": "landos-engine/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read().decode("utf-8", errors="replace")

    def normalize(self, raw: str) -> List[CandidateSignal]:
        out: List[CandidateSignal] = []
        for row in csv.DictReader(io.StringIO(raw)):
            apn = (_col(row, "Parcel Id", "Parcel ID", "ParcelId") or "").strip()
            if not apn:
                continue  # skip header noise / blank rows; never invent an APN
            addr = (_col(row, "Address") or "").strip()
            local = (_col(row, "Local Unit", "Township") or "").strip()
            full_addr = ", ".join(p for p in [addr, local, "MI"] if p)
            lat = _coord(_col(row, "Latitude")) or _coord(_col(row, "Alt Latitude"))
            lng = _coord(_col(row, "Longitude")) or _coord(_col(row, "Alt Longitude"))
            min_bid = _num(_col(row, "Minimum Bid"))
            sev = _num(_col(row, "SEV"))
            # tax-foreclosure is a strong distress signal (0.85 base); a deep
            # discount (low min bid vs SEV) nudges strength up.
            strength = 0.85
            if sev and min_bid and sev > 0 and (min_bid / sev) < 0.25:
                strength = 0.92
            out.append({
                "apn": apn,
                "address": full_addr or None,
                "lat": lat,
                "lng": lng,
                "signal_type": "tax_delinquent",
                "signal_strength": round(strength, 3),
                "signal_date": date(self.signal_year, 1, 1),
                "raw_meta": {
                    "minimum_bid": min_bid,
                    "current_taxes": _num(_col(row, "Current Taxes")),
                    "sev": sev,
                    "local_unit": local,
                    "county": (_col(row, "County") or self.county).strip(),
                    "lot_number": (_col(row, "Lot Number") or "").strip(),
                    "legal": (_col(row, "Legal Description") or "").strip()[:300],
                    "catalog_id": self.catalog_id,
                },
            })
        return out

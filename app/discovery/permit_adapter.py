"""
permit_adapter.py — DiscoveryAdapter for county permit / building records (emerging pre-market signals).

PURPOSE (per DALE Codex 008 + emerging strategy):
Detect early "pre-market" activity on parcels via permits (new structures, driveways, septic, utilities, clearing).
This is a delta signal: activity where there was none = owner or buyer preparing land for sale/ development.
Especially powerful for boomer-owned rural/vacant land showing first signs of life events (retirement, estate prep, sale).

ACCESS: va_fed by default (Bryan's manual county portal doctrine + ToS for many county sites).
- County clerk / planning / GIS portals often allow date-range or address search for recent permits.
- VA pulls CSV of relevant permits (address/parcel + description + date + type).
- Future: bulk DUA or API where available (many counties expose open data).

CSV expected columns (flexible; adapter normalizes):
  permit_number, issue_date, permit_type, description, address, parcel_apn (county tax 71-form preferred),
  owner_name (if shown), source_url, county_fips, raw_notes

The adapter produces signals with high "emerging" confidence when the parcel previously had low/no recent signals in LandOS (negative space boost in pipeline).

Ties to:
- LandOS discovery base + ingest_discovery pipeline.
- neuruh-geointel / satellite for cross-validation.
- DALE local intel (drone/ground confirm permit activity).
- Heat map population: tag as 'permit_delta' for pre-market boomer layer.

DO NOT scrape aggressively; respect robots/ToS. Use va_fed or sanctioned bulk.
"""

from __future__ import annotations

import csv
import os
from typing import List

from .base import DiscoveryAdapter, DiscoverySignal

DEFAULT_CSV = "data/incoming/permits_<county_fips>.csv"


class PermitCountyAdapter(DiscoveryAdapter):
    source_type = "permit_delta"
    # county_fips set in __init__ or subclass; example for St. Joseph IN 18141
    county_fips = "18141"  # override per instance
    access_mode = "va_fed"
    access_path = "County planning/clerk permit search or CSV export (manual per doctrine)"
    recipe_hint = "R2"  # pre-market activity -> motivated seller outreach variant

    def __init__(self, tenant_id: str, county_fips: str = "18141", csv_path: str = None):
        super().__init__(tenant_id)
        self.county_fips = county_fips
        self.csv_path = csv_path or DEFAULT_CSV.replace("<county_fips>", county_fips)

    def fetch(self) -> List[DiscoverySignal]:
        """Read VA-supplied permit CSV. Same shape as future automated adapter."""
        if not os.path.exists(self.csv_path):
            raise FileNotFoundError(
                f"{self.csv_path} not found — permit_delta adapter is va_fed. "
                "VA pulls recent permits from county portal (date range, relevant types: building, septic, "
                "driveway, utility, land disturbance) and exports CSV with parcel_apn preferred."
            )
        signals: List[DiscoverySignal] = []
        with open(self.csv_path, newline="") as f:
            for row in csv.DictReader(f):
                pnum = (row.get("permit_number") or "").strip()
                if not pnum:
                    continue
                apn = (row.get("parcel_apn") or "").strip() or None
                # Emerging signal: permit activity is strong pre-market indicator
                # Confidence higher if description suggests land improvement (clearing, access, utilities)
                desc = (row.get("description") or "").lower()
                conf = 0.75
                if any(k in desc for k in ["driveway", "septic", "utility", "clear", "access", "structure"]):
                    conf = 0.88
                signals.append({
                    "source_ref": pnum,
                    "source_url": (row.get("source_url") or "").strip() or None,
                    "owner_name": (row.get("owner_name") or "").strip() or None,
                    "signal_date": (row.get("issue_date") or "").strip() or None,
                    "confidence": conf,
                    "parcel_apn": apn,
                    "raw_payload": {
                        **{k: (v or "").strip() for k, v in row.items()},
                        "permit_type": (row.get("permit_type") or "").strip(),
                        "description_lower": desc,
                    },
                })
        return signals


# Example subclass for a specific county if needed
class PermitStJoeINAdapter(PermitCountyAdapter):
    county_fips = "18141"
    access_path = "St. Joseph County IN planning/permits portal (va_fed CSV)"
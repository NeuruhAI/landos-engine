"""
boomer_life_event_adapter.py — DiscoveryAdapter for boomer-specific life event signals (Codex 007).

PURPOSE:
Target the boomer demographic wave explicitly — aging owners (born 1946-1964) with land holdings
where life events (death, health decline, retirement, downsizing, heir disputes, estate planning)
create motivated sellers BEFORE or alongside formal probate.

Signals:
- Obituaries / death notices cross-referenced with ownership (pre or parallel to probate filing).
- Senior / age-related tax exemptions or changes.
- Guardianship / conservatorship filings.
- Divorce or family court involving land (boomer generation divorces).
- Business dissolution or farm/land retirement filings.
- DALE local: "owner moved to assisted living", "kids visiting and clearing", etc.

This feeds the "boomer heat map" directly.

ACCESS: va_fed for obits (newspapers, legacy.com, county notices), tax data (assessor exports),
court dockets (similar to probate). Bulk where possible.

Ties to:
- Existing probate_in (this catches earlier).
- Negative space + deltas for "quiet boomer parcel suddenly active".
- Heat map: strong 'boomer_life_event' layer.
- PMLA: already has probate + divorce weights; add explicit boomer boost.

CSV columns example (obit cross):
  source_ref, signal_date, decedent_name, age_or_birth, address_or_parcel, obituary_url,
  cross_matched_apn, notes, county_fips
"""

from __future__ import annotations

import csv
import os
from typing import List

from .base import DiscoveryAdapter, DiscoverySignal

DEFAULT_CSV = "data/incoming/boomer_life_<county_fips>.csv"


class BoomerLifeEventAdapter(DiscoveryAdapter):
    source_type = "boomer_life_event"
    county_fips = "18141"
    access_mode = "va_fed"
    access_path = "Obituaries, tax assessor senior data, court dockets, DALE local reports (va_fed CSV)"
    recipe_hint = "R4"  # life event -> estate/heir outreach

    def __init__(self, tenant_id: str, county_fips: str = "18141", csv_path: str = None):
        super().__init__(tenant_id)
        self.county_fips = county_fips
        self.csv_path = csv_path or DEFAULT_CSV.replace("<county_fips>", county_fips)

    def fetch(self) -> List[DiscoverySignal]:
        if not os.path.exists(self.csv_path):
            raise FileNotFoundError(
                f"{self.csv_path} not found — boomer_life_event is va_fed. "
                "VA/analyst curates obits + tax senior changes + local DALE reports, cross-matches to parcels."
            )
        signals: List[DiscoverySignal] = []
        with open(self.csv_path, newline="") as f:
            for row in csv.DictReader(f):
                ref = (row.get("source_ref") or "").strip()
                if not ref:
                    continue
                apn = (row.get("cross_matched_apn") or "").strip() or None
                conf = 0.65
                if apn:
                    conf = 0.82  # cross-matched to parcel = stronger
                if "dale" in (row.get("notes") or "").lower():
                    conf = min(conf + 0.1, 0.92)
                signals.append({
                    "source_ref": ref,
                    "source_url": (row.get("obituary_url") or row.get("source_url") or "").strip() or None,
                    "owner_name": (row.get("decedent_name") or row.get("owner_name") or "").strip() or None,
                    "signal_date": (row.get("signal_date") or "").strip() or None,
                    "confidence": conf,
                    "parcel_apn": apn,
                    "raw_payload": {
                        **{k: (v or "").strip() for k, v in row.items()},
                        "boomer_proxy": True,  # explicit flag for heat map / scoring
                    },
                })
        return signals


class BoomerLifeStJoeINAdapter(BoomerLifeEventAdapter):
    county_fips = "18141"
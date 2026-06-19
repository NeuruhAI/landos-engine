"""
negative_space_adapter.py — DiscoveryAdapter for computed "absence as signal" (Codex 013).

PURPOSE:
Surface parcels that exhibit strong "should be motivated" signals (high LandOS absentee + distress + land quality)
but have ABSENCE of recent activity or public listings. This is the "invisible" emerging opportunity layer.
Especially powerful for boomer owners: aging, large holdings, no updates for years = life event brewing
(health, retirement, estate planning, heirs not engaged yet).

This is primarily COMPUTED (DB query against owner_leads + discovery_signals history + overlays),
not external fetch. It can be "va_fed" for human-curated quiet-parcel lists from DALE local knowledge
or VA boots-on-ground.

In pipeline: run after other signals; boost emerging score for high base_score + long silence + DALE flag.

Ties to:
- LandOS owner_leads, discovery_signals (migration 007).
- Heat map: 'negative_space' layer.
- DALE: local "we know this parcel is quiet but high potential" from Node Zero ops.
- PMLA / LandOS scoring: add emerging_negative_space boost.

Output: signals with owner_name from existing lead, high confidence if multiple factors (e.g. boomer proxy + good metrics + silence > 18 months).

For full implementation, this would query the DB directly (using tenant_id, county_fips filters).
Here we support va_fed CSV for curated lists + stub for computed.
"""

from __future__ import annotations

import csv
import os
from typing import List

from .base import DiscoveryAdapter, DiscoverySignal

DEFAULT_CSV = "data/incoming/negative_space_<county_fips>.csv"


class NegativeSpaceAdapter(DiscoveryAdapter):
    source_type = "negative_space"
    county_fips = "18141"
    access_mode = "hybrid"  # computed + va_fed (DALE curated)
    access_path = "Computed from LandOS DB (quiet high-potential parcels) + DALE local knowledge CSV"
    recipe_hint = "R5"  # negative space boomer -> targeted outreach

    def __init__(self, tenant_id: str, county_fips: str = "18141", csv_path: str = None):
        super().__init__(tenant_id)
        self.county_fips = county_fips
        self.csv_path = csv_path or DEFAULT_CSV.replace("<county_fips>", county_fips)

    def fetch(self) -> List[DiscoverySignal]:
        """For va_fed/curated: read CSV of quiet parcels (from DALE intel or analyst).
        For production computed: query DB for parcels where:
          - owner_leads.absentee_score + distress high
          - no recent discovery_signals or updates
          - land metrics good (from overlays)
          - optional boomer proxy (senior exemption, name patterns, age inference)
        Then emit as signals.
        """
        signals: List[DiscoverySignal] = []

        # Va_fed / curated path
        if os.path.exists(self.csv_path):
            with open(self.csv_path, newline="") as f:
                for row in csv.DictReader(f):
                    apn = (row.get("parcel_apn") or "").strip()
                    if not apn:
                        continue
                    conf = float(row.get("confidence") or 0.65)
                    signals.append({
                        "source_ref": f"negspace_{apn}_{row.get('as_of_date', '')}",
                        "source_url": (row.get("source_url") or "").strip() or "computed_dale_local",
                        "owner_name": (row.get("owner_name") or "").strip() or None,
                        "signal_date": (row.get("as_of_date") or "").strip() or None,
                        "confidence": conf,
                        "parcel_apn": apn,
                        "raw_payload": {
                            **{k: (v or "").strip() for k, v in row.items()},
                            "is_computed": False,
                            "dale_note": (row.get("dale_note") or "").strip() or None,
                        },
                    })
            return signals

        # If no CSV, in real impl this would compute from DB.
        # Stub: return empty with note. Production would use psycopg to query.
        # Example logic (commented for reference):
        # SELECT ol.parcel_apn, ol.owner_name, ol.absentee_score, ... FROM landos.owner_leads ol
        # LEFT JOIN landos.discovery_signals ds ON ... WHERE ds.id IS NULL AND ol.absentee_score > 70 ...
        # AND county_fips = %s AND tenant_id = %s AND last_activity < now() - interval '12 months'
        # Then emit with confidence based on score + metrics.

        return signals  # empty in stub; implement DB query for computed mode


class NegativeSpaceStJoeINAdapter(NegativeSpaceAdapter):
    county_fips = "18141"
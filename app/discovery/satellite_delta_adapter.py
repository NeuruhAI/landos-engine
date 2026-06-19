"""
satellite_delta_adapter.py — DiscoveryAdapter for satellite / imagery change detection (pre-market deltas).

PURPOSE (Codex 008 + emerging strategy):
Detect physical changes on parcels via satellite/imagery time-series that indicate emerging motivation
or buyer interest BEFORE public records or listings.
- New clearing, structures, roads, fencing = pre-market prep or investor activity.
- Increased neglect / overgrowth on high-potential absentee land = rising motivation (boomer life events).
- "Poor satellite" flags already boost PMLA; this makes it delta-based (change over time).

ACCESS: Hybrid.
- va_fed / bulk for commercial high-res (Maxar, Planet) or local DALE drone imagery.
- Automated for free public (Sentinel-2, Landsat via AWS Earth Search or Google Earth Engine).
- Integrates with neuruh-geointel for parcel/satellite scoring.
- DALE Node Zero provides proprietary local high-res drone data — the "no one else has" edge.

Output signals include delta details: change_type (clearing_detected, new_structure, neglect_increase),
delta_magnitude, before/after references (image hashes or tile IDs), confidence.

Ties directly to:
- LandOS discovery pipeline (signals resolved to parcels -> emerging boost in owner_leads or heat_map).
- Heat map population: 'satellite_delta' layer for pre-market boomer opportunities.
- DALE physical flywheel: drone data from Node Zero is unique local signal source.
- Negative space: combine with quiet high-score parcels to surface "invisible" emerging ones.

Implementation notes:
- For production, use efficient change detection (NDVI differencing, object detection ML).
- Store raw references for audit/provenance.
- Confidence calibrated higher for DALE drone data (local, recent, validated).

va_fed CSV example columns for drone/commercial:
  parcel_apn, observation_date, change_type, delta_magnitude (0-1), source (drone_maxar_sentinel),
  image_ref_before, image_ref_after, notes, county_fips
"""

from __future__ import annotations

import csv
import os
from typing import List

from .base import DiscoveryAdapter, DiscoverySignal

DEFAULT_CSV = "data/incoming/satellite_deltas_<county_fips>.csv"


class SatelliteDeltaAdapter(DiscoveryAdapter):
    source_type = "satellite_delta"
    county_fips = "18141"  # default; override
    access_mode = "hybrid"  # automated (public) + va_fed (drone/commercial)
    access_path = "Public satellite (Sentinel/Landsat) + DALE Node Zero drone + commercial imagery"
    recipe_hint = "R3"  # pre-market physical change -> motivated seller

    def __init__(self, tenant_id: str, county_fips: str = "18141", csv_path: str = None):
        super().__init__(tenant_id)
        self.county_fips = county_fips
        self.csv_path = csv_path or DEFAULT_CSV.replace("<county_fips>", county_fips)

    def fetch(self) -> List[DiscoverySignal]:
        """Read delta CSV (from geointel pipeline, drone exports, or public analysis).
        Same output whether automated or va_fed."""
        if not os.path.exists(self.csv_path):
            # For automated path, this would call geointel or Earth Engine instead of raising.
            # Here we support va_fed for DALE drone/commercial data.
            raise FileNotFoundError(
                f"{self.csv_path} not found. For DALE/local: export drone change detections. "
                "For public: run geointel/satellite analysis and export CSV with parcel_apn."
            )
        signals: List[DiscoverySignal] = []
        with open(self.csv_path, newline="") as f:
            for row in csv.DictReader(f):
                apn = (row.get("parcel_apn") or "").strip()
                if not apn:
                    continue
                change = (row.get("change_type") or "").strip().lower()
                mag = float(row.get("delta_magnitude") or 0.0)
                # Boost confidence for significant changes or local DALE data
                conf = 0.7 + min(mag * 0.25, 0.2)
                if "dale" in (row.get("source") or "").lower() or "drone" in change:
                    conf = min(conf + 0.15, 0.95)  # local DALE data is higher trust
                signals.append({
                    "source_ref": f"{apn}_{row.get('observation_date', '')}_{change[:20]}",
                    "source_url": (row.get("source_url") or "").strip() or None,
                    "owner_name": None,  # deltas often don't have owner; resolved in pipeline
                    "signal_date": (row.get("observation_date") or "").strip() or None,
                    "confidence": round(conf, 2),
                    "parcel_apn": apn,
                    "raw_payload": {
                        **{k: (v or "").strip() for k, v in row.items()},
                        "change_type": change,
                        "delta_magnitude": mag,
                    },
                })
        return signals


# Convenience for St. Joseph
class SatelliteDeltaStJoeINAdapter(SatelliteDeltaAdapter):
    county_fips = "18141"
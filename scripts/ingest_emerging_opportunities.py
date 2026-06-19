#!/usr/bin/env python3
"""
ingest_emerging_opportunities.py — Pipeline to discover emerging pre-market boomer opportunities
and populate the heat map / enrich owner_leads.

PLAY: 
- Run multiple discovery adapters for deltas, negative space, boomer life events, permits, satellite.
- Resolve signals to parcels (reuse existing logic or simple APN match).
- Compute "emerging" boost: recency + confidence + multi-signal fusion + DALE local flag.
- Tag / create leads in landos.owner_leads with source='emerging' + provenance.
- Populate heat map layer (for now, simple JSON export or insert to a heat_map table; integrate with territory-engine later).
- Output ranked list for APG / CRM import + heat map viz data.

Usage (example for St. Joe):
  python -m scripts.ingest_emerging_opportunities --county 18141 --tenant <tenant_id> --limit 500

Requires:
- .env with DATABASE_URL (LandOS spine)
- CSV inputs in data/incoming/ for va_fed adapters (from VA / DALE ops)
- For computed negative_space: will stub or implement DB query.

Ties to v2 brief Section 22 recommendation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Dict, Any

# Add app to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg
from psycopg.rows import dict_row

from app.discovery.base import DiscoveryAdapter, DiscoverySignal
from app.discovery.permit_adapter import PermitCountyAdapter
from app.discovery.satellite_delta_adapter import SatelliteDeltaAdapter
from app.discovery.negative_space_adapter import NegativeSpaceAdapter
from app.discovery.boomer_life_event_adapter import BoomerLifeEventAdapter

# Simple county config (expand from COUNTY_CONFIG in normalizers)
COUNTY_CONFIG = {
    "18141": {"name": "St. Joseph IN", "fips": "18141"},
    "26021": {"name": "Berrien MI", "fips": "26021"},
}

DEFAULT_TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"  # APG from canon

def get_db():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set (use LandOS spine pooler)")
    return psycopg.connect(url, row_factory=dict_row)

def resolve_to_parcel(signal: DiscoverySignal, conn) -> str | None:
    """Simple resolver: prefer signal parcel_apn, else match by owner_name + county (stub).
    In production: reuse logic from ingest_discovery.py / normalizers.
    """
    if signal.get("parcel_apn"):
        return signal["parcel_apn"]
    # Stub: could query owner_leads by owner_name + county_fips for best match
    return None

def compute_emerging_score(signal: DiscoverySignal, base_land_score: float = 50.0, dale_local: bool = False) -> float:
    """Emerging boost: base LandOS score + signal confidence * recency + multi factors + DALE premium."""
    conf = float(signal.get("confidence", 0.6))
    # Recency (assume signal_date recent = higher; stub)
    score = base_land_score * 0.6 + conf * 40
    if dale_local:
        score += 15  # DALE local ground truth premium
    if "boomer" in str(signal.get("raw_payload", {})).lower():
        score += 10
    return min(max(score, 0), 100)

def upsert_emerging_lead(conn, signal: DiscoverySignal, county_fips: str, tenant_id: str, emerging_score: float):
    """Upsert into owner_leads with emerging provenance. Simple version; production uses full normalizer + score."""
    apn = signal.get("parcel_apn") or "UNKNOWN"
    owner = signal.get("owner_name") or "Unknown Owner"
    prov = f"emerging:{signal['source_ref']}"
    now = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO landos.owner_leads (
                tenant_id, county_fips, parcel_apn, owner_name,
                source, provenance, raw_meta,
                final_lead_score, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, county_fips, parcel_apn) DO UPDATE
            SET final_lead_score = GREATEST(landos.owner_leads.final_lead_score, EXCLUDED.final_lead_score),
                provenance = landos.owner_leads.provenance || '|' || EXCLUDED.provenance,
                updated_at = EXCLUDED.updated_at
            RETURNING lead_id
        """, (
            tenant_id, county_fips, apn, owner,
            "emerging", prov, json.dumps({"emerging_signal": signal}),
            emerging_score, now, now
        ))
        lead_id = cur.fetchone()["lead_id"]
    return lead_id

def populate_heat_map_export(leads: List[Dict], county_fips: str, out_dir: Path = Path("data/heatmaps")):
    """Export simple heat map data (JSON + CSV for viz in territory-engine / CRM map).
    In production: insert to landos.heat_map or PostGIS layer.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    data = {
        "county_fips": county_fips,
        "generated_at": ts,
        "opportunities": leads,
    }
    json_path = out_dir / f"emerging_heat_{county_fips}_{ts}.json"
    json_path.write_text(json.dumps(data, indent=2, default=str))
    print(f"Heat map export: {json_path}")
    # Also CSV for easy import
    csv_path = out_dir / f"emerging_heat_{county_fips}_{ts}.csv"
    # simple write
    import csv as csvmod
    if leads:
        with csv_path.open("w", newline="") as f:
            writer = csvmod.DictWriter(f, fieldnames=leads[0].keys())
            writer.writeheader()
            writer.writerows(leads)
    print(f"CSV: {csv_path}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--county", required=True, help="county_fips e.g. 18141")
    parser.add_argument("--tenant", default=DEFAULT_TENANT)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()

    county = args.county
    tenant = args.tenant
    if county not in COUNTY_CONFIG:
        print(f"Warning: {county} not in COUNTY_CONFIG, proceeding anyway.")

    print(f"Ingesting emerging opportunities for county {county} tenant {tenant}...")

    # Instantiate adapters (va_fed will use CSVs in data/incoming/)
    adapters: List[DiscoveryAdapter] = [
        PermitCountyAdapter(tenant, county_fips=county),
        SatelliteDeltaAdapter(tenant, county_fips=county),
        NegativeSpaceAdapter(tenant, county_fips=county),
        BoomerLifeEventAdapter(tenant, county_fips=county),
    ]

    all_signals: List[DiscoverySignal] = []
    for ad in adapters:
        try:
            sigs = ad.fetch()
            print(f"  {ad.source_type}: {len(sigs)} signals")
            all_signals.extend(sigs)
        except FileNotFoundError as e:
            print(f"  {ad.source_type}: {e} (skipping; place CSV or implement computed)")
        except Exception as e:
            print(f"  {ad.source_type} error: {e}")

    print(f"Total raw signals: {len(all_signals)}")

    leads_for_heat: List[Dict] = []
    with get_db() as conn:
        for sig in all_signals[:args.limit]:
            apn = resolve_to_parcel(sig, conn)
            if not apn:
                continue
            # Stub base LandOS score; in prod join or compute
            base = 60.0
            dale = "dale" in str(sig.get("raw_payload", {})).lower() or "drone" in str(sig).lower()
            emerg = compute_emerging_score(sig, base, dale)
            lead_id = upsert_emerging_lead(conn, sig, county, tenant, emerg)
            leads_for_heat.append({
                "lead_id": str(lead_id),
                "parcel_apn": apn,
                "owner_name": sig.get("owner_name"),
                "emerging_score": round(emerg, 1),
                "source_type": sig.get("source_type", "emerging"),
                "signal_date": sig.get("signal_date"),
                "confidence": sig.get("confidence"),
                "dale_local": dale,
            })

    print(f"Upserted/enriched ~{len(leads_for_heat)} emerging leads.")

    populate_heat_map_export(leads_for_heat, county)

    print("Done. Heat map data in data/heatmaps/. Next: import to CRM or territory-engine viz.")
    print("Recommendation: schedule this + feed DALE local CSVs regularly. Cross with PMLA for final routing.")

if __name__ == "__main__":
    main()
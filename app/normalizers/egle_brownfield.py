"""
egle_brownfield.py — Tier-2 normalizer: EGLE brownfield-redevelopment parcels.

PUBLIC RECORDS. Michigan EGLE RRD Open Data, ArcGIS REST (host gisagoegle.state.mi.us),
Brownfields layer 3. Records carry `ParcelTaxID` in the SAME dashed APN format as the
Berrien tax roll (often a comma-separated list of parcels in one project) — so a
brownfield signal can STACK with a tax_delinquent signal on the same parcel, which is
exactly what the correlation layer rewards.

Why EGLE and not SLBA: the MI State Land Bank ePropertyPlus bulk list is gated behind
the SPA session (its list endpoint 500s without it; only by-parcel works). EGLE's REST
is open and APN-keyed, so it's the fetchable Tier-2 blight/environmental source.

Note: a brownfield is a managed-redevelopment designation (often funded cleanup), NOT
an automatic deal-killer — so this emits a `brownfield` signal and does NOT set the
environmental_flag gate. (On-parcel Part 201 contamination would; EGLE's Part 201
layer 0 lacks ParcelTaxID, so it can't be APN-keyed yet — see TODO.)

Layer 0 (Part 201 contamination) carries SiteID + address but NO ParcelTaxID, so it is
intentionally NOT ingested here (no fabricated APN mapping). TODO: address→APN resolve.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, List

from .base import Normalizer, CandidateSignal

BASE = "https://gisagoegle.state.mi.us/arcgis/rest/services/EGLE/RRDOpenData/MapServer/3/query"


def _is_null(s: str) -> bool:
    return (not s) or s.strip().lower().startswith("<null")


class EgleBrownfieldNormalizer(Normalizer):
    source_type = "brownfield"
    fetch_method = "arcgis_rest"
    schedule = "monthly"

    def __init__(self, tenant_id: str, county: str = "Berrien", city: str = "BENTON HARBOR",
                 signal_year: int = 2026):
        super().__init__(tenant_id)
        self.county = county
        self.city = city
        self.signal_year = signal_year
        self.source_id = f"egle_brownfield_{city.lower().replace(' ', '_')}"
        self.name = f"EGLE RRD brownfields — {city}, {county}"
        self.provenance = f"EGLE RRDOpenData L3 (gisagoegle.state.mi.us); where City LIKE '%{city}%'"

    def fetch(self) -> dict:
        u = (BASE + "?where=" + urllib.parse.quote(f"City LIKE '%{self.city}%'")
             + "&outFields=ProjectName,SiteAddress,ParcelTaxID,City"
             + "&returnGeometry=true&outSR=4326&f=geojson")
        req = urllib.request.Request(u, headers={"User-Agent": "landos-engine/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def normalize(self, fc: dict) -> List[CandidateSignal]:
        out: List[CandidateSignal] = []
        for f in fc.get("features", []):
            p = f.get("properties", {}) or {}
            g = f.get("geometry") or {}
            ptid = (p.get("ParcelTaxID") or "").strip()
            if _is_null(ptid):
                continue  # project-level row with no parcel ids — never invent one
            lat = lng = None
            if g.get("type") == "Point":
                lng, lat = g["coordinates"][0], g["coordinates"][1]
            project = (p.get("ProjectName") or "").strip()
            site_addr = (p.get("SiteAddress") or "").strip()[:200]
            for apn in (a.strip() for a in ptid.split(",")):
                if _is_null(apn):
                    continue
                out.append({
                    "apn": apn,
                    "address": site_addr or None,
                    "lat": lat,  # project point — approximate for multi-parcel projects
                    "lng": lng,
                    "signal_type": "brownfield",
                    "signal_strength": 0.55,
                    "signal_date": date(self.signal_year, 1, 1),
                    "raw_meta": {
                        "project": project,
                        "site_address": site_addr,
                        "source": "EGLE RRDOpenData brownfields L3",
                        "city": self.city,
                        "geometry_note": "project point (approximate for multi-parcel projects)",
                    },
                })
        return out

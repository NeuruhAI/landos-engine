"""
egle_part201.py — Tier-2 normalizer: EGLE Part 201 environmental-contamination
sites, turned into APN-keyed signals.

PUBLIC RECORDS. EGLE RRD Open Data, ArcGIS REST layer 0 (Part 201 sites). These
records carry SiteID + address + lat/lng but NO ParcelTaxID, so each site's point
is resolved to a Berrien APN via app.resolvers.address_to_apn (spatial intersect).
Sites that don't resolve to a confident APN are LOGGED unresolved and NOT emitted —
never a fabricated APN.

A Part 201 signal sets raw_meta.environmental_flag = True, which the deep report /
lead queue read as a REAL (but curable — Phase II) environmental flag, and which can
STACK with a tax_delinquent signal on the same parcel (the correlation layer).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, List, Optional

from .base import Normalizer, CandidateSignal
from app.resolvers.address_to_apn import AddressApnResolver

BASE0 = "https://gisagoegle.state.mi.us/arcgis/rest/services/EGLE/RRDOpenData/MapServer/0/query"


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class EglePart201Normalizer(Normalizer):
    source_type = "environmental_part201"
    fetch_method = "arcgis_rest"
    schedule = "monthly"

    def __init__(self, tenant_id: str, county: str = "Berrien", city: str = "BENTON HARBOR",
                 signal_year: int = 2026):
        super().__init__(tenant_id)
        self.county = county
        self.city = city
        self.signal_year = signal_year
        self.source_id = f"egle_part201_{city.lower().replace(' ', '_')}"
        self.name = f"EGLE Part 201 contamination — {city}, {county}"
        self.provenance = f"EGLE RRDOpenData L0 (gisagoegle.state.mi.us); where City LIKE '%{city}%'"

    def fetch(self) -> dict:
        u = (BASE0 + "?where=" + urllib.parse.quote(f"City LIKE '%{self.city}%'")
             + "&outFields=SiteName,SiteID,Address,City,Latitude,Longitude,Contaminants,RiskCondition"
             + "&returnGeometry=false&f=json")
        req = urllib.request.Request(u, headers={"User-Agent": "landos-engine/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)

    def normalize(self, raw: dict) -> List[CandidateSignal]:  # unused; resolution needs the cursor (see run)
        return []

    @staticmethod
    def build_signal(site: dict, apn: Optional[str], confidence: Optional[str],
                     signal_year: int) -> Optional[CandidateSignal]:
        """Pure: a resolved site -> signal; an unresolved site (apn None) -> None."""
        if not apn:
            return None
        return {
            "apn": apn,
            "address": (site.get("Address") or "").strip() or None,
            "lat": _f(site.get("Latitude")),
            "lng": _f(site.get("Longitude")),
            "signal_type": "environmental_part201",
            "signal_strength": 0.6,
            "signal_date": date(signal_year, 1, 1),
            "raw_meta": {
                "site_name": site.get("SiteName"),
                "site_id": site.get("SiteID"),
                "contaminants": site.get("Contaminants"),
                "risk_condition": site.get("RiskCondition"),
                "environmental_flag": True,        # read as a curable flag downstream
                "apn_confidence": confidence,
                "source": "EGLE RRDOpenData Part201 L0 + Berrien GIS spatial APN",
            },
        }

    def run(self, conn) -> int:
        """fetch sites -> resolve each to an APN -> upsert resolved, log resolved+unresolved."""
        with conn.cursor() as cur:
            self.ensure_source(cur)
            try:
                raw = self.fetch()
            except Exception as e:  # noqa: BLE001
                self.log_ingest(cur, 0, "error", f"{e.__class__.__name__}: {e}")
                conn.commit()
                raise
            sites = [f.get("attributes", {}) for f in raw.get("features", [])]
            resolver = AddressApnResolver(cur, self.tenant_id)
            candidates: List[CandidateSignal] = []
            unresolved = 0
            for s in sites:
                r = resolver.resolve(address=(s.get("Address") or None),
                                     lat=_f(s.get("Latitude")), lng=_f(s.get("Longitude")))
                cand = self.build_signal(s, r.get("apn"), r.get("confidence"), self.signal_year)
                if cand:
                    candidates.append(cand)
                else:
                    unresolved += 1
            n = self.upsert_signals(cur, candidates)
            self.log_ingest(cur, n, "ok" if n else "empty",
                            f"part201: {len(sites)} sites, {n} resolved->APN, "
                            f"{unresolved} unresolved (no confident APN, NOT emitted)")
        conn.commit()
        return n

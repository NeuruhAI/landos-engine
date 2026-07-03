"""
county_rest.py — tier 1: free per-county GIS/ArcGIS parcel endpoints.

Interface + ONE worked stub (Berrien 26021, documented in config.py) — NOT a
50-county build. A county serves from here only when its config row exists
AND an operator has flipped verified=True after confirming the endpoint +
field mapping against a known parcel. Unverified or unconfigured counties
raise ProviderUnavailable and the chain degrades — the provider never serves
matches off a guessed field mapping (no-fabrication discipline).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import List

from .base import ParcelProvider, ParcelDict, ProviderUnavailable, name_tokens
from .config import COUNTY_REST_ENDPOINTS


class CountyRESTProvider(ParcelProvider):
    name = "county_rest"
    cost_note = "free — county-published GIS REST"

    def _cfg(self, county_fips: str) -> dict:
        cfg = COUNTY_REST_ENDPOINTS.get(county_fips)
        if not cfg:
            raise ProviderUnavailable(
                f"no county REST endpoint configured for {county_fips}")
        if not cfg.get("verified"):
            raise ProviderUnavailable(
                f"county REST endpoint for {county_fips} is UNVERIFIED "
                f"({cfg.get('note', 'confirm endpoint + fields, then set verified=True')})")
        return cfg

    def _http(self, cfg: dict, params: dict) -> dict:
        req = urllib.request.Request(f"{cfg['url']}?{urllib.parse.urlencode(params)}",
                                     headers={"User-Agent": "landos-engine/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception as e:
            raise ProviderUnavailable(f"county REST error: {e}") from e

    def _query(self, cfg: dict, where: str) -> List[dict]:
        # geometry requested in WGS84 so parcels carry their own centroid —
        # enriched-without-coords is a half-harvest we would re-fetch later
        params = {"where": where, "outFields": "*", "f": "json", "resultRecordCount": 25,
                  "returnGeometry": "true", "outSR": 4326}
        data = self._http(cfg, params)
        if isinstance(data.get("error"), dict) and "agination" in str(data["error"].get("message", "")):
            # classic MapServer layers (e.g. Allegan) reject resultRecordCount
            params.pop("resultRecordCount")
            data = self._http(cfg, params)
        if "error" in data:
            raise ProviderUnavailable(f"county REST API error: {data['error']}")
        return data.get("features", [])

    @staticmethod
    def _centroid(geom):
        """Mean of the outer-ring vertices (WGS84) — parcel-pin precision, the
        same grade the 4b parcel-centroid geocode used. None when no geometry."""
        try:
            ring = (geom or {}).get("rings", [[]])[0]
            pts = ring[:-1] if len(ring) > 1 and ring[0] == ring[-1] else ring
            if not pts:
                return (None, None)
            lng = sum(p[0] for p in pts) / len(pts)
            lat = sum(p[1] for p in pts) / len(pts)
            if -90 <= lat <= 90 and -180 <= lng <= 180:
                return (round(lat, 7), round(lng, 7))
        except (TypeError, IndexError, ZeroDivisionError):
            pass
        return (None, None)

    @staticmethod
    def _attr(attrs: dict, cfg: dict, field_key: str):
        """Clean string for the attribute cfg[field_key] names, else None.
        Unmapped keys stay honest-NULL — never composed from guessed fields."""
        f = cfg.get(field_key)
        if not f or attrs.get(f) is None:
            return None
        return str(attrs[f]).strip() or None

    def _to_parcel(self, county_fips: str, cfg: dict, feature: dict) -> ParcelDict:
        attrs = feature.get("attributes", {})
        lat, lng = self._centroid(feature.get("geometry"))
        raw_apn = (str(attrs.get(cfg["apn_field"]) or "").strip() or None)
        prefix = cfg.get("apn_prefix") or ""
        # return the CANONICAL county APN (strip the layer-only prefix) so
        # matches line up with landos.owner_leads.parcel_apn
        apn = raw_apn[len(prefix):] if (prefix and raw_apn and raw_apn.startswith(prefix)) else raw_apn
        return {
            "county_fips": county_fips,
            "parcel_apn": apn,
            "owner_name": self._attr(attrs, cfg, "owner_field"),
            "owner_name2": self._attr(attrs, cfg, "owner2_field"),
            "owner_mailing_address": self._attr(attrs, cfg, "mail_street_field"),
            "owner_mailing_city": self._attr(attrs, cfg, "mail_city_field"),
            "owner_mailing_state": self._attr(attrs, cfg, "mail_state_field"),
            "owner_mailing_zip": self._attr(attrs, cfg, "mail_zip_field"),
            "situs_address": self._attr(attrs, cfg, "situs_street_field"),
            "situs_city": self._attr(attrs, cfg, "situs_city_field"),
            "situs_state": self._attr(attrs, cfg, "situs_state_field"),
            "situs_zip": self._attr(attrs, cfg, "situs_zip_field"),
            "situs_lat": lat,
            "situs_lng": lng,
            "provenance": f"county_rest:{cfg['url']}",
            "raw_meta": {"county_rest_attributes": attrs},
        }

    def lookup_by_apn(self, county_fips: str, apn: str) -> List[ParcelDict]:
        cfg = self._cfg(county_fips)
        q_apn = (cfg.get("apn_prefix") or "") + apn
        rows = self._query(cfg, f"{cfg['apn_field']} = '{q_apn.replace(chr(39), chr(39) * 2)}'")
        return [p for p in (self._to_parcel(county_fips, cfg, a) for a in rows)
                if p["parcel_apn"]]

    def search_by_owner(self, county_fips: str, owner_name: str) -> List[ParcelDict]:
        cfg = self._cfg(county_fips)
        q = name_tokens(owner_name) or (owner_name or "").strip().upper()
        if not q:
            return []
        rows = self._query(cfg, f"UPPER({cfg['owner_field']}) LIKE '%{q}%'")
        return [p for p in (self._to_parcel(county_fips, cfg, a) for a in rows)
                if p["parcel_apn"]]

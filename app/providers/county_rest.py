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

    def _query(self, cfg: dict, where: str) -> List[dict]:
        params = urllib.parse.urlencode({
            "where": where, "outFields": "*", "f": "json", "resultRecordCount": 25,
        })
        req = urllib.request.Request(f"{cfg['url']}?{params}",
                                     headers={"User-Agent": "landos-engine/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception as e:
            raise ProviderUnavailable(f"county REST error: {e}") from e
        if "error" in data:
            raise ProviderUnavailable(f"county REST API error: {data['error']}")
        return [f.get("attributes", {}) for f in data.get("features", [])]

    def _to_parcel(self, county_fips: str, cfg: dict, attrs: dict) -> ParcelDict:
        return {
            "county_fips": county_fips,
            "parcel_apn": (str(attrs.get(cfg["apn_field"]) or "").strip() or None),
            "owner_name": (str(attrs.get(cfg["owner_field"]) or "").strip() or None),
            "situs_address": None,
            "provenance": f"county_rest:{cfg['url']}",
            "raw_meta": {"county_rest_attributes": attrs},
        }

    def lookup_by_apn(self, county_fips: str, apn: str) -> List[ParcelDict]:
        cfg = self._cfg(county_fips)
        rows = self._query(cfg, f"{cfg['apn_field']} = '{apn}'")
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

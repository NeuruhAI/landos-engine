"""
fetchgis.py — tier 1b: the FetchGIS land-records search endpoint
(app.fetchgis.com/ws/PSsearch2.php), the county-official parcel search behind
~20 rural-MI county GIS portals (ENRICH-1 census 2026-07-05).

NOT ArcGIS: a plain GET returning XML <searchdata><record> rows with
ParcelNumber / OwnerName / PropertyAddress (combined 'STREET, CITY') /
PropertyClass. No owner mailing, no geometry — those stay honest-NULL.

Per-county config (FETCHGIS_ENDPOINTS in config.py): map (the FetchGIS map
key, e.g. 'stjo' for St. Joseph MI), dash ('true' for dash-separated PINs,
'false' for space-separated), optional apn_strip_prefix (a county code our
canonical APN carries but the layer omits — stripped to query, restored on
return so matches line up with owner_leads.parcel_apn), verified (the same
refuse-unverified discipline as county_rest).

Query form: primary = the APN split into Pin1..PinN on its separator (the
form every census agent verified); fallback = Pin1=<whole APN> (also
confirmed live). Matching upstream stays canonical-equality — a miss is a
miss, never a guess. HTTP/1.1 via urllib; the endpoint accepts the honest
Mozilla-compatible engine UA (verified 2026-07-05; bare python UAs get WAF'd).
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import List

from .base import ParcelProvider, ParcelDict, ProviderUnavailable
from .config import FETCHGIS_ENDPOINTS

BASE = "https://app.fetchgis.com/ws/PSsearch2.php"
UA = "Mozilla/5.0 (compatible; landos-engine/1.0; contact jeramieshicks@gmail.com)"


class FetchGISProvider(ParcelProvider):
    name = "fetchgis"
    cost_note = "free — county-official FetchGIS land-records search"

    def _cfg(self, county_fips: str) -> dict:
        cfg = FETCHGIS_ENDPOINTS.get(county_fips)
        if not cfg:
            raise ProviderUnavailable(f"no FetchGIS map configured for {county_fips}")
        if not cfg.get("verified"):
            raise ProviderUnavailable(f"FetchGIS map for {county_fips} is UNVERIFIED")
        return cfg

    def _http(self, params: dict) -> str:
        url = f"{BASE}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            raise ProviderUnavailable(f"FetchGIS error: {e}") from e

    @staticmethod
    def _records(body: str) -> List[dict]:
        """<searchdata><record><ParcelNumber>… -> [{tag: text}]. An empty body
        (the endpoint's silent no-such-map answer) is just zero records."""
        body = (body or "").strip()
        if not body:
            return []
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise ProviderUnavailable(f"FetchGIS unparseable response: {e}") from e
        out = []
        for rec in root.iter("record"):
            out.append({c.tag: (c.text or "").strip() for c in rec})
        return out

    def _to_parcel(self, county_fips: str, cfg: dict, rec: dict) -> ParcelDict:
        strip = cfg.get("apn_strip_prefix") or ""
        layer_apn = rec.get("ParcelNumber") or None
        # restore the county-code prefix the layer omits -> our canonical form
        apn = (strip + layer_apn) if (strip and layer_apn) else layer_apn
        situs = (rec.get("PropertyAddress") or "").strip().strip(",").strip() or None
        return {
            "county_fips": county_fips,
            "parcel_apn": apn,
            "owner_name": rec.get("OwnerName") or None,
            "owner_name2": None,
            "owner_mailing_address": None,     # not exposed by this endpoint
            "owner_mailing_city": None,
            "owner_mailing_state": None,
            "owner_mailing_zip": None,
            "situs_address": situs,            # combined 'STREET, CITY' as served
            "situs_city": None, "situs_state": None, "situs_zip": None,
            "situs_lat": None, "situs_lng": None,   # no geometry — honest NULL
            "provenance": f"fetchgis:{cfg['map']}",
            "raw_meta": {"fetchgis_record": rec},
        }

    def lookup_by_apn(self, county_fips: str, apn: str) -> List[ParcelDict]:
        cfg = self._cfg(county_fips)
        strip = cfg.get("apn_strip_prefix") or ""
        q = apn[len(strip):] if (strip and apn.startswith(strip)) else apn
        sep = "-" if cfg.get("dash", "true") == "true" else " "
        base = {"Map": cfg["map"], "dash": cfg.get("dash", "true"), "recLimit": 5}
        parts = [p for p in q.split(sep) if p]
        tries = []
        if len(parts) > 1:
            tries.append({**base, **{f"Pin{i+1}": p for i, p in enumerate(parts)}})
        tries.append({**base, "Pin1": q})            # whole-PIN fallback (also verified)
        for params in tries:
            recs = self._records(self._http(params))
            if recs:
                return [p for p in (self._to_parcel(county_fips, cfg, r) for r in recs)
                        if p["parcel_apn"]]
        return []

    def search_by_owner(self, county_fips: str, owner_name: str) -> List[ParcelDict]:
        # PSsearch2 is PIN-keyed; owner search is a different (unverified) surface.
        raise ProviderUnavailable("FetchGIS owner search not configured")


def canon(s):
    return re.sub(r"[^A-Za-z0-9]", "", s or "").upper()

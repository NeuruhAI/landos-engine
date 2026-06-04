"""
address_to_apn.py — reusable address/point -> Berrien County APN resolver.

Method: geocode an address to a point if needed (Census geocoder), then SPATIAL-
INTERSECT that point against the Berrien County Parcels_Future ArcGIS layer to get
the parcel's PIN. A confident match = EXACTLY ONE parcel intersects the point. If
zero or multiple intersect, return apn=None + a reason — NEVER guess.

Results are cached in landos.address_apn_cache (keyed by address or "lat,lng") to
avoid re-querying. The resolver takes a DB cursor (standard tuple cursor) so cache
reads/writes ride the caller's transaction.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

PARCELS_QUERY = ("https://gis.berriencountymi.gov/server/rest/services/"
                 "Parcels_Future/FeatureServer/1/query")
CENSUS_GEOCODE = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
PIN_FIELD = "PIN"


def classify_pin(features: List[dict], pin_field: str = PIN_FIELD) -> Tuple[Optional[str], Optional[str], str]:
    """Pure: turn ArcGIS intersect features into (apn, confidence, reason). One
    feature => confident; zero/many => no guess."""
    if not features:
        return (None, None, "no parcel intersects point")
    if len(features) > 1:
        return (None, None, f"ambiguous: {len(features)} parcels intersect")
    attrs = features[0].get("attributes") or {}
    pin = attrs.get(pin_field)
    if not pin:
        return (None, None, "intersecting parcel has no PIN")
    return (str(pin), "high", "1 parcel intersects (spatial)")


def _geocode(address: str) -> Tuple[Optional[float], Optional[float]]:
    url = CENSUS_GEOCODE + "?" + urllib.parse.urlencode(
        {"address": address, "benchmark": "Public_AR_Current", "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": "landos-engine/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    m = d.get("result", {}).get("addressMatches", [])
    if not m:
        return (None, None)
    c = m[0]["coordinates"]
    return (c["y"], c["x"])  # lat, lng


class AddressApnResolver:
    def __init__(self, cur, tenant_id: str):
        self.cur = cur
        self.tenant_id = tenant_id

    # ---- cache (standard tuple cursor) ----
    def _cache_get(self, key: str):
        self.cur.execute(
            "select apn, confidence, reason, lat, lng from landos.address_apn_cache "
            "where tenant_id=%s and cache_key=%s",
            (self.tenant_id, key),
        )
        return self.cur.fetchone()

    def _cache_put(self, key, address, lat, lng, apn, conf, reason):
        self.cur.execute(
            """
            insert into landos.address_apn_cache
              (tenant_id, cache_key, address, lat, lng, apn, confidence, reason)
            values (%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (tenant_id, cache_key) do update set
              address=excluded.address, lat=excluded.lat, lng=excluded.lng, apn=excluded.apn,
              confidence=excluded.confidence, reason=excluded.reason, resolved_at=now()
            """,
            (self.tenant_id, key, address, lat, lng, apn, conf, reason),
        )

    def _spatial_pin(self, lat: float, lng: float):
        geom = json.dumps({"x": lng, "y": lat, "spatialReference": {"wkid": 4326}})
        params = {
            "geometry": geom, "geometryType": "esriGeometryPoint", "inSR": "4326",
            "spatialRel": "esriSpatialRelIntersects", "outFields": PIN_FIELD,
            "returnGeometry": "false", "f": "json",
        }
        url = PARCELS_QUERY + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "landos-engine/1.0"})
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.load(r)
        return classify_pin(d.get("features", []))

    def resolve(self, address: Optional[str] = None, lat: Optional[float] = None,
                lng: Optional[float] = None) -> Dict[str, Any]:
        if lat is not None and lng is not None:
            key = f"{float(lat):.6f},{float(lng):.6f}"
        elif address:
            key = address.strip().lower()
        else:
            return {"apn": None, "confidence": None, "reason": "no address or coords", "lat": None, "lng": None}

        hit = self._cache_get(key)
        if hit is not None:
            apn, conf, reason, clat, clng = hit
            return {"apn": apn, "confidence": conf, "reason": reason or "cache", "lat": clat, "lng": clng}

        rlat, rlng = lat, lng
        if rlat is None or rlng is None:
            try:
                rlat, rlng = _geocode(address)
            except Exception as e:  # noqa: BLE001
                self._cache_put(key, address, None, None, None, None, f"geocode error: {e.__class__.__name__}")
                return {"apn": None, "confidence": None, "reason": "geocode error", "lat": None, "lng": None}
            if rlat is None:
                self._cache_put(key, address, None, None, None, None, "no geocode match")
                return {"apn": None, "confidence": None, "reason": "no geocode match", "lat": None, "lng": None}

        try:
            apn, conf, reason = self._spatial_pin(rlat, rlng)
        except Exception as e:  # noqa: BLE001
            self._cache_put(key, address, rlat, rlng, None, None, f"spatial error: {e.__class__.__name__}")
            return {"apn": None, "confidence": None, "reason": "spatial error", "lat": rlat, "lng": rlng}

        self._cache_put(key, address, rlat, rlng, apn, conf, reason)
        return {"apn": apn, "confidence": conf, "reason": reason, "lat": rlat, "lng": rlng}

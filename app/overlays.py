"""
overlays.py — pure geometry helpers for the policy-overlay layer.

Point-in-polygon by ray casting on GeoJSON rings (Polygon / MultiPolygon), plus
a distance test for Point-geometry overlays (e.g. EGLE brownfield site points
carrying a buffer radius). No dependencies, no DB, no network — overlay rows are
passed in by the caller. Coordinates are GeoJSON order: [lng, lat], WGS84.
"""
from math import radians, sin, cos, asin, sqrt
from typing import Any, Dict, List, Optional

_EARTH_M = 6371000.0  # mean Earth radius, metres


def _point_in_ring(lng: float, lat: float, ring: List[List[float]]) -> bool:
    """Ray-casting test: is (lng,lat) inside this single ring of [lng,lat] pairs?"""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (
            lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi
        ):
            inside = not inside
        j = i
    return inside


def _point_in_polygon(lng: float, lat: float, rings: List[List[List[float]]]) -> bool:
    """First ring = outer boundary; any remaining rings are holes."""
    if not rings:
        return False
    if not _point_in_ring(lng, lat, rings[0]):
        return False
    for hole in rings[1:]:
        if _point_in_ring(lng, lat, hole):
            return False
    return True


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return 2 * _EARTH_M * asin(sqrt(a))


def point_in_geometry(
    lat: float, lng: float, geom: Optional[Dict[str, Any]], buffer_m: Optional[float] = None
) -> bool:
    """True if (lat,lng) falls inside a GeoJSON Polygon/MultiPolygon, or within
    buffer_m of a Point. Unknown / empty geometry -> False."""
    if not geom:
        return False
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if not coords:
        return False
    if gtype == "Polygon":
        return _point_in_polygon(lng, lat, coords)
    if gtype == "MultiPolygon":
        return any(_point_in_polygon(lng, lat, poly) for poly in coords)
    if gtype == "Point" and buffer_m:
        plng, plat = coords[0], coords[1]
        return _haversine_m(lat, lng, plat, plng) <= float(buffer_m)
    return False


def _bbox_miss(lat: float, lng: float, ov: Dict[str, Any]) -> bool:
    """Fast reject using a stored bbox (padded for Point buffers)."""
    minx, miny, maxx, maxy = ov.get("minx"), ov.get("miny"), ov.get("maxx"), ov.get("maxy")
    if None in (minx, miny, maxx, maxy):
        return False  # no bbox -> cannot reject, fall through to full test
    # bbox may arrive as Decimal (Postgres NUMERIC); coerce for mixed arithmetic.
    minx, miny, maxx, maxy = float(minx), float(miny), float(maxx), float(maxy)
    pad = (float(ov["buffer_m"]) / 111000.0) if ov.get("buffer_m") else 0.0
    return not (minx - pad <= lng <= maxx + pad and miny - pad <= lat <= maxy + pad)


def overlay_hits(lat: float, lng: float, overlays: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return the overlay rows whose geometry contains (lat,lng). Each hit echoes
    the fields the multiplier engine needs (type, provenance, weight, eff date)."""
    hits: List[Dict[str, Any]] = []
    for ov in overlays:
        if _bbox_miss(lat, lng, ov):
            continue
        if point_in_geometry(lat, lng, ov.get("geometry"), ov.get("buffer_m")):
            hits.append(
                {
                    "overlay_type": ov.get("overlay_type"),
                    "name": ov.get("name"),
                    "provenance_status": ov.get("provenance_status"),
                    "multiplier_weight": float(ov.get("multiplier_weight") or 0.0),
                    "effective_date": ov.get("effective_date"),
                    "source": ov.get("source"),
                }
            )
    return hits

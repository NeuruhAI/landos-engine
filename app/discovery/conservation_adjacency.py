"""
conservation_adjacency.py — DiscoveryAdapter: parcels bordering protected ground.

THE THESIS. A parcel that borders permanently protected ground — a land trust, a
nature preserve, a PAD-US conservation area — carries a durable dispo premium: the
view/adjacency can never be built out. It is a value MODIFIER on disposition, and a
buy signal for the land/estate candidates next to it. We already run a spatial
overlay engine (landos.overlay_polygons) with a geometry, an adjacency buffer, and a
multiplier weight. This adapter reads it. $0.

    signal: {type: conservation_adjacency, parcel_apn, overlay_name, overlay_type,
             proximity: 'within'|'adjacent', dispo_multiplier, confidence}

CENSUS FIRST. This is a spatial join between parcel points and PROTECTED-class
overlays. It NEVER fabricates adjacency: it censuses which overlay types are present
and DISARMS where no protected layer exists.

    LIVE STATE 2026-07-08: DISARMED. The overlay engine is live and populated (19
    rows) — but with brownfield / opportunity_zone / bh_housing_tif / ox_creek_corridor,
    NONE of which is a conservation / land-trust / protected layer. Parcel POINT
    geometry is well captured (owner_leads.situs_lat/lng ~1135). So the machinery and
    the geometry are ready; the protected LAYER is the only missing piece.

    REAL FIRST TASK (ingest-side): stage a protected-ground layer into
    landos.overlay_polygons with overlay_type in the protected set (PAD-US protected
    areas, a local/regional land-trust parcel layer, or preserve/greenway boundaries),
    with a buffer_m and multiplier_weight. THEN this adapter arms with no code change
    (adjacency logic proven vs synthetic overlays). Exact-polygon distance is a
    PostGIS/shapely refinement; today's bbox+buffer proximity is the $0 stdlib proxy.

MATCHING & LEAD CREATION belong to the pipeline — this adapter's only job is the signal.
"""
from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .base import DiscoveryAdapter, DiscoverySignal

# Overlay types that count as "protected ground" (configurable). The live overlays
# (brownfield/opportunity_zone/tif/creek_corridor) are deliberately NOT here — a
# brownfield or a tax-incentive zone is not protected conservation land.
DEFAULT_PROTECTED_TYPES = {
    "conservation", "protected", "land_trust", "preserve", "nature_preserve",
    "park", "greenway", "wildlife", "open_space", "wetland_protected",
}
# provenance we trust enough to emit on (a 'stub' overlay is scaffolding, not data).
_EMITTABLE_PROVENANCE = {"authoritative", "sourced_confirmed", "sourced_unconfirmed"}
_M_PER_DEG_LAT = 111_320.0


def _buffer_degrees(buffer_m: float, lat: float) -> tuple:
    """Convert a metres buffer to (dlat, dlng) degrees at a given latitude. Honest
    small-angle approximation — adequate for a bbox proximity gate, not survey math."""
    dlat = buffer_m / _M_PER_DEG_LAT
    dlng = buffer_m / (_M_PER_DEG_LAT * max(math.cos(math.radians(lat)), 0.01))
    return dlat, dlng


def proximity(parcel: Dict[str, Any], overlay: Dict[str, Any]) -> Optional[str]:
    """PURE. Return 'within' if the parcel point falls inside the overlay bbox,
    'adjacent' if within buffer_m of it, else None. Returns None when geometry or
    bbox is missing (silence, never a fabricated adjacency)."""
    lat, lng = parcel.get("lat"), parcel.get("lng")
    try:
        lat = float(lat); lng = float(lng)
    except (TypeError, ValueError):
        return None
    box = [overlay.get(k) for k in ("minx", "miny", "maxx", "maxy")]
    if any(v is None for v in box):
        return None
    minx, miny, maxx, maxy = (float(v) for v in box)      # x=lng, y=lat
    if minx <= lng <= maxx and miny <= lat <= maxy:
        return "within"
    dlat, dlng = _buffer_degrees(float(overlay.get("buffer_m") or 0.0), lat)
    if (minx - dlng) <= lng <= (maxx + dlng) and (miny - dlat) <= lat <= (maxy + dlat):
        return "adjacent"
    return None


def _confidence(proximity_kind: str, provenance_status: str) -> float:
    """Score adjacency: inside/authoritative is strongest; a buffer touch or an
    unconfirmed source is weaker. Capped."""
    base = 0.80 if proximity_kind == "within" else 0.68
    if provenance_status == "sourced_unconfirmed":
        base -= 0.15
    return round(min(max(base, 0.0), 0.90), 2)


def evaluate(parcel: Dict[str, Any], overlays: List[Dict[str, Any]],
             protected_types: Optional[set] = None) -> Optional[Dict[str, Any]]:
    """PURE. Return the strongest protected-overlay adjacency for a parcel, or None.
    Considers only overlays whose type is in the protected set and whose provenance
    is emittable."""
    types = protected_types or DEFAULT_PROTECTED_TYPES
    best = None
    for ov in overlays:
        if str(ov.get("overlay_type") or "").strip().lower() not in types:
            continue
        if str(ov.get("provenance_status") or "").strip().lower() not in _EMITTABLE_PROVENANCE:
            continue
        prox = proximity(parcel, ov)
        if not prox:
            continue
        conf = _confidence(prox, str(ov.get("provenance_status") or "").strip().lower())
        cand = {"parcel_apn": parcel.get("parcel_apn"), "county_fips": parcel.get("county_fips"),
                "overlay_name": ov.get("name"), "overlay_type": ov.get("overlay_type"),
                "proximity": prox, "dispo_multiplier": _num(ov.get("multiplier_weight")),
                "confidence": conf}
        if best is None or cand["confidence"] > best["confidence"]:
            best = cand
    return best


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# CENSUS
# ---------------------------------------------------------------------------
def census(parcels: List[Dict[str, Any]], overlays: List[Dict[str, Any]],
           protected_types: Optional[set] = None) -> Dict[str, Any]:
    """Single-scope census. COVERED requires >=1 emittable PROTECTED overlay AND >=1
    parcel with geometry. Reports which overlay types ARE present so the gap is
    concrete (e.g. 'engine holds brownfield/OZ but no protected layer')."""
    types = protected_types or DEFAULT_PROTECTED_TYPES
    present_types = sorted({str(o.get("overlay_type") or "").strip().lower() for o in overlays})
    protected = [o for o in overlays
                 if str(o.get("overlay_type") or "").strip().lower() in types
                 and str(o.get("provenance_status") or "").strip().lower() in _EMITTABLE_PROVENANCE]
    with_geom = sum(1 for p in parcels if _num(p.get("lat")) is not None and _num(p.get("lng")) is not None)
    matches = sum(1 for p in parcels if evaluate(p, overlays, types))
    covered = bool(protected) and with_geom > 0
    entry = {"parcels": len(parcels), "parcels_with_geometry": with_geom,
             "overlays_total": len(overlays), "overlay_types_present": present_types,
             "protected_overlays": len(protected), "matches": matches, "covered": covered}
    if not covered:
        gaps = []
        if not protected:
            gaps.append(f"no protected/land-trust overlay ingested (present types: {present_types or 'none'})")
        if with_geom == 0:
            gaps.append("no parcel geometry")
        entry["gap"] = "; ".join(gaps)
        entry["first_task"] = ("ingest-side: stage a protected-ground layer into landos.overlay_polygons "
                               "(PAD-US / land-trust / preserve) with overlay_type in the protected set, "
                               "a buffer_m and multiplier_weight")
    return entry


def load_overlays(cur, tenant_id: str) -> List[Dict[str, Any]]:
    cur.execute(
        """select overlay_type, name, provenance_status, buffer_m, multiplier_weight,
                  minx, miny, maxx, maxy
           from landos.overlay_polygons
           where tenant_id=%s or tenant_id is null""",
        (tenant_id,))
    cols = ("overlay_type", "name", "provenance_status", "buffer_m", "multiplier_weight",
            "minx", "miny", "maxx", "maxy")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_parcels(cur, tenant_id: str) -> List[Dict[str, Any]]:
    cur.execute(
        """select county_fips, parcel_apn, situs_lat as lat, situs_lng as lng
           from landos.owner_leads
           where tenant_id=%s and parcel_apn is not null and situs_lat is not null""",
        (tenant_id,))
    return [{"county_fips": r[0], "parcel_apn": r[1], "lat": r[2], "lng": r[3]} for r in cur.fetchall()]


def census_live(cur, tenant_id: str) -> Dict[str, Any]:
    return census(load_parcels(cur, tenant_id), load_overlays(cur, tenant_id))


class ConservationAdjacencyAdapter(DiscoveryAdapter):
    """Derived DiscoveryAdapter: flags parcels within/adjacent to protected overlays.
    Census-gated; DISARMED today (overlay engine holds no protected layer)."""

    source_type = "conservation_adjacency"
    county_fips = "*"
    access_mode = "derived"
    access_path = "landos.overlay_polygons (protected types) x owner_leads situs points"
    recipe_hint = "R7"          # dispo value modifier

    def __init__(self, tenant_id: str, parcels: Optional[List[Dict[str, Any]]] = None,
                 overlays: Optional[List[Dict[str, Any]]] = None,
                 protected_types: Optional[set] = None):
        super().__init__(tenant_id)
        self.parcels = parcels or []
        self.overlays = overlays or []
        self.protected_types = protected_types or DEFAULT_PROTECTED_TYPES
        self.last_census: Dict[str, Any] = {}
        self.non_matches = 0

    def fetch(self) -> List[DiscoverySignal]:
        self.last_census = census(self.parcels, self.overlays, self.protected_types)
        self.non_matches = 0
        if not self.last_census.get("covered"):
            return []                                   # disarmed — never fabricate adjacency
        signals: List[DiscoverySignal] = []
        for p in self.parcels:
            ev = evaluate(p, self.overlays, self.protected_types)
            if not ev:
                self.non_matches += 1
                continue
            signals.append({
                "source_ref": f"{ev['county_fips']}:{ev['parcel_apn']}:conservation_adjacency",
                "source_url": None, "owner_name": None, "signal_date": None,
                "confidence": ev["confidence"], "parcel_apn": ev["parcel_apn"],
                "raw_payload": {"type": "conservation_adjacency", "parcel_apn": ev["parcel_apn"],
                                "overlay_name": ev["overlay_name"], "overlay_type": ev["overlay_type"],
                                "proximity": ev["proximity"], "dispo_multiplier": ev["dispo_multiplier"]},
            })
        return signals

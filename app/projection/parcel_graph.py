"""PARCEL_GRAPH unlock: derive adjacency from polygons already in custody.

Gap class: NORMALIZED_FACT_MISSING.
Unlock mode: DERIVED_FROM_GEOMETRY.

The plat-book graph already exists as SQL — landos.fn_parcel_plat and
landos.v_mine_same_owner_adjacent both use ST_Touches on landos.parcel_geom.
Those facts were never a normalized estate-query ingredient. This module
reuses that relation (shared boundary, interiors disjoint) in stdlib against
GeoJSON / WKT / rings the query already holds.

No PostGIS call. No shapely. No scraper. No new GIS buy. No Supabase write.
A missing polygon stays UNKNOWN — never a fabricated neighbor or acreage.
Scope is the in-custody corpus, not a county-complete cadastral graph.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

from app.overlays import point_in_geometry

from .classes import (
    FAMILY_PARCEL_GRAPH,
    INGREDIENT_ADJACENT_APNS,
    INGREDIENT_SAME_OWNER_ADJACENT,
    INGREDIENT_SHARED_BOUNDARY,
    NOT_RAW_SOURCE_MISSING,
    STATE_KNOWN,
    STATE_UNKNOWN,
    unlock_classes,
)
from .numbers import first_present, first_text, row_apn

# Existing geometry spellings already written by load_parcel_geom, county REST,
# fn_parcel_plat, and ArcGIS feature rows. Do not add a key unless a writer
# already emits it.
GEOM_KEYS = (
    "geom",
    "geometry",
    "geojson",
    "polygon",
    "parcel_geom",
    "wkt",
    "geom_wkt",
    "rings",
)

# fn_parcel_plat output already attached to a query/pin envelope.
PLAT_KEYS = ("plat", "parcel_plat", "fn_parcel_plat")

COORD_EPS = 1e-8
NEIGHBOR_CAP = 12  # same cap as migrations/023_fn_parcel_plat_owner_join.sql
RELATION_ST_TOUCHES = "ST_Touches"
KIND_EDGE = "EDGE"
KIND_VERTEX = "VERTEX"
SCOPE_CORPUS = "CORPUS"

_WKT_PAIR = re.compile(r"(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)")
_WKT_PREFIX = re.compile(r"^\s*(MULTI)?POLYGON\s*", re.IGNORECASE)

FIELD_CENSUS: tuple[dict[str, str], ...] = (
    {
        "field": "landos.parcel_geom.geom",
        "writer": "scripts/load_parcel_geom.py",
        "when": "county MultiPolygon 4326 already loaded",
        "path": "geometry custody (engine reads; ingest writes)",
    },
    {
        "field": "fn_parcel_plat neighbors",
        "writer": "migrations/023_fn_parcel_plat_owner_join.sql",
        "when": "ST_Touches subject + neighbors (cap 12)",
        "path": "plat FeatureCollection",
    },
    {
        "field": "v_mine_same_owner_adjacent",
        "writer": "migrations/028_owner_roll_mines.sql",
        "when": "ST_Touches + exact-normalized same owner",
        "path": "owner-roll mine (not a query ingredient)",
    },
    {
        "field": "field_pins plat envelope",
        "writer": "app/field_pins.py:_plat",
        "when": "capture envelope already calls fn_parcel_plat",
        "path": "field-pin read path",
    },
)


def field_census() -> list[dict[str, str]]:
    """Exact existing-geometry census. Empty invented list is the contract."""
    return [dict(row) for row in FIELD_CENSUS]


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _coord(value: Any) -> Optional[tuple[float, float]]:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        lng = float(value[0])
        lat = float(value[1])
    except (TypeError, ValueError):
        return None
    if lng != lng or lat != lat:
        return None
    return (lng, lat)


def _ring_from_coords(coords: Any) -> list[tuple[float, float]]:
    ring: list[tuple[float, float]] = []
    if not isinstance(coords, (list, tuple)):
        return ring
    for raw in coords:
        pt = _coord(raw)
        if pt is not None:
            ring.append(pt)
    return ring


def _rings_from_polygon_coords(coords: Any) -> list[list[tuple[float, float]]]:
    """GeoJSON Polygon coordinates -> exterior rings (holes ignored for touch)."""
    if not isinstance(coords, (list, tuple)) or not coords:
        return []
    exterior = _ring_from_coords(coords[0])
    return [exterior] if len(exterior) >= 4 else []


def _rings_from_wkt(text: str) -> list[list[tuple[float, float]]]:
    if not _WKT_PREFIX.match(text):
        return []
    rings: list[list[tuple[float, float]]] = []
    for chunk in re.findall(r"\(\(([^()]+)\)\)", text):
        ring = [(float(x), float(y)) for x, y in _WKT_PAIR.findall(chunk)]
        if len(ring) >= 4:
            rings.append(ring)
    return rings


def _geojson_from_rings(rings: list[list[tuple[float, float]]]) -> Optional[dict[str, Any]]:
    if not rings:
        return None
    coords = [[[lng, lat] for lng, lat in ring] for ring in rings]
    if len(coords) == 1:
        return {"type": "Polygon", "coordinates": coords}
    return {"type": "MultiPolygon", "coordinates": [[ring] for ring in coords]}


def rings_from_geometry(value: Any) -> list[list[tuple[float, float]]]:
    """Pull exterior rings from GeoJSON / WKT / ArcGIS rings. Empty if unknown."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return _rings_from_wkt(value)
    if isinstance(value, (list, tuple)):
        if value and _coord(value[0]) is not None:
            ring = _ring_from_coords(value)
            return [ring] if len(ring) >= 4 else []
        # ArcGIS rings: list of rings of [lng,lat]
        rings = []
        for part in value:
            ring = _ring_from_coords(part)
            if len(ring) >= 4:
                rings.append(ring)
        return rings
    if not isinstance(value, dict):
        return []
    gtype = str(value.get("type") or "")
    if gtype == "Feature":
        return rings_from_geometry(value.get("geometry"))
    if gtype == "FeatureCollection":
        rings: list[list[tuple[float, float]]] = []
        for feat in value.get("features") or []:
            props = _as_mapping(feat.get("properties") if isinstance(feat, dict) else {})
            role = str(props.get("kind") or props.get("role") or "").lower()
            if role == "neighbor":
                continue
            rings.extend(rings_from_geometry(feat))
        return rings
    if gtype == "Polygon":
        return _rings_from_polygon_coords(value.get("coordinates"))
    if gtype == "MultiPolygon":
        rings: list[list[tuple[float, float]]] = []
        for poly in value.get("coordinates") or []:
            rings.extend(_rings_from_polygon_coords(poly))
        return rings
    if gtype == "Point":
        return []
    if "coordinates" in value:
        guessed = rings_from_geometry(
            {"type": "Polygon", "coordinates": value.get("coordinates")}
        )
        if guessed:
            return guessed
    if "rings" in value:
        return rings_from_geometry(value.get("rings"))
    if "wkt" in value:
        return rings_from_geometry(value.get("wkt"))
    if "geom" in value:
        return rings_from_geometry(value.get("geom"))
    if "geometry" in value and value.get("geometry") is not value:
        return rings_from_geometry(value.get("geometry"))
    return []


def _row_geometry_value(row: dict) -> Any:
    direct = first_present(row, GEOM_KEYS)
    if direct is not None:
        return direct
    for nest_key in ("parcel_meta", "raw_meta"):
        nested = row.get(nest_key)
        if isinstance(nested, dict):
            hit = first_present(nested, GEOM_KEYS)
            if hit is not None:
                return hit
    for plat_key in PLAT_KEYS:
        if plat_key in row:
            return row.get(plat_key)
    return None


def extract_rings(row: Optional[dict]) -> list[list[tuple[float, float]]]:
    return rings_from_geometry(_row_geometry_value(dict(row or {})))


def _plat_neighbor_rows(row: dict) -> list[dict[str, Any]]:
    """Harvest neighbor Features already labeled by fn_parcel_plat."""
    plat = None
    for key in PLAT_KEYS:
        if key in row:
            plat = row.get(key)
            break
    if plat is None:
        meta = row.get("parcel_meta")
        if isinstance(meta, dict):
            plat = first_present(meta, PLAT_KEYS)
    if not isinstance(plat, dict) or plat.get("type") != "FeatureCollection":
        return []
    out: list[dict[str, Any]] = []
    for feat in plat.get("features") or []:
        if not isinstance(feat, dict):
            continue
        props = _as_mapping(feat.get("properties"))
        role = str(props.get("kind") or props.get("role") or "").lower()
        if role != "neighbor":
            continue
        apn = first_text(props, ("apn", "parcel_apn", "parcel_id"))
        if not apn:
            continue
        geom = feat.get("geometry")
        if not rings_from_geometry(geom):
            continue
        out.append({
            "apn": apn,
            "parcel_apn": apn,
            "county_fips": first_text(props, ("county_fips", "fips")) or row_county(row),
            "owner_name": first_text(props, ("owner_name", "owner")),
            "vintage_flag": first_text(props, ("vintage_flag", "owner_flag")),
            "geometry": geom,
            "source_field": "plat",
        })
    return out


def row_county(row: Optional[dict]) -> Optional[str]:
    raw = first_present(dict(row or {}), ("county_fips", "fips"))
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits or None


def owner_norm(row: Optional[dict]) -> Optional[str]:
    name = first_text(dict(row or {}), ("owner_name", "owner"))
    if not name:
        return None
    return " ".join(name.upper().split())


def _is_govt(row: Optional[dict]) -> bool:
    flag = first_text(dict(row or {}), ("vintage_flag", "owner_flag"))
    return bool(flag) and flag.strip().lower() == "govt"


def _same_county(subject: dict, other: dict) -> bool:
    a = row_county(subject)
    b = row_county(other)
    if a is None or b is None:
        return False
    return a == b


def _same_parcel(subject: dict, other: dict) -> bool:
    """Identity for graph self-skip. Do not use short digit keys from apn_keys
    — R-EAST-1 and L-WEST-1 both collapse to '1' and would false-merge."""
    a = row_apn(subject)
    b = row_apn(other)
    if not a or not b:
        return False
    if a.strip().upper() == b.strip().upper():
        return True
    a_alnum = "".join(ch for ch in a.upper() if ch.isalnum())
    b_alnum = "".join(ch for ch in b.upper() if ch.isalnum())
    if a_alnum and a_alnum == b_alnum:
        return True
    a_digits = "".join(ch for ch in a if ch.isdigit())
    b_digits = "".join(ch for ch in b if ch.isdigit())
    if len(a_digits) >= 6 and a_digits == b_digits:
        return True
    return False


def _bbox(ring: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _bboxes_touch(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
    eps: float = COORD_EPS,
) -> bool:
    return not (
        a[2] < b[0] - eps
        or b[2] < a[0] - eps
        or a[3] < b[1] - eps
        or b[3] < a[1] - eps
    )


def _edges(ring: list[tuple[float, float]]) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    if len(ring) < 2:
        return []
    pts = list(ring)
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1) if pts[i] != pts[i + 1]]


def _near(a: tuple[float, float], b: tuple[float, float], eps: float = COORD_EPS) -> bool:
    return abs(a[0] - b[0]) <= eps and abs(a[1] - b[1]) <= eps


def _cross(
    o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _on_segment(
    p: tuple[float, float],
    a: tuple[float, float],
    b: tuple[float, float],
    eps: float = COORD_EPS,
) -> bool:
    if abs(_cross(a, b, p)) > eps:
        return False
    return (
        min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
    )


def _seg_overlap_length(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
    eps: float = COORD_EPS,
) -> float:
    """Length of collinear overlap, or 0. Not a survey measure — relation only."""
    if abs(_cross(a1, a2, b1)) > eps or abs(_cross(a1, a2, b2)) > eps:
        return 0.0
    dx = a2[0] - a1[0]
    dy = a2[1] - a1[1]
    span = (dx * dx + dy * dy) ** 0.5
    if span <= eps:
        return 0.0
    def t(p: tuple[float, float]) -> float:
        return ((p[0] - a1[0]) * dx + (p[1] - a1[1]) * dy) / (span * span)
    lo = max(0.0, min(t(b1), t(b2)))
    hi = min(1.0, max(t(b1), t(b2)))
    if hi - lo <= eps / max(span, eps):
        return 0.0
    return (hi - lo) * span


def _point_on_rings(p: tuple[float, float], rings: list[list[tuple[float, float]]]) -> bool:
    for ring in rings:
        for a, b in _edges(ring):
            if _on_segment(p, a, b):
                return True
    return False


def _centroid(ring: list[tuple[float, float]]) -> Optional[tuple[float, float]]:
    pts = ring[:-1] if len(ring) >= 2 and ring[0] == ring[-1] else ring
    if not pts:
        return None
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _strictly_inside(
    p: tuple[float, float], rings: list[list[tuple[float, float]]]
) -> bool:
    if _point_on_rings(p, rings):
        return False
    geom = _geojson_from_rings(rings)
    if geom is None:
        return False
    return point_in_geometry(p[1], p[0], geom)


def _interiors_intersect(
    a: list[list[tuple[float, float]]], b: list[list[tuple[float, float]]]
) -> bool:
    samples: list[tuple[float, float]] = []
    for ring in a:
        cen = _centroid(ring)
        if cen is not None:
            samples.append(cen)
        for pt in ring:
            samples.append(pt)
    for p in samples:
        if _strictly_inside(p, b):
            return True
    return False


def classify_touch(
    subject_rings: list[list[tuple[float, float]]],
    other_rings: list[list[tuple[float, float]]],
) -> Optional[str]:
    """ST_Touches equivalent: boundaries meet, interiors disjoint.

    Returns EDGE (shared boundary segment), VERTEX (point-only), or None.
    Overlap / containment is not adjacency — never reported as a neighbor.
    """
    if not subject_rings or not other_rings:
        return None
    sub_boxes = [_bbox(r) for r in subject_rings]
    other_boxes = [_bbox(r) for r in other_rings]
    if not any(_bboxes_touch(sb, ob) for sb in sub_boxes for ob in other_boxes):
        return None
    if _interiors_intersect(subject_rings, other_rings) or _interiors_intersect(
        other_rings, subject_rings
    ):
        return None
    shared_vertex = False
    shared_edge = False
    for s_ring in subject_rings:
        for o_ring in other_rings:
            o_edges = _edges(o_ring)
            for pt in s_ring:
                if any(_on_segment(pt, a, b) for a, b in o_edges):
                    shared_vertex = True
            for a1, a2 in _edges(s_ring):
                for b1, b2 in o_edges:
                    if _seg_overlap_length(a1, a2, b1, b2) > COORD_EPS:
                        shared_edge = True
                    elif _on_segment(a1, b1, b2) or _on_segment(a2, b1, b2) or _on_segment(
                        b1, a1, a2
                    ) or _on_segment(b2, a1, a2):
                        shared_vertex = True
    if shared_edge:
        return KIND_EDGE
    if shared_vertex:
        return KIND_VERTEX
    return None


def unique_geom_rows(rows: Iterable[Mapping[str, Any] | None]) -> list[dict[str, Any]]:
    """Deduplicate parcel dicts (the APN index stores the same row under many keys)."""
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for raw in rows:
        if raw is None:
            continue
        row = dict(raw)
        # The APN index stores the same mapping under many keys — identity, not
        # a copied dict, is what we need to dedupe.
        source_id = id(raw)
        if source_id in seen:
            continue
        if not row_apn(row):
            continue
        if not extract_rings(row):
            continue
        seen.add(source_id)
        out.append(row)
    return out


def build_geom_corpus(
    rows: Optional[Iterable[Mapping[str, Any]]] = None,
    ingested_parcels: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """In-custody polygon rows only. Point / acreage-only rows are dropped."""
    pile: list[Mapping[str, Any]] = []
    if rows:
        pile.extend(rows)
    if ingested_parcels:
        pile.extend(ingested_parcels.values())
    return unique_geom_rows(pile)


def _neighbor_record(
    other: dict,
    kind: str,
    *,
    same_owner: bool,
    source_field: str,
) -> dict[str, Any]:
    return {
        "apn": row_apn(other),
        "relation": RELATION_ST_TOUCHES,
        "shared_boundary": kind,
        "same_owner": same_owner,
        "source_field": source_field,
    }


def derive_parcel_graph(
    estate_row: Optional[dict] = None,
    ingested: Optional[dict] = None,
    geom_corpus: Optional[Iterable[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Derive ST_Touches neighbors from polygons already on the query/corpus."""
    overlay = dict(ingested or {})
    row = dict(estate_row or {})
    subject = dict(row)
    if overlay:
        # Overlay wins for identity / geom / owner, same as other unlocks.
        subject.update(overlay)

    subject_rings = extract_rings(overlay) or extract_rings(row)
    source_field = None
    if extract_rings(overlay):
        source_field = next(
            (k for k in (*GEOM_KEYS, *PLAT_KEYS) if first_present(overlay, (k,)) is not None),
            "ingested.geometry",
        )
    elif extract_rings(row):
        source_field = next(
            (k for k in (*GEOM_KEYS, *PLAT_KEYS) if first_present(row, (k,)) is not None),
            "estate.geometry",
        )

    candidates: list[dict[str, Any]] = []
    if geom_corpus is not None:
        candidates.extend(unique_geom_rows(geom_corpus))
    candidates.extend(_plat_neighbor_rows(subject))
    if overlay:
        candidates.extend(unique_geom_rows([overlay]))

    peer_rings_present = False
    neighbors: list[dict[str, Any]] = []
    seen_apn: set[str] = set()
    for other in candidates:
        if _same_parcel(subject, other):
            continue
        if not _same_county(subject, other):
            continue
        other_rings = extract_rings(other)
        if not other_rings:
            continue
        peer_rings_present = True
        kind = classify_touch(subject_rings, other_rings)
        if kind is None:
            continue
        apn = row_apn(other)
        if apn in seen_apn:
            continue
        seen_apn.add(apn)
        same_owner = (
            not _is_govt(subject)
            and owner_norm(subject) is not None
            and owner_norm(subject) == owner_norm(other)
        )
        other_source = other.get("source_field") if other.get("source_field") == "plat" else (
            source_field or "geometry"
        )
        neighbors.append(
            _neighbor_record(
                other,
                kind,
                same_owner=same_owner,
                source_field=str(other_source),
            )
        )

    neighbors.sort(key=lambda item: item["apn"])
    neighbors = neighbors[:NEIGHBOR_CAP]
    return {
        "subject_rings": subject_rings,
        "source_field": source_field,
        "peer_rings_present": peer_rings_present,
        "neighbors": neighbors,
        "adjacent_apns": [item["apn"] for item in neighbors],
        "edge_apns": [item["apn"] for item in neighbors if item["shared_boundary"] == KIND_EDGE],
        "same_owner_apns": [item["apn"] for item in neighbors if item["same_owner"]],
    }


def _unknown_fact(ingredient_id: str, *, reason: str, why: str) -> dict[str, Any]:
    gap, mode = unlock_classes(FAMILY_PARCEL_GRAPH)
    return {
        "ingredient_id": ingredient_id,
        "family": FAMILY_PARCEL_GRAPH,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "kind": "DERIVED",
        "state": STATE_UNKNOWN,
        "value": None,
        "source_field": None,
        "reason": reason,
        "why": why,
    }


def _known_fact(
    ingredient_id: str,
    value: Any,
    *,
    source_field: Optional[str],
    reason: str,
    why: str,
) -> dict[str, Any]:
    gap, mode = unlock_classes(FAMILY_PARCEL_GRAPH)
    return {
        "ingredient_id": ingredient_id,
        "family": FAMILY_PARCEL_GRAPH,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "kind": "DERIVED",
        "state": STATE_KNOWN,
        "value": value,
        "source_field": source_field,
        "reason": reason,
        "why": why,
    }


def project_parcel_graph_facts(
    estate_row: Optional[dict] = None,
    ingested: Optional[dict] = None,
    geom_corpus: Optional[Iterable[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Normalized parcel-graph facts. UNKNOWN when geometry cannot support a graph."""
    gap, mode = unlock_classes(FAMILY_PARCEL_GRAPH)
    derived = derive_parcel_graph(estate_row, ingested, geom_corpus)
    base = {
        "family": FAMILY_PARCEL_GRAPH,
        "gap_class": gap,
        "unlock_mode": mode,
        "not_class": NOT_RAW_SOURCE_MISSING,
        "relation": RELATION_ST_TOUCHES,
        "scope": SCOPE_CORPUS,
        "neighbor_cap": NEIGHBOR_CAP,
        "live": False,
        "invented_neighbors": [],
        "supabase_mutated": False,
        "scraper": False,
        "neighbors": [],
    }

    if not derived["subject_rings"]:
        why = (
            "Parcel-graph adjacency is ST_Touches on polygons already in custody "
            "(landos.parcel_geom / plat GeoJSON). This row has no polygon — "
            "acreage, a point, or land-use is not a shared boundary. UNKNOWN, "
            "never a fabricated neighbor."
        )
        return {
            **base,
            "reason": "GEOMETRY_NOT_ON_QUERY",
            "facts": {
                INGREDIENT_ADJACENT_APNS: _unknown_fact(
                    INGREDIENT_ADJACENT_APNS, reason="GEOMETRY_NOT_ON_QUERY", why=why
                ),
                INGREDIENT_SHARED_BOUNDARY: _unknown_fact(
                    INGREDIENT_SHARED_BOUNDARY, reason="GEOMETRY_NOT_ON_QUERY", why=why
                ),
                INGREDIENT_SAME_OWNER_ADJACENT: _unknown_fact(
                    INGREDIENT_SAME_OWNER_ADJACENT, reason="GEOMETRY_NOT_ON_QUERY", why=why
                ),
            },
        }

    if not derived["peer_rings_present"]:
        why = (
            "Subject polygon is in custody but no other same-county polygon is "
            "on this query corpus. Cannot assert isolation or neighbors — the "
            "county layer may hold parcels we do not have here. UNKNOWN, never "
            "a fabricated empty graph."
        )
        return {
            **base,
            "reason": "NEIGHBOR_CORPUS_EMPTY",
            "facts": {
                INGREDIENT_ADJACENT_APNS: _unknown_fact(
                    INGREDIENT_ADJACENT_APNS, reason="NEIGHBOR_CORPUS_EMPTY", why=why
                ),
                INGREDIENT_SHARED_BOUNDARY: _unknown_fact(
                    INGREDIENT_SHARED_BOUNDARY, reason="NEIGHBOR_CORPUS_EMPTY", why=why
                ),
                INGREDIENT_SAME_OWNER_ADJACENT: _unknown_fact(
                    INGREDIENT_SAME_OWNER_ADJACENT, reason="NEIGHBOR_CORPUS_EMPTY", why=why
                ),
            },
        }

    src = derived["source_field"] or "geometry"
    neighbors = derived["neighbors"]
    adjacent = derived["adjacent_apns"]
    edge = derived["edge_apns"]
    same_owner = derived["same_owner_apns"]
    why = (
        f"Derived {RELATION_ST_TOUCHES} neighbors from in-custody polygons "
        f"({src}); scope={SCOPE_CORPUS}, cap={NEIGHBOR_CAP}."
    )
    return {
        **base,
        "reason": "DERIVED_FROM_GEOMETRY",
        "source_field": src,
        "neighbors": neighbors,
        "facts": {
            INGREDIENT_ADJACENT_APNS: _known_fact(
                INGREDIENT_ADJACENT_APNS,
                adjacent,
                source_field=src,
                reason="DERIVED_ST_TOUCHES",
                why=why,
            ),
            INGREDIENT_SHARED_BOUNDARY: _known_fact(
                INGREDIENT_SHARED_BOUNDARY,
                bool(edge),
                source_field=src,
                reason="DERIVED_SHARED_EDGE" if edge else "NO_SHARED_EDGE_IN_CORPUS",
                why=why,
            ),
            INGREDIENT_SAME_OWNER_ADJACENT: _known_fact(
                INGREDIENT_SAME_OWNER_ADJACENT,
                same_owner,
                source_field=src,
                reason="DERIVED_SAME_OWNER_ST_TOUCHES",
                why=why,
            ),
        },
    }

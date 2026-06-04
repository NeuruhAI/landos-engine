from app.overlays import point_in_geometry, overlay_hits

SQUARE = {"type": "Polygon", "coordinates": [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]]}
SQUARE_WITH_HOLE = {
    "type": "Polygon",
    "coordinates": [
        [[-2, -2], [2, -2], [2, 2], [-2, 2], [-2, -2]],
        [[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]],
    ],
}
MULTI = {
    "type": "MultiPolygon",
    "coordinates": [
        [[[-1, -1], [1, -1], [1, 1], [-1, 1], [-1, -1]]],
        [[[9, 9], [11, 9], [11, 11], [9, 11], [9, 9]]],
    ],
}


def test_inside_square():
    assert point_in_geometry(0.0, 0.0, SQUARE) is True


def test_outside_square():
    assert point_in_geometry(5.0, 5.0, SQUARE) is False


def test_hole_excluded():
    assert point_in_geometry(0.0, 0.0, SQUARE_WITH_HOLE) is False  # in the hole
    assert point_in_geometry(1.5, 1.5, SQUARE_WITH_HOLE) is True   # ring between hole and outer


def test_multipolygon():
    assert point_in_geometry(10.0, 10.0, MULTI) is True
    assert point_in_geometry(0.0, 0.0, MULTI) is True
    assert point_in_geometry(5.0, 5.0, MULTI) is False


def test_point_buffer():
    pt = {"type": "Point", "coordinates": [-86.45, 42.11]}  # [lng, lat]
    assert point_in_geometry(42.1105, -86.45, pt, buffer_m=200) is True   # ~55 m away
    assert point_in_geometry(42.20, -86.45, pt, buffer_m=200) is False    # ~10 km away


def test_empty_geometry():
    assert point_in_geometry(0.0, 0.0, None) is False
    assert point_in_geometry(0.0, 0.0, {"type": "Polygon", "coordinates": []}) is False


def test_overlay_hits_with_bbox_prefilter():
    overlays = [
        {
            "overlay_type": "opportunity_zone", "name": "Z1",
            "provenance_status": "authoritative", "multiplier_weight": 0.30,
            "effective_date": "2026-12-31", "source": "tigerweb",
            "geometry": SQUARE, "minx": -1, "miny": -1, "maxx": 1, "maxy": 1,
        },
        {
            "overlay_type": "brownfield", "name": "B1",
            "provenance_status": "sourced_unconfirmed", "multiplier_weight": 0.0,
            "source": "egle", "geometry": {"type": "Point", "coordinates": [50, 50]},
            "buffer_m": 100, "minx": 50, "miny": 50, "maxx": 50, "maxy": 50,
        },
    ]
    hits = overlay_hits(0.0, 0.0, overlays)
    assert len(hits) == 1
    assert hits[0]["overlay_type"] == "opportunity_zone"
    assert hits[0]["provenance_status"] == "authoritative"

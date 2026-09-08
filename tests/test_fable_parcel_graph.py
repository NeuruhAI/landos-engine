"""Fable PARCEL_GRAPH unlock: ST_Touches from polygons already in custody.

Gap: NORMALIZED_FACT_MISSING. Mode: DERIVED_FROM_GEOMETRY.
Reuses fn_parcel_plat / v_mine_same_owner_adjacent (ST_Touches), does not
invent a cadastral scrape. No Supabase. No DeedSonar. No LIVE path.
UNKNOWN when there is no polygon or no peer polygon in the corpus —
never a fabricated neighbor or acreage.
"""
from __future__ import annotations

from pathlib import Path

from app.free_routes.estate_rows import load_estate_rows
from app.projection.classes import (
    FAMILY_PARCEL_GRAPH,
    GAP_NORMALIZED_FACT_MISSING,
    INGREDIENT_ADJACENT_APNS,
    INGREDIENT_SAME_OWNER_ADJACENT,
    INGREDIENT_SHARED_BOUNDARY,
    NOT_RAW_SOURCE_MISSING,
    UNLOCK_DERIVED_FROM_GEOMETRY,
    unlock_classes,
)
from app.projection.estate_query import (
    index_ingested_parcels,
    project_estate_query,
    project_onto_estate_row,
)
from app.projection.parcel_graph import (
    KIND_EDGE,
    KIND_VERTEX,
    _same_parcel,
    classify_touch,
    field_census,
    project_parcel_graph_facts,
    rings_from_geometry,
)


def _poly(lon_min, lat_min, lon_max, lat_max):
    """Axis-aligned WGS84 polygon — same construction as test_field_pin_live."""
    return {
        "type": "Polygon",
        "coordinates": [[
            [lon_min, lat_min],
            [lon_max, lat_min],
            [lon_max, lat_max],
            [lon_min, lat_max],
            [lon_min, lat_min],
        ]],
    }


# Field-pin isolated scene (county 99001): R-EAST-1 shares an edge with R-EAST-2.
EAST_1 = _poly(-86.6996, 41.5995, -86.6990, 41.6005)
EAST_2 = _poly(-86.6990, 41.5995, -86.6984, 41.6005)
WEST_1 = _poly(-86.7010, 41.5995, -86.7004, 41.6005)


def test_gap_mode_is_normalized_fact_missing_derived_from_geometry():
    gap, mode = unlock_classes(FAMILY_PARCEL_GRAPH)
    assert gap == GAP_NORMALIZED_FACT_MISSING
    assert mode == UNLOCK_DERIVED_FROM_GEOMETRY
    assert gap == "NORMALIZED_FACT_MISSING"
    assert mode == "DERIVED_FROM_GEOMETRY"


def test_census_names_existing_geometry_seams_only():
    census = field_census()
    fields = {row["field"] for row in census}
    assert fields == {
        "landos.parcel_geom.geom",
        "fn_parcel_plat neighbors",
        "v_mine_same_owner_adjacent",
        "field_pins plat envelope",
    }
    blob = " ".join(row["writer"].lower() for row in census)
    assert "invent" not in blob
    assert "scrape" not in blob


def test_classify_touch_matches_field_pin_shared_edge():
    a = rings_from_geometry(EAST_1)
    b = rings_from_geometry(EAST_2)
    west = rings_from_geometry(WEST_1)
    assert classify_touch(a, b) == KIND_EDGE
    assert classify_touch(b, a) == KIND_EDGE
    assert classify_touch(a, west) is None
    assert classify_touch(a, a) is None  # interiors overlap; self is not adjacent


def test_short_digit_suffix_does_not_make_two_apns_the_same_parcel():
    assert _same_parcel({"apn": "R-EAST-1"}, {"apn": "L-WEST-1"}) is False
    assert _same_parcel({"apn": "0648-08-0648"}, {"apn": "0648080648"}) is True
    assert _same_parcel({"apn": "R-EAST-1"}, {"apn": "R-EAST-1"}) is True


def test_overlapping_polygons_are_not_adjacent():
    overlap = _poly(-86.6994, 41.5996, -86.6988, 41.6004)  # overlaps EAST_1
    assert classify_touch(rings_from_geometry(EAST_1), rings_from_geometry(overlap)) is None


def test_corner_only_is_st_touches_vertex_not_shared_edge():
    corner = _poly(-86.6990, 41.6005, -86.6984, 41.6010)  # touches EAST_1 at NE corner
    assert classify_touch(rings_from_geometry(EAST_1), rings_from_geometry(corner)) == KIND_VERTEX


def test_derive_adjacent_from_ingested_polygons():
    ingested = index_ingested_parcels([
        {"parcel_apn": "R-EAST-1", "county_fips": "99001", "owner_name": "SMITH FAMILY TRUST",
         "geometry": EAST_1},
        {"parcel_apn": "R-EAST-2", "county_fips": "99001", "owner_name": "SMITH FAMILY TRUST",
         "geometry": EAST_2},
        {"parcel_apn": "L-WEST-1", "county_fips": "99001", "owner_name": "ROE",
         "geometry": WEST_1},
    ])
    fact = project_parcel_graph_facts(
        {"apn": "R-EAST-1", "county_fips": "99001"},
        ingested["R-EAST-1"],
        ingested.values(),
    )
    adj = fact["facts"][INGREDIENT_ADJACENT_APNS]
    shared = fact["facts"][INGREDIENT_SHARED_BOUNDARY]
    same = fact["facts"][INGREDIENT_SAME_OWNER_ADJACENT]
    assert fact["family"] == FAMILY_PARCEL_GRAPH
    assert fact["gap_class"] == GAP_NORMALIZED_FACT_MISSING
    assert fact["unlock_mode"] == UNLOCK_DERIVED_FROM_GEOMETRY
    assert fact["not_class"] == NOT_RAW_SOURCE_MISSING
    assert fact["live"] is False
    assert fact["invented_neighbors"] == []
    assert adj["state"] == "KNOWN"
    assert adj["value"] == ["R-EAST-2"]
    assert shared["value"] is True
    assert same["value"] == ["R-EAST-2"]
    assert fact["neighbors"][0]["relation"] == "ST_Touches"
    assert fact["neighbors"][0]["shared_boundary"] == KIND_EDGE


def test_wkt_multipolygon_is_existing_geom_spelling():
    wkt = (
        "MULTIPOLYGON(((-86.6996 41.5995,-86.6990 41.5995,"
        "-86.6990 41.6005,-86.6996 41.6005,-86.6996 41.5995)))"
    )
    fact = project_parcel_graph_facts(
        {"apn": "R-EAST-1", "county_fips": "99001", "wkt": wkt},
        None,
        [{"apn": "R-EAST-2", "county_fips": "99001", "geometry": EAST_2}],
    )
    assert fact["facts"][INGREDIENT_ADJACENT_APNS]["value"] == ["R-EAST-2"]
    assert fact["facts"][INGREDIENT_ADJACENT_APNS]["source_field"] == "wkt"


def test_govt_anchor_is_not_same_owner_assemblage():
    fact = project_parcel_graph_facts(
        {"apn": "R-EAST-1", "county_fips": "99001", "owner_name": "STATE OF INDIANA DNR",
         "vintage_flag": "govt", "geometry": EAST_1},
        None,
        [{"apn": "R-EAST-2", "county_fips": "99001", "owner_name": "STATE OF INDIANA DNR",
          "geometry": EAST_2}],
    )
    assert fact["facts"][INGREDIENT_ADJACENT_APNS]["value"] == ["R-EAST-2"]
    assert fact["facts"][INGREDIENT_SAME_OWNER_ADJACENT]["value"] == []


def test_different_county_never_adjacent():
    fact = project_parcel_graph_facts(
        {"apn": "R-EAST-1", "county_fips": "99001", "geometry": EAST_1},
        None,
        [{"apn": "R-EAST-2", "county_fips": "18091", "geometry": EAST_2}],
    )
    assert fact["reason"] == "NEIGHBOR_CORPUS_EMPTY"
    assert fact["facts"][INGREDIENT_ADJACENT_APNS]["state"] == "UNKNOWN"


def test_acreage_only_stays_unknown_never_fabricated_graph():
    fact = project_parcel_graph_facts(
        {"apn": "A", "county_fips": "37183", "acreage": 12.4, "land_use": "AG"},
        {"acreage": 99.0, "land_use_class": "401"},
    )
    assert fact["reason"] == "GEOMETRY_NOT_ON_QUERY"
    for ingredient in (
        INGREDIENT_ADJACENT_APNS,
        INGREDIENT_SHARED_BOUNDARY,
        INGREDIENT_SAME_OWNER_ADJACENT,
    ):
        assert fact["facts"][ingredient]["state"] == "UNKNOWN"
        assert fact["facts"][ingredient]["value"] is None
        assert fact["facts"][ingredient]["gap_class"] == GAP_NORMALIZED_FACT_MISSING
        assert fact["facts"][ingredient]["not_class"] == NOT_RAW_SOURCE_MISSING


def test_point_geometry_is_not_a_shared_boundary():
    fact = project_parcel_graph_facts({
        "apn": "P",
        "county_fips": "99001",
        "geometry": {"type": "Point", "coordinates": [-86.6993, 41.6]},
        "situs_lat": 41.6,
        "situs_lng": -86.6993,
    })
    assert fact["reason"] == "GEOMETRY_NOT_ON_QUERY"


def test_subject_polygon_without_peers_is_unknown_not_empty_graph():
    fact = project_parcel_graph_facts(
        {"apn": "R-EAST-1", "county_fips": "99001", "geometry": EAST_1}
    )
    assert fact["reason"] == "NEIGHBOR_CORPUS_EMPTY"
    assert fact["facts"][INGREDIENT_ADJACENT_APNS]["value"] is None
    assert fact["invented_neighbors"] == []


def test_peer_polygons_that_do_not_touch_are_known_empty():
    fact = project_parcel_graph_facts(
        {"apn": "R-EAST-1", "county_fips": "99001", "geometry": EAST_1},
        None,
        [{"apn": "L-WEST-1", "county_fips": "99001", "geometry": WEST_1}],
    )
    assert fact["reason"] == "DERIVED_FROM_GEOMETRY"
    assert fact["facts"][INGREDIENT_ADJACENT_APNS]["state"] == "KNOWN"
    assert fact["facts"][INGREDIENT_ADJACENT_APNS]["value"] == []
    assert fact["facts"][INGREDIENT_SHARED_BOUNDARY]["value"] is False


def test_estate_query_exposes_graph_from_ingested_polygons():
    ingested = index_ingested_parcels([
        {"parcel_apn": "R-EAST-1", "county_fips": "99001", "owner_name": "SMITH",
         "geometry": EAST_1},
        {"parcel_apn": "R-EAST-2", "county_fips": "99001", "owner_name": "SMITH",
         "geometry": EAST_2},
    ])
    hit = project_onto_estate_row(
        {"apn": "R-EAST-1", "county_fips": "99001", "owner_name": "SMITH"},
        ingested,
    )
    miss = project_onto_estate_row(
        {"apn": "NO-GEOM", "county_fips": "99001", "acreage": 2.5},
        ingested,
    )
    graph = hit["projected"]["parcel_graph"]
    assert hit["adjacent_apns"] == ["R-EAST-2"]
    assert hit["shared_boundary"] is True
    assert hit["same_owner_adjacent"] == ["R-EAST-2"]
    assert graph["facts"][INGREDIENT_ADJACENT_APNS]["value"] == ["R-EAST-2"]
    assert FAMILY_PARCEL_GRAPH in hit["projected"]["families"]
    assert hit["projected"]["invented_neighbors"] == []
    assert hit["projected"]["supabase_mutated"] is False
    assert hit["projected"]["scraper"] is False
    assert "adjacent_apns" not in miss
    assert miss["projected"]["parcel_graph"]["facts"][INGREDIENT_ADJACENT_APNS]["state"] == "UNKNOWN"


def test_estate_query_does_not_clobber_existing_adjacent_column():
    ingested = index_ingested_parcels([
        {"parcel_apn": "R-EAST-1", "county_fips": "99001", "geometry": EAST_1},
        {"parcel_apn": "R-EAST-2", "county_fips": "99001", "geometry": EAST_2},
    ])
    row = project_onto_estate_row(
        {"apn": "R-EAST-1", "county_fips": "99001", "adjacent_apns": ["HELD"]},
        ingested,
    )
    assert row["adjacent_apns"] == ["HELD"]
    assert row["projected"]["parcel_graph"]["facts"][INGREDIENT_ADJACENT_APNS]["value"] == [
        "R-EAST-2"
    ]


def test_batch_query_derives_graph_across_estate_rows():
    rows = project_estate_query([
        {"apn": "R-EAST-1", "county_fips": "99001", "owner_name": "SMITH",
         "geometry": EAST_1},
        {"apn": "R-EAST-2", "county_fips": "99001", "owner_name": "DOE",
         "geometry": EAST_2},
    ])
    assert rows[0]["adjacent_apns"] == ["R-EAST-2"]
    assert rows[1]["adjacent_apns"] == ["R-EAST-1"]
    assert rows[0]["same_owner_adjacent"] == []
    assert rows[1]["same_owner_adjacent"] == []


def test_plat_featurecollection_neighbors_are_reused_not_invented():
    plat = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": EAST_1,
                "properties": {"kind": "subject", "apn": "R-EAST-1", "county_fips": "99001"},
            },
            {
                "type": "Feature",
                "geometry": EAST_2,
                "properties": {"kind": "neighbor", "apn": "R-EAST-2", "county_fips": "99001",
                               "owner": "SMITH"},
            },
        ],
    }
    fact = project_parcel_graph_facts({
        "apn": "R-EAST-1",
        "county_fips": "99001",
        "owner_name": "SMITH",
        "plat": plat,
    })
    assert fact["facts"][INGREDIENT_ADJACENT_APNS]["value"] == ["R-EAST-2"]
    assert fact["neighbors"][0]["source_field"] == "plat"


def test_load_estate_rows_still_projects_without_geometry(tmp_path):
    master = tmp_path / "Wake" / "master"
    master.mkdir(parents=True)
    (master / "wake_master_0001.csv").write_text(
        "candidate_id,owner_name,apn,assessed_value,tax_balance,global_rank\n"
        "c1,SMITH JOHN,0648080648,150000,1500,1\n",
        encoding="utf-8",
    )
    rows = load_estate_rows(tmp_path, ["Wake"])
    assert rows[0]["equity_estimated_pct"] == 99.0
    graph = rows[0]["projected"]["parcel_graph"]
    assert graph["facts"][INGREDIENT_ADJACENT_APNS]["state"] == "UNKNOWN"
    assert graph["gap_class"] == GAP_NORMALIZED_FACT_MISSING


def test_projection_modules_still_refuse_forbidden_lanes():
    root = Path(__file__).resolve().parents[1] / "app" / "projection"
    blob = "".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    lowered = blob.lower()
    for needle in (
        "import supabase",
        "from supabase",
        "import deedsonar",
        "from deedsonar",
        "requests.get",
        "playwright",
        "selenium",
        "import kofile",
        "import fidlar",
        "import govos",
        "import shapely",
        "from shapely",
    ):
        assert needle not in lowered

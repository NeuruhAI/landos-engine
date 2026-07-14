"""
test_load_parcel_geom.py — the ArcGIS feature -> parcel_geom row contract (Job 7).
No DB, no network: pure mapping, pagination params, and the SQL/registry invariants
the geometry_infra_spec handoff contract pins.
"""
import json
import re

from scripts.load_parcel_geom import (COUNTIES, PAGE_SIZE, UPSERT_SQL,
                                      feature_to_row, page_params)

CFG = COUNTIES["stjoseph"]


def _feat(apn="71-06-11-200-007.000-017", fid=42, geom=None):
    if geom is None:
        geom = {"type": "Polygon",
                "coordinates": [[[-86.49, 41.67], [-86.48, 41.67],
                                 [-86.48, 41.68], [-86.49, 41.67]]]}
    return {"properties": {"PARCELSTAT": apn, "FID": fid}, "geometry": geom}


# ---- feature -> row ---------------------------------------------------------------

def test_happy_path_maps_the_contract_columns():
    row, skip = feature_to_row(_feat(), CFG)
    assert skip is None
    assert row["county_fips"] == "18141"
    assert row["parcel_apn"] == "71-06-11-200-007.000-017"
    assert row["source"] == "arcgis:stjoseph_parcel25"
    assert row["source_ref"] == "42"
    assert json.loads(row["geom_json"])["type"] == "Polygon"


def test_blank_apn_is_an_honest_skip_not_a_row():
    for apn in ("", "   ", None):
        f = _feat()
        f["properties"]["PARCELSTAT"] = apn
        row, skip = feature_to_row(f, CFG)
        assert row is None and skip == "no_apn"


def test_missing_or_nonpolygon_geometry_is_an_honest_skip():
    f = _feat(geom=None)
    f["geometry"] = None
    assert feature_to_row(f, CFG) == (None, "bad_geom")
    point = _feat(geom={"type": "Point", "coordinates": [-86.49, 41.67]})
    assert feature_to_row(point, CFG) == (None, "bad_geom")


def test_multipolygon_passes_through():
    mp = {"type": "MultiPolygon",
          "coordinates": [[[[-86.49, 41.67], [-86.48, 41.67],
                            [-86.48, 41.68], [-86.49, 41.67]]]]}
    row, skip = feature_to_row(_feat(geom=mp), CFG)
    assert skip is None and json.loads(row["geom_json"])["type"] == "MultiPolygon"


def test_apn_is_stripped_to_the_canonical_string():
    row, _ = feature_to_row(_feat(apn="  71-06-11-200-007.000-017 "), CFG)
    assert row["parcel_apn"] == "71-06-11-200-007.000-017"


# ---- pagination params ------------------------------------------------------------

def test_page_params_pin_4326_geojson_and_stable_ordering():
    p = page_params("FID", "PARCELSTAT", 4000)
    assert p["outSR"] == "4326" and p["f"] == "geojson"
    assert p["orderByFields"] == "FID"
    assert p["resultOffset"] == "4000"
    assert p["resultRecordCount"] == str(PAGE_SIZE)
    assert "PARCELSTAT" in p["outFields"] and "FID" in p["outFields"]


# ---- SQL + registry invariants (must match migrations/020_geometry_infra.sql) ------

def test_upsert_sql_is_idempotent_multipolygon_4326_inside_centroid():
    s = re.sub(r"\s+", " ", UPSERT_SQL.lower())
    assert "on conflict (county_fips, parcel_apn) do update" in s   # converging re-runs
    assert "st_multi" in s and "4326" in s                          # CRS/type at rest
    assert "st_pointonsurface" in s                                 # inside-guaranteed
    assert "st_makevalid" in s                                      # heal, don't drop
    assert "delete" not in s                                        # AI never deletes


def test_registry_entries_carry_full_provenance():
    for cfg in COUNTIES.values():
        assert re.fullmatch(r"\d{5}", cfg["county_fips"])
        assert cfg["source"].startswith("arcgis:")
        # Two lanes, one contract: a REST entry pins its /query endpoint; a
        # manifest-sealed local entry pins the repo-relative geojson it consumed.
        if "geojson_file" in cfg:
            assert cfg["geojson_file"].endswith(".geojson")
            assert not cfg["geojson_file"].startswith("/")   # repo-relative, portable
        else:
            assert cfg["layer"].startswith("https://") and "/query" in cfg["layer"]
        assert cfg["apn_field"] and cfg["oid_field"]

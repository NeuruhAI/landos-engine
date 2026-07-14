"""
test_flood_sfha_lanes.py — the NFHL SFHA load + community-join cross contracts
(geometry_infra_spec Part 4). No DB, no network: params, mapping, SQL invariants,
and the unarmed-weight posture.
"""
import re

from scripts.load_nfhl_sfha import (COUNTIES, OVERLAY_TYPE, UPSERT_SQL,
                                    feature_to_row, page_params, zone_name)
from scripts.cross_flood_sfha import (STAMP, ZONE_PARCELS, ZONES, county_of)


def _flat(s):
    return re.sub(r"\s+", " ", s.lower())


# ---- load lane ---------------------------------------------------------------------

def test_registry_is_geometry_covered_counties_only():
    # Berrien 26021: no parcel polygons -> no cross to run -> NOT pulled (named gate).
    assert set(COUNTIES) == {"18141", "18091"}
    assert "26021" not in COUNTIES


def test_page_params_pin_sfha_only_4326_geojson_stable_ordering():
    p = page_params("18141C", 2000)
    assert "SFHA_TF='T'" in p["where"] and "DFIRM_ID='18141C'" in p["where"]
    assert p["outSR"] == "4326" and p["f"] == "geojson"
    assert p["orderByFields"] == "FLD_AR_ID"
    assert p["resultOffset"] == "2000"


def test_zone_name_is_the_stable_natural_key_and_carries_the_dfirm():
    n = zone_name("18141C", {"FLD_AR_ID": "18141C_42", "FLD_ZONE": "AE"})
    assert n == "NFHL 18141C 18141C_42 zone AE"
    assert county_of(n) == "18141"          # the cross parses county back out


def test_feature_row_maps_the_idiom_and_skips_honestly():
    feat = {"properties": {"FLD_AR_ID": "X_1", "FLD_ZONE": "A", "ZONE_SUBTY": "",
                           "STATIC_BFE": -9999},
            "geometry": {"type": "Polygon",
                         "coordinates": [[[-86.4, 41.6], [-86.3, 41.6],
                                          [-86.3, 41.7], [-86.4, 41.6]]]}}
    row, skip = feature_to_row(feat, "18141C")
    assert skip is None and row["overlay_type"] == OVERLAY_TYPE
    assert row["minx"] == -86.4 and row["maxy"] == 41.7
    assert "FEMA NFHL" in row["source"] and "DFIRM_ID=18141C" in row["source"]

    feat["geometry"] = {"type": "Point", "coordinates": [-86.4, 41.6]}
    assert feature_to_row(feat, "18141C") == (None, "bad_geom")
    feat["geometry"] = None
    assert feature_to_row(feat, "18141C") == (None, "bad_geom")


def test_upsert_is_idempotent_authoritative_and_never_rearms_weight():
    s = _flat(UPSERT_SQL)
    assert "on conflict (tenant_id, overlay_type, name) do update" in s
    assert "'authoritative'" in s
    assert "values (null" in s                     # tenant-agnostic geography fact
    # UNARMED posture: weight loads 0.0 and a re-pull must NEVER clobber an
    # operator-armed weight — multiplier_weight stays out of the conflict update.
    assert "0.0" in s
    update_clause = s.split("do update set")[1]
    assert "multiplier_weight" not in update_clause
    assert "delete" not in s


# ---- cross lane --------------------------------------------------------------------

def test_zone_parcels_subdivides_then_rides_the_gist_prefilter_at_4326():
    s = _flat(ZONE_PARCELS)
    assert "st_subdivide(st_makevalid" in s        # big floodplains -> cheap pieces
    assert "st_geomfromgeojson(%(geojson)s), 4326" in s
    assert "g.geom && z.piece" in s                # tight per-piece index prefilter
    assert "st_intersects(g.geom, z.piece)" in s
    assert "select distinct g.parcel_apn" in s     # pieces never double-count
    assert "g.county_fips = %(fips)s" in s         # never cross-county


def test_stamp_is_additive_and_guarded_to_new():
    s = _flat(STAMP)
    assert "|| jsonb_build_object('sfha_cross'" in s
    assert "status = 'new'" in s
    assert "set matched_lead_id" not in s          # a community is never one lead


def test_no_sql_ever_deletes():
    for sql in (ZONES, ZONE_PARCELS, STAMP):
        assert "delete" not in sql.lower()


def test_county_of_rejects_garbage_names():
    assert county_of("Ox Creek Pathway (EGLE proxy)") == ""
    assert county_of("NFHL x") == ""

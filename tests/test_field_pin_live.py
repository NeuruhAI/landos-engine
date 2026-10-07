"""
test_field_pin_live.py — FIELD PIN V1 live suite against the ISOLATED database.

Runs ONLY when LANDOS_FIELD_PIN_TEST_DB points at the throwaway PostGIS
container (scripts/field_pin_isolated_db.sh up). It never reads DATABASE_URL
or .env, and it refuses any database not named landos_isolated_test — this
suite can never touch production.

Scene (synthetic county 99001, WGS84 near 41.6 N, 86.7 W; driver on a
north-south road at lon -86.70 heading north):

      L-WEST-1   road   R-EAST-1  R-EAST-2      (R-EAST-1 touches R-EAST-2)
                  |x|   N-AHEAD-1 straddles the road further north

Covers: ray-intersect first (nearest-first past the 3 m buffer), side law
(right/left/ahead), gated nearest fallback ('at', 'unknown'), honest none
(covered_no_parcel vs dark_county), unresolved preservation, retroactive
re-resolution, idempotent replay + byte-identical receipt, duplicate-capture
linkage, plat GeoJSON, owner evidence incl. vintage law, in-CRM check,
Lake Michigan UNWIRED -> OK, adversarial invalid geometry, and NO lead mint.
"""
import json
import os
import uuid

import psycopg
import pytest
from psycopg.rows import dict_row

from app import field_pins as fp

DB = os.environ.get("LANDOS_FIELD_PIN_TEST_DB", "")

pytestmark = pytest.mark.skipif(
    not DB, reason="LANDOS_FIELD_PIN_TEST_DB not set (run scripts/field_pin_isolated_db.sh up)"
)

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
CTY = "99001"          # main synthetic scene
CTY_DARK = "99002"     # starts with no geometry; gains it mid-suite
CTY_BAD = "99003"      # invalid (bowtie) geometry

ORIGIN = (41.6, -86.7)


def _poly(lon_min, lat_min, lon_max, lat_max):
    return (f"MULTIPOLYGON((({lon_min} {lat_min},{lon_max} {lat_min},"
            f"{lon_max} {lat_max},{lon_min} {lat_max},{lon_min} {lat_min})))")


PARCELS = [
    # (apn, wkt) — all ~33 m from the road at nearest edge
    ("R-EAST-1", _poly(-86.6996, 41.5995, -86.6990, 41.6005)),
    ("R-EAST-2", _poly(-86.6990, 41.5995, -86.6984, 41.6005)),   # touches R-EAST-1
    ("L-WEST-1", _poly(-86.7010, 41.5995, -86.7004, 41.6005)),
    ("N-AHEAD-1", _poly(-86.7003, 41.6004, -86.6997, 41.6012)),
]

OWNERS = [
    ("R-EAST-1", "SMITH FAMILY TRUST", None, "1001 TEST RD"),
    ("R-EAST-2", "STATE OF INDIANA DNR", "govt", "1003 TEST RD"),
    # L-WEST-1: no owner row at all -> not_on_file
    ("N-AHEAD-1", "   ", None, None),                            # blank -> not_on_file
]


@pytest.fixture(scope="module")
def conn():
    c = psycopg.connect(DB, row_factory=dict_row)
    if c.info.dbname != "landos_isolated_test":
        c.close()
        pytest.fail("refusing: LANDOS_FIELD_PIN_TEST_DB is not the isolated test database")
    yield c
    c.close()


@pytest.fixture(scope="module", autouse=True)
def scene(conn):
    with conn.cursor() as cur:
        cur.execute("delete from landos.field_pins")
        cur.execute("delete from landos.shoreline_geom")
        cur.execute("delete from landos.parcel_geom where county_fips in (%s,%s,%s)", (CTY, CTY_DARK, CTY_BAD))
        cur.execute("delete from landos.parcel_owner where county_fips in (%s,%s,%s)", (CTY, CTY_DARK, CTY_BAD))
        cur.execute("delete from public.acquisition_leads where county_fips in (%s,%s,%s)", (CTY, CTY_DARK, CTY_BAD))
        for apn, wkt in PARCELS:
            cur.execute(
                "insert into landos.parcel_geom (county_fips, parcel_apn, geom, centroid, source) values "
                "(%s, %s, st_setsrid(st_geomfromtext(%s),4326), "
                " st_pointonsurface(st_setsrid(st_geomfromtext(%s),4326)), 'test:fieldpin_scene')",
                (CTY, apn, wkt, wkt))
        for apn, owner, flag, situs in OWNERS:
            cur.execute(
                "insert into landos.parcel_owner (county_fips, parcel_apn, owner_name, vintage_flag, situs_address, source) "
                "values (%s,%s,%s,%s,%s,'test:fieldpin_scene')", (CTY, apn, owner, flag, situs))
        # invalid bowtie geometry in CTY_BAD (self-intersecting ring) — adversarial
        cur.execute(
            "insert into landos.parcel_geom (county_fips, parcel_apn, geom, source) values "
            "(%s,'BOWTIE-1', st_setsrid(st_geomfromtext("
            "'MULTIPOLYGON(((-86.6996 41.7995,-86.6990 41.8005,-86.6990 41.7995,-86.6996 41.8005,-86.6996 41.7995)))'"
            "),4326), 'test:adversarial')", (CTY_BAD,))
        # the in-CRM fixture: R-EAST-1 is already on the acquisition board
        cur.execute(
            "insert into public.acquisition_leads (tenant_id, county_fips, parcel_apn, stage, card_state, status) "
            "values (%s,%s,'R-EAST-1','due_diligence','active','new')", (TENANT, CTY))
    conn.commit()
    yield


def payload(**over):
    p = {
        "tenant_id": TENANT, "device_id": "phone-1",
        "lat": ORIGIN[0], "lon": ORIGIN[1], "observed_at": "2026-07-22T15:00:00Z",
        "heading_deg": 0.0, "heading_source": "gps_course", "side": "right",
        "category": "distress", "spoken_text": "collapsed barn on the right",
        "accuracy_m": 8.0, "idempotency_key": None,
    }
    p.update(over)
    return p


def capture(conn, **over):
    out = fp.capture_pin(conn, payload(**over))
    conn.commit()
    return out


def _crm_count(conn):
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from public.acquisition_leads")
        return cur.fetchone()["n"]


# ── the resolution ladder, live ──────────────────────────────────────────────
def test_ray_intersect_right_side_nearest_first(conn):
    before = _crm_count(conn)
    out = capture(conn, idempotency_key="ray-right-1")
    r = out["resolution"]
    assert out["ok"] and r["resolution_method"] == "ray_intersect"
    assert r["apn"] == "R-EAST-1"                     # nearest of the two east parcels
    assert r["county_fips"] == CTY
    assert float(r["confidence"]) == 0.90
    # owner evidence: name + flag + situs address of record
    assert r["owner"] == "SMITH FAMILY TRUST" and r["owner_flag"] == "private"
    assert r["address"] == "1001 TEST RD"
    # already on the board -> in_crm present
    assert r["in_crm"]["present"] is True and r["in_crm"]["stage"] == "due_diligence"
    # plat: subject + the touching neighbor, as GeoJSON
    plat = out["plat"]
    assert plat["type"] == "FeatureCollection"
    kinds = {f["properties"].get("kind") for f in plat["features"]}
    assert kinds == {"subject", "neighbor"}
    subject = next(f for f in plat["features"] if f["properties"]["kind"] == "subject")
    assert subject["properties"]["apn"] == "R-EAST-1"
    assert subject["geometry"]["type"] in ("Polygon", "MultiPolygon")
    neighbors = {f["properties"]["apn"] for f in plat["features"] if f["properties"]["kind"] == "neighbor"}
    assert neighbors == {"R-EAST-2"}                  # ST_Touches, honest owner join
    nbr = next(f for f in plat["features"] if f["properties"]["kind"] == "neighbor")
    assert nbr["properties"]["owner_flag"] == "govt"  # vintage law surfaces the flag
    # capture minted NOTHING
    assert _crm_count(conn) == before


def test_left_side_hits_west_parcel(conn):
    out = capture(conn, side="left", idempotency_key="ray-left-1")
    r = out["resolution"]
    assert r["resolution_method"] == "ray_intersect" and r["apn"] == "L-WEST-1"
    assert r["owner"] == "not on file" and r["owner_flag"] == "not_on_file"
    assert r["address"] is None                        # honest-null situs
    assert r["in_crm"] == {"present": False}


def test_ahead_hits_north_parcel_blank_owner_honest(conn):
    out = capture(conn, side="ahead", idempotency_key="ray-ahead-1")
    r = out["resolution"]
    assert r["resolution_method"] == "ray_intersect" and r["apn"] == "N-AHEAD-1"
    assert r["owner"] == "not on file" and r["owner_flag"] == "not_on_file"  # blank name


def test_at_side_uses_gated_nearest_fallback(conn):
    out = capture(conn, side="at", idempotency_key="at-1")
    r = out["resolution"]
    assert r["resolution_method"] == "nearest_parcel"
    assert float(r["confidence"]) == 0.50
    assert r["apn"] in ("R-EAST-1", "L-WEST-1")        # ~33 m either side, within 60 m


def test_unknown_side_unknown_heading_nearest_fallback(conn):
    out = capture(conn, side="unknown", heading_source="unknown", heading_deg=None,
                  idempotency_key="unknown-1")
    assert out["resolution"]["resolution_method"] == "nearest_parcel"


def test_ray_miss_in_covered_area_is_honest_none(conn):
    # 1.1 km north of the scene: geometry within 2 km, but the east ray misses
    # everything and the projection is >60 m from any parcel.
    out = capture(conn, lat=41.61, idempotency_key="miss-1")
    r = out["resolution"]
    assert r["resolution_method"] == "none" and r["apn"] is None
    assert r["coverage"] == "covered_no_parcel"
    assert out["plat"] is None                          # no subject parcel to plat


def test_dark_county_pin_preserved_unresolved(conn):
    out = capture(conn, lat=45.0, lon=-85.0, idempotency_key="dark-1")
    r = out["resolution"]
    assert r["resolution_method"] == "none" and r["coverage"] == "dark_county"
    with conn.cursor() as cur:
        cur.execute("select resolution_method, resolved_apn from landos.field_pins where pin_id = %s::uuid",
                    (out["pin_id"],))
        row = cur.fetchone()
    assert row["resolution_method"] == "none" and row["resolved_apn"] is None  # stored, not dropped


# ── retroactive re-resolution ────────────────────────────────────────────────
def test_later_reresolution_when_geometry_lands(conn):
    origin = (43.0, -86.3)
    out = capture(conn, lat=origin[0], lon=origin[1], idempotency_key="retro-1")
    assert out["resolution"]["resolution_method"] == "none"
    assert out["resolution"]["coverage"] == "dark_county"
    pin_id = out["pin_id"]

    # county geometry lands AFTER the capture (33 m east of that origin)
    with conn.cursor() as cur:
        cur.execute(
            "insert into landos.parcel_geom (county_fips, parcel_apn, geom, source) values "
            "(%s,'RETRO-1', st_setsrid(st_geomfromtext(%s),4326), 'test:retro_load')",
            (CTY_DARK, _poly(-86.2996, 42.9995, -86.2990, 43.0005)))
    conn.commit()

    out2 = fp.reresolve_pin(conn, pin_id)
    conn.commit()
    assert out2["ok"] and out2["pin_id"] == pin_id
    assert out2["resolution"]["resolution_method"] == "ray_intersect"
    assert out2["resolution"]["apn"] == "RETRO-1"
    assert out2["plat"]["type"] == "FeatureCollection"


def test_reresolve_unknown_pin_honest_404(conn):
    out = fp.reresolve_pin(conn, str(uuid.uuid4()))
    conn.commit()
    assert out == {"ok": False, "error": "pin_not_found"}


# ── idempotency + duplicate handling ─────────────────────────────────────────
def test_idempotent_replay_same_pin_same_receipt(conn):
    first = capture(conn, idempotency_key="idem-1", device_id="phone-idem")
    replay = capture(conn, idempotency_key="idem-1", device_id="phone-idem")
    assert replay["replay"] is True and first["replay"] is False
    assert replay["pin_id"] == first["pin_id"]
    assert replay["receipt"] == first["receipt"]        # byte-identical
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from landos.field_pins where idempotency_key='idem-1'")
        assert cur.fetchone()["n"] == 1                 # exactly one row, ever


def test_duplicate_capture_stored_and_linked(conn):
    a = capture(conn, idempotency_key="dupe-a", device_id="phone-dupe",
                observed_at="2026-07-22T16:00:00Z")
    b = capture(conn, idempotency_key="dupe-b", device_id="phone-dupe",
                observed_at="2026-07-22T16:01:00Z", lat=41.60001)  # ~1 m away, 60 s later
    assert b["pin_id"] != a["pin_id"]                   # stored, never dropped
    assert b["duplicate_of_pin_id"] == a["pin_id"]      # linked to the original
    c = capture(conn, idempotency_key="dupe-c", device_id="phone-dupe",
                observed_at="2026-07-22T16:10:00Z")     # outside the 120 s window
    assert c["duplicate_of_pin_id"] is None


# ── adversarial geometry ─────────────────────────────────────────────────────
def test_invalid_parcel_geometry_never_discards_the_pin(conn):
    out = capture(conn, lat=41.8, lon=-86.7, idempotency_key="bowtie-1")
    # Whatever PostGIS makes of the bowtie, the capture is stored and honest:
    with conn.cursor() as cur:
        cur.execute("select pin_id from landos.field_pins where idempotency_key='bowtie-1'")
        assert cur.fetchone() is not None
    assert out["ok"] is True
    r = out["resolution"]
    assert (r["resolution_method"] in ("ray_intersect", "nearest_parcel", "none")
            or "resolution_error" in r)


def test_validation_rejects_before_touching_the_db(conn):
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from landos.field_pins")
        before = cur.fetchone()["n"]
    for bad in ({"lat": 95.0}, {"side": "behind"}, {"observed_at": "not-a-time"}):
        with pytest.raises(fp.FieldPinValidationError):
            fp.capture_pin(conn, payload(**bad))
        conn.rollback()
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from landos.field_pins")
        assert cur.fetchone()["n"] == before            # nothing stored


def test_plat_unknown_parcel_is_honest_error(conn):
    with conn.cursor() as cur:
        cur.execute("select landos.fn_parcel_plat(%s, 'NO-SUCH-APN') as plat", (CTY,))
        plat = cur.fetchone()["plat"]
    assert plat["type"] == "Error" and plat["error"] == "parcel_not_found"


# ── Lake Michigan: honestly UNWIRED, then honestly OK ────────────────────────
def test_lake_unwired_without_authoritative_geometry(conn):
    out = capture(conn, idempotency_key="lake-unwired-1")
    assert out["lake_michigan"]["status"] == "UNWIRED"
    # a NON-authoritative trace must not wire the lane
    with conn.cursor() as cur:
        cur.execute(
            "insert into landos.shoreline_geom (waterbody, geom, source, authoritative) values "
            "('lake_michigan', st_multi(st_setsrid(st_geomfromtext("
            "'LINESTRING(-86.71 41.55, -86.71 41.65)'),4326)), 'test:unreviewed_trace', false)")
    conn.commit()
    out2 = fp.get_pin(conn, out["pin_id"])
    conn.commit()
    assert out2["lake_michigan"]["status"] == "UNWIRED"


def test_lake_ok_with_authoritative_geometry_parcel_and_origin_bases(conn):
    with conn.cursor() as cur:
        cur.execute(
            "insert into landos.shoreline_geom (waterbody, geom, source, authoritative) values "
            "('lake_michigan', st_multi(st_setsrid(st_geomfromtext("
            "'LINESTRING(-86.71 41.55, -86.71 41.65)'),4326)), 'test:authoritative_shoreline', true)")
    conn.commit()

    resolved = capture(conn, idempotency_key="lake-ok-1")
    lake = resolved["lake_michigan"]
    assert lake["status"] == "OK" and lake["basis"] == "resolved_parcel"
    assert lake["source"] == "test:authoritative_shoreline"
    assert 700 <= float(lake["distance_m"]) <= 1000     # ~865 m at this latitude
    assert lake["corridor"] == "corridor_500m_1mi"

    unresolved = capture(conn, lat=45.0, lon=-85.0, idempotency_key="lake-ok-2")
    lake2 = unresolved["lake_michigan"]
    assert lake2["status"] == "OK" and lake2["basis"] == "pin_origin"
    assert lake2["corridor"] == "outside_corridor"


# ── the hard NOs ─────────────────────────────────────────────────────────────
def test_no_lead_minting_across_the_whole_suite(conn):
    # the ONLY acquisition_leads row in the scene counties is the fixture
    with conn.cursor() as cur:
        cur.execute("select count(*) as n from public.acquisition_leads where county_fips in (%s,%s,%s)",
                    (CTY, CTY_DARK, CTY_BAD))
        assert cur.fetchone()["n"] == 1
        cur.execute("select count(*) as n from landos.owner_leads where county_fips in (%s,%s,%s)",
                    (CTY, CTY_DARK, CTY_BAD))
        assert cur.fetchone()["n"] == 0


def test_rls_enabled_on_every_field_pin_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            "select relname, relrowsecurity from pg_class c join pg_namespace n on n.oid=c.relnamespace "
            "where n.nspname='landos' and relname in ('field_pins','shoreline_geom','parcel_geom','parcel_owner')")
        rows = {r["relname"]: r["relrowsecurity"] for r in cur.fetchall()}
    assert rows == {"field_pins": True, "shoreline_geom": True, "parcel_geom": True, "parcel_owner": True}

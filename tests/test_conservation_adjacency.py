"""
test_conservation_adjacency.py — parcels bordering protected ground (dispo modifier).

Covers the PURE proximity gate (within/adjacent/none, buffer math), protected-type +
provenance filtering, confidence, census gap (naming the non-protected types present),
and adapter disarm-vs-arm. One DB-backed case proves the LIVE census reports the gap
(overlay engine live, no protected layer) — skips when unreachable.
"""
import pytest

from app.discovery import conservation_adjacency as ca
from app.discovery.conservation_adjacency import ConservationAdjacencyAdapter

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def _ov(overlay_type, name="Preserve", prov="authoritative", buffer_m=200,
        mult=1.15, minx=-86.50, miny=42.10, maxx=-86.48, maxy=42.12):
    return {"overlay_type": overlay_type, "name": name, "provenance_status": prov,
            "buffer_m": buffer_m, "multiplier_weight": mult,
            "minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy}


def _p(apn, lat, lng, county="26021"):
    return {"county_fips": county, "parcel_apn": apn, "lat": lat, "lng": lng}


# ---- proximity -------------------------------------------------------------

def test_point_inside_bbox_is_within():
    assert ca.proximity(_p("A", 42.11, -86.49), _ov("conservation")) == "within"


def test_point_in_buffer_is_adjacent():
    # just east of maxx (-86.48) by ~150m, buffer 200m -> adjacent
    assert ca.proximity(_p("A", 42.11, -86.4785), _ov("conservation", buffer_m=200)) == "adjacent"


def test_point_far_is_none():
    assert ca.proximity(_p("A", 42.50, -86.00), _ov("conservation", buffer_m=200)) is None


def test_missing_geometry_is_none():
    assert ca.proximity(_p("A", None, None), _ov("conservation")) is None


# ---- type + provenance filtering -------------------------------------------

def test_non_protected_type_never_matches():
    # a brownfield / opportunity_zone overlap is NOT conservation adjacency
    assert ca.evaluate(_p("A", 42.11, -86.49), [_ov("brownfield")]) is None
    assert ca.evaluate(_p("A", 42.11, -86.49), [_ov("opportunity_zone")]) is None


def test_stub_provenance_excluded():
    assert ca.evaluate(_p("A", 42.11, -86.49), [_ov("conservation", prov="stub")]) is None


def test_protected_within_emits_with_multiplier():
    ev = ca.evaluate(_p("A", 42.11, -86.49), [_ov("land_trust", mult=1.2)])
    assert ev["proximity"] == "within" and ev["dispo_multiplier"] == 1.2
    assert ev["overlay_type"] == "land_trust"


# ---- census ----------------------------------------------------------------

def test_census_reports_gap_with_present_types():
    # live-like: overlays present but none protected
    overlays = [_ov("brownfield"), _ov("opportunity_zone")]
    c = ca.census([_p("A", 42.11, -86.49)], overlays)
    assert c["covered"] is False and c["protected_overlays"] == 0
    assert "no protected/land-trust overlay" in c["gap"]
    assert "brownfield" in c["gap"] and "PAD-US" in c["first_task"]


def test_census_covered_with_protected_layer():
    c = ca.census([_p("A", 42.11, -86.49), _p("B", 42.50, -86.0)], [_ov("preserve")])
    assert c["covered"] is True and c["protected_overlays"] == 1 and c["matches"] == 1


# ---- adapter ---------------------------------------------------------------

def test_adapter_disarms_without_protected_layer():
    a = ConservationAdjacencyAdapter(TENANT, parcels=[_p("A", 42.11, -86.49)],
                                     overlays=[_ov("brownfield")])
    assert a.fetch() == [] and a.last_census["covered"] is False


def test_adapter_arms_and_shapes_signal():
    parcels = [_p("HIT", 42.11, -86.49), _p("FAR", 42.9, -85.0)]
    a = ConservationAdjacencyAdapter(TENANT, parcels=parcels, overlays=[_ov("conservation", name="Warren Woods")])
    sigs = a.fetch()
    assert len(sigs) == 1 and a.non_matches == 1
    s = sigs[0]
    assert s["parcel_apn"] == "HIT" and s["source_ref"] == "26021:HIT:conservation_adjacency"
    assert s["raw_payload"]["type"] == "conservation_adjacency"
    assert s["raw_payload"]["overlay_name"] == "Warren Woods" and s["raw_payload"]["proximity"] == "within"


# ---- DB-backed live gap ----------------------------------------------------

def test_live_census_reports_gap_today():
    psycopg = pytest.importorskip("psycopg")
    from app.config import DATABASE_URL
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"database unreachable: {e}")
    try:
        with conn.cursor() as cur:
            rep = ca.census_live(cur, TENANT)
        assert rep["overlays_total"] >= 1, "expected the overlay engine to hold rows"
        assert rep["protected_overlays"] == 0 and rep["covered"] is False
    finally:
        conn.rollback()
        conn.close()

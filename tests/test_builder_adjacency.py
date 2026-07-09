"""
test_builder_adjacency.py — match active builders to teardown/land candidates.

Covers the PURE clustering (min-permits threshold, new-build filter), the geographic
join, confidence, census gap, and adapter disarm-vs-arm. One DB-backed case proves
the LIVE census reports the gap (no permit source) — skips when unreachable.
"""
import pytest

from app.discovery import builder_adjacency as ba
from app.discovery.builder_adjacency import BuilderAdjacencyAdapter

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def _permit(builder, zip_, permit_type="new", issued_year=2025):
    return {"builder": builder, "zip": zip_, "permit_type": permit_type, "issued_year": issued_year}


def _cand(apn, zip_, county="26021"):
    return {"county_fips": county, "parcel_apn": apn, "zip": zip_}


# ---- clustering ------------------------------------------------------------

def test_active_builders_thresholds_and_normalizes():
    permits = [_permit("Toll Bros", "49117"), _permit("toll  bros", "49117"),  # same builder, 2
               _permit("OneOff LLC", "49117")]                                  # 1 -> not active
    bz = ba.active_builders(permits, min_permits=2)
    assert bz["49117"] == [{"builder": "TOLL BROS", "permits": 2}]


def test_non_new_build_permits_ignored():
    permits = [_permit("ReRoof Co", "49117", permit_type="reroof"),
               _permit("ReRoof Co", "49117", permit_type="reroof")]
    assert ba.active_builders(permits, min_permits=2) == {}


def test_since_year_filter():
    permits = [_permit("Old Co", "49117", issued_year=2019),
               _permit("Old Co", "49117", issued_year=2020)]
    assert ba.active_builders(permits, min_permits=2, since_year=2024) == {}


# ---- join ------------------------------------------------------------------

def test_match_joins_candidate_to_builder_in_same_zip():
    bz = {"49117": [{"builder": "TOLL BROS", "permits": 3}, {"builder": "PULTE", "permits": 2}]}
    evs = ba.match([_cand("A", "49117"), _cand("B", "48601")], bz)
    assert len(evs) == 1 and evs[0]["parcel_apn"] == "A"
    assert evs[0]["top_builder"] == "TOLL BROS" and evs[0]["cluster_size"] == 2


def test_confidence_grows_with_cluster_and_volume():
    assert ba._confidence(1, 1) < ba._confidence(3, 5)


# ---- census ----------------------------------------------------------------

def test_census_reports_no_permit_gap():
    c = ba.census([], [_cand("A", "49117")])
    assert c["covered"] is False and "no permit records ingested" in c["gap"]
    assert "Census BPS" in c["first_task"]


def test_census_covered_when_permits_and_candidates_present():
    permits = [_permit("Toll Bros", "49117"), _permit("Toll Bros", "49117")]
    c = ba.census(permits, [_cand("A", "49117"), _cand("B", "48601")])
    assert c["covered"] is True and c["matches"] == 1 and c["active_builders"] == 1


# ---- adapter ---------------------------------------------------------------

def test_adapter_disarms_without_permits():
    a = BuilderAdjacencyAdapter(TENANT, permits=[], candidates=[_cand("A", "49117")])
    assert a.fetch() == [] and a.last_census["covered"] is False


def test_adapter_arms_and_shapes_signal():
    permits = [_permit("Toll Bros", "49117"), _permit("Toll Bros", "49117"), _permit("Toll Bros", "49117"),
               _permit("Pulte", "49117"), _permit("Pulte", "49117")]
    cands = [_cand("HIT", "49117"), _cand("MISS", "48601")]
    a = BuilderAdjacencyAdapter(TENANT, permits=permits, candidates=cands)
    sigs = a.fetch()
    assert len(sigs) == 1 and a.non_matches == 1
    s = sigs[0]
    assert s["parcel_apn"] == "HIT" and s["source_ref"] == "26021:HIT:builder_adjacency"
    assert s["raw_payload"]["type"] == "builder_adjacency"
    assert s["raw_payload"]["cluster_size"] == 2 and s["raw_payload"]["top_builder"] == "TOLL BROS"


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
            rep = ba.census_live(cur, TENANT)
        assert rep["permit_records"] == 0 and rep["covered"] is False
        assert "no permit records ingested" in rep["gap"]
    finally:
        conn.rollback()
        conn.close()

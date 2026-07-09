"""
test_aging_owner_luxury.py — pre-downsizing pipeline (65+ × long tenure × luxury × dated).

Covers the senior 65+-proxy parse (honest None on absence), the full gate, confidence,
census gap-naming, and the adapter disarm-vs-arm. One DB-backed case proves the LIVE
census reports the gap (senior exemption not captured) — skips when unreachable.
"""
import pytest

from app.discovery import aging_owner_luxury as ag
from app.discovery.aging_owner_luxury import AgingOwnerLuxuryAdapter

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
LUX = {"49085", "49117"}


def _p(county, apn, zip_, senior=None, year_built=None, tenure=None):
    return {"county_fips": county, "parcel_apn": apn, "zip": zip_,
            "senior_exemption": senior, "year_built": year_built, "tenure_years": tenure}


# ---- senior proxy parse ----------------------------------------------------

def test_senior_parse_absence_is_none():
    assert ag._senior({"senior_exemption": None}) is None    # not captured -> None
    assert ag._senior({"senior_exemption": ""}) is None
    assert ag._senior({"senior_exemption": "true"}) is True
    assert ag._senior({"senior_exemption": True}) is True
    assert ag._senior({"senior_exemption": "no"}) is False


# ---- gate ------------------------------------------------------------------

def test_full_candidate_emits():
    ev = ag.evaluate(_p("26049", "P1", "49085", senior=True, year_built=1955, tenure=32), LUX)
    assert ev["senior"] is True and ev["tenure_years"] == 32 and ev["zip"] == "49085"


def test_missing_senior_suppresses_silently():
    # today's shape: no senior exemption captured -> None -> not a candidate (not a fake reject)
    assert ag.evaluate(_p("26049", "P2", "49085", senior=None, year_built=1955, tenure=32), LUX) is None


def test_short_tenure_new_build_or_nonluxury_suppress():
    assert ag.evaluate(_p("26049", "A", "49085", True, 1955, 10), LUX) is None   # short hold
    assert ag.evaluate(_p("26049", "B", "49085", True, 2010, 32), LUX) is None   # new build
    assert ag.evaluate(_p("26049", "C", "48601", True, 1955, 32), LUX) is None   # not luxury


# ---- confidence ------------------------------------------------------------

def test_confidence_scales_with_tenure_and_age():
    assert ag._confidence(45, 1950) == 0.92            # 0.80 + 0.08 (>=40yr) + 0.04 (<=1960)
    assert ag._confidence(26, 1990) == 0.80            # base only
    assert ag._confidence(45, 1950) > ag._confidence(26, 1990)


# ---- census ----------------------------------------------------------------

def test_census_names_senior_gap_today():
    recs = [_p("26049", "A", "49085", senior=None, year_built=None, tenure=None)]
    c = ag.census(recs, LUX)["26049"]
    assert c["covered"] is False and c["senior_exemption_captured"] == 0
    assert "senior/homestead exemption" in c["gap"] and "ReportAll" in c["first_task"]


def test_census_covered_when_senior_captured_and_luxury_defined():
    recs = [_p("26049", "A", "49085", senior=True, year_built=1955, tenure=32),
            _p("26049", "B", "49085", senior=False, year_built=1955, tenure=32)]  # captured, not senior
    c = ag.census(recs, LUX)["26049"]
    assert c["covered"] is True and c["senior_exemption_captured"] == 2 and c["candidates"] == 1


# ---- adapter ---------------------------------------------------------------

def test_adapter_disarms_today_emits_nothing():
    recs = [_p("26049", "A", "49085", senior=None, year_built=None, tenure=None)]
    a = AgingOwnerLuxuryAdapter(TENANT, parcels=recs, luxury_zips=LUX)
    assert a.fetch() == [] and a.last_census["26049"]["covered"] is False


def test_adapter_arms_and_shapes_signal_when_captured():
    recs = [_p("26049", "HIT", "49085", senior=True, year_built=1950, tenure=40),
            _p("26049", "NO", "49085", senior=False, year_built=1950, tenure=40)]
    a = AgingOwnerLuxuryAdapter(TENANT, parcels=recs, luxury_zips=LUX)
    sigs = a.fetch()
    assert len(sigs) == 1 and a.non_matches == 1
    s = sigs[0]
    assert s["parcel_apn"] == "HIT" and s["source_ref"] == "26049:HIT:aging_owner_luxury"
    assert s["raw_payload"]["type"] == "aging_owner_luxury" and s["raw_payload"]["senior"] is True


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
            rep = ag.census_live(cur, TENANT)      # luxury set from config
        assert rep, "expected parcels on the spine"
        assert all(not c["covered"] for c in rep.values())
        assert all(c["senior_exemption_captured"] == 0 for c in rep.values())
    finally:
        conn.rollback()
        conn.close()

"""
test_lot_value_arbitrage.py — Bryan's luxury teardown play (assessor math).

Covers the PURE land ratio (guarded against missing/corrupt assessor amounts), the
full candidate gate (ratio threshold + luxury-zip + old + long-hold, with honest
silence on any missing capture), confidence tiers, the per-county census that names
WHICH gap is open, and the adapter's disarm-vs-arm behaviour. One DB-backed case
proves the LIVE census reports the three-part gap (no land/total split, no
year_built, no tenure captured) — it skips when the spine is unreachable and never
writes.
"""
import pytest

from app.discovery import lot_value_arbitrage as lva
from app.discovery.lot_value_arbitrage import LotValueArbitrageAdapter

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
LUX = {"48009", "48301"}         # a synthetic luxury-zip set (Birmingham / Bloomfield MI)


def _p(county, apn, zip_, land, total, year_built=None, tenure=None):
    return {"county_fips": county, "parcel_apn": apn, "zip": zip_,
            "land_assessed": land, "total_assessed": total,
            "year_built": year_built, "tenure_years": tenure}


# ---- pure land ratio -------------------------------------------------------

def test_land_ratio_basic_and_formats():
    assert lva.land_ratio(420000, 480000) == 0.875
    assert lva.land_ratio("$420,000", "480,000") == 0.875     # formatted amounts parse
    assert lva.land_ratio(240000, 480000) == 0.5


def test_land_ratio_guards_missing_and_corrupt():
    assert lva.land_ratio(None, 480000) is None               # not captured -> None, never 0
    assert lva.land_ratio(420000, None) is None
    assert lva.land_ratio(420000, 0) is None                  # no divide-by-zero fake
    assert lva.land_ratio(600000, 480000) is None             # land>total = corrupt row
    assert lva.land_ratio("TBA", 480000) is None              # unparseable -> None


# ---- candidate gate: emit only on a genuine teardown -----------------------

def test_full_teardown_candidate_emits():
    ev = lva.evaluate(_p("26125", "P1", "48009", 420000, 480000, year_built=1948, tenure=31), LUX)
    assert ev["land_ratio"] == 0.875 and ev["zip"] == "48009" and ev["tier"] == "high"


def test_zip_outside_luxury_set_suppresses():
    assert lva.evaluate(_p("26125", "P2", "48601", 420000, 480000, 1948, 31), LUX) is None


def test_ratio_below_threshold_suppresses():
    # land is a minority of value -> the structure carries the worth, not a teardown
    assert lva.evaluate(_p("26125", "P3", "48009", 120000, 480000, 1948, 31), LUX) is None


def test_new_structure_suppresses():
    assert lva.evaluate(_p("26125", "P4", "48009", 420000, 480000, year_built=2015, tenure=31), LUX) is None


def test_short_tenure_suppresses():
    assert lva.evaluate(_p("26125", "P5", "48009", 420000, 480000, year_built=1948, tenure=3), LUX) is None


def test_missing_capture_is_silent_never_a_false_candidate():
    # no year_built and no tenure captured (today's shape) -> honest silence, not a hit
    assert lva.evaluate(_p("26125", "P6", "48009", 420000, 480000, year_built=None, tenure=None), LUX) is None


# ---- confidence ------------------------------------------------------------

def test_confidence_scales_with_ratio_and_age_and_hold():
    assert lva._confidence(0.95, 1948, 31) == 0.95            # near-pure land + old + long hold (capped)
    assert lva._confidence(0.80, 1975, 20) == 0.79            # 0.75 + 0.04 (old-ish), no hold boost
    assert lva._confidence(0.55, None, None) == 0.60          # med band, no boosts
    assert lva._confidence(0.80, 1975, 20) > lva._confidence(0.55, None, None)


# ---- census: names WHICH gap is open ---------------------------------------

def test_census_reports_three_part_gap_when_split_absent():
    # today's live shape: SEV present (total) but no land split, no year_built, no tenure
    recs = [_p("26049", "A", "49085", None, 90000, year_built=None, tenure=None)]
    c = lva.census(recs, LUX)["26049"]
    assert c["covered"] is False and c["valuation_split_captured"] == 0
    assert "land/total assessed split NOT captured" in c["gap"]
    assert "year_built NOT captured" in c["gap"] and "tenure NOT captured" in c["gap"]
    assert "ReportAll" in c["first_task"]


def test_census_reports_luxury_undefined_gap():
    recs = [_p("26125", "A", "48009", 420000, 480000, 1948, 31)]
    c = lva.census(recs, set())["26125"]              # no luxury set supplied
    assert c["covered"] is False and "luxury-zip set undefined" in c["gap"]


def test_census_covered_and_counts_candidates_when_all_present():
    recs = [
        _p("26125", "HIT", "48009", 420000, 480000, 1948, 31),
        _p("26125", "NEW", "48009", 420000, 480000, 2015, 31),   # too new
        _p("26125", "CHEAPLAND", "48301", 100000, 480000, 1948, 31),  # ratio too low
    ]
    c = lva.census(recs, LUX)["26125"]
    assert c["covered"] is True and c["valuation_split_captured"] == 3
    assert c["in_luxury_zip"] == 3 and c["candidates"] == 1


# ---- adapter: disarm vs arm ------------------------------------------------

def test_adapter_disarms_when_split_absent_emits_nothing():
    recs = [_p("26049", "A", "49085", None, 90000, None, None)]     # no land split (today)
    a = LotValueArbitrageAdapter(TENANT, parcels=recs, luxury_zips=LUX)
    assert a.fetch() == []                            # disarmed, never fabricated
    assert a.last_census["26049"]["covered"] is False


def test_adapter_arms_covered_county_and_shapes_signal():
    recs = [
        _p("26125", "HIT", "48009", 440000, 480000, 1950, 28),
        _p("26125", "NODROP", "48009", 200000, 480000, 1950, 28),   # ratio below threshold
    ]
    a = LotValueArbitrageAdapter(TENANT, parcels=recs, luxury_zips=LUX)
    sigs = a.fetch()
    assert len(sigs) == 1 and a.non_matches == 1      # the sub-threshold parcel is logged
    s = sigs[0]
    assert s["parcel_apn"] == "HIT"
    assert s["source_ref"] == "26125:HIT:lot_value_arbitrage"
    assert s["raw_payload"]["type"] == "lot_value_arbitrage"
    assert s["raw_payload"]["land_ratio"] == round(440000 / 480000, 4)
    assert s["raw_payload"]["zip"] == "48009" and s["raw_payload"]["tier"] == "high"


def test_adapter_pins_single_county():
    recs = [_p("26125", "A", "48009", 440000, 480000, 1950, 28),
            _p("26099", "B", "48009", 440000, 480000, 1950, 28)]
    a = LotValueArbitrageAdapter(TENANT, parcels=recs, luxury_zips=LUX, county_fips="26125")
    sigs = a.fetch()
    assert len(sigs) == 1 and sigs[0]["parcel_apn"] == "A"
    assert set(a.last_census) == {"26125"}


# ---- DB-backed: the LIVE census must report the gap (no fake signal) --------

def test_live_census_reports_gap_today():
    psycopg = pytest.importorskip("psycopg")
    from app.config import DATABASE_URL
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"database unreachable: {e}")
    try:
        with conn.cursor() as cur:
            # the luxury-zip set now defaults to the CRM config (option_sets/mig 066),
            # so the zip BASIS is wired — but the land/total split still isn't captured,
            # so every county is an honest gap and 0 candidates surface.
            zips = lva.load_luxury_zips(cur, TENANT)
            rep = lva.census_live(cur, TENANT)          # defaults to the config set
        assert zips, "expected the luxury_zip set to be seeded (mig 066)"
        assert rep, "expected parcels on the spine"
        assert all(not c["covered"] for c in rep.values())
        assert all(c["valuation_split_captured"] == 0 for c in rep.values())
        assert sum(c["candidates"] for c in rep.values()) == 0
    finally:
        conn.rollback()
        conn.close()

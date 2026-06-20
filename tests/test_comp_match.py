"""
test_comp_match.py — COMP-MATCHER v1: arm's-length filter, band widening, conservative
NULL-on-sparse, FMV + arbitrage mapping, grounded-0 vs NULL.
"""
from app.comp_match import match_subject, is_arms_length


def _c(ppa, ac, price=50000, date="2024-06-01"):
    return {"parcel_number": f"71-{ppa}-{ac}", "conveyance_date": date, "sale_price": str(price),
            "acreage": str(ac), "price_per_acre": str(ppa), "vacant_land_flag": "Y",
            "county_fips": "18141"}


def test_arms_length_filter():
    assert is_arms_length(_c(50000, 0.2)) is True
    assert is_arms_length(_c(2.39, 0.4, price=1)) is False      # $1 nominal quitclaim
    assert is_arms_length(_c(50000, 0.2, price=500)) is False   # below $1k floor
    assert is_arms_length(_c(2_000_000, 0.02)) is False         # > $1M/ac per-acre artifact
    assert is_arms_length({**_c(50000, 0.2), "vacant_land_flag": "N"}) is False


def test_insufficient_comps_null_not_zero():
    r = match_subject({"parcel_apn": "x", "acreage": 0.2, "av_land": "5000"}, [_c(50000, 0.2)], "2023-06")
    assert r["arbitrage_component"] is None and "insufficient" in r["status"]


def test_band_widens_once():
    # 2 in ±50% (0.1-0.3); 0.38 only in ±100% (0-0.4) -> widen to reach 3
    pool = [_c(50000, 0.2), _c(60000, 0.25), _c(40000, 0.38)]
    r = match_subject({"parcel_apn": "x", "acreage": 0.2, "av_land": "5000"}, pool, "2023-06")
    assert r["band_used"] == "+/-100%" and r["comps_n"] == 3
    assert r["arbitrage_component"] is not None


def test_fmv_and_arbitrage_mapping():
    pool = [_c(40000, 0.2), _c(50000, 0.22), _c(60000, 0.18)]   # median ppa 50000
    r = match_subject({"parcel_apn": "x", "acreage": 0.2, "av_land": "4000"}, pool, "2023-06")
    assert r["comps_n"] == 3 and r["fmv"] == 10000.0            # 50000 * 0.2
    assert r["spread"] == 6000.0 and r["arbitrage_component"] == 60.0   # (10000-4000)/10000


def test_assessed_above_market_is_grounded_zero_not_null():
    pool = [_c(40000, 0.2), _c(50000, 0.22), _c(60000, 0.18)]
    r = match_subject({"parcel_apn": "x", "acreage": 0.2, "av_land": "15000"}, pool, "2023-06")
    assert r["arbitrage_component"] == 0.0 and r["status"] == "grounded"   # comps exist, no upside


def test_acreage_floor_guard():
    r = match_subject({"parcel_apn": "x", "acreage": 0.005, "av_land": "5000"},
                      [_c(50000, 0.2)] * 5, "2023-06")
    assert r["arbitrage_component"] is None and "floor" in r["status"]

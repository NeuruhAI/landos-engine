from datetime import date

from app.comps import compute_arv, compute_max_bid, apg_tier
from scripts.import_comps import validate_comp_row

AS_OF = date(2026, 6, 3)


def _comp(price, acreage=None, dist=0.5, sd="2026-01-01", apn="A", addr="x"):
    return {"sale_price": price, "acreage": acreage, "distance_mi": dist,
            "sale_date": date.fromisoformat(sd), "comp_apn": apn, "comp_address": addr,
            "provenance": "manual_entry"}


# ---- ARV ----
def test_arv_per_lot_when_no_acreage():
    comps = [_comp(40000), _comp(50000), _comp(60000)]
    r = compute_arv(None, comps, AS_OF)
    assert r["basis"] == "per_lot" and r["n"] == 3
    assert 40000 <= r["arv"] <= 60000  # within comp range
    assert r["confidence"] == "normal"


def test_arv_per_acre_when_acreage_known():
    comps = [_comp(100000, acreage=1.0), _comp(120000, acreage=1.0), _comp(80000, acreage=1.0)]
    r = compute_arv(2.0, comps, AS_OF)  # subject is 2 acres
    assert r["basis"] == "per_acre"
    # ~ $100k/acre * 2 acres ~ $200k
    assert 180000 <= r["arv"] <= 220000


def test_arv_empty_is_blocked():
    assert compute_arv(None, [], AS_OF)["arv"] is None
    assert compute_arv(1.0, [{"sale_price": 0}], AS_OF)["arv"] is None


def test_arv_sparse_low_confidence():
    assert compute_arv(None, [_comp(50000)], AS_OF)["confidence"].startswith("low")


# ---- max bid / APG tiers ----
def test_apg_tiers():
    assert apg_tier(40000)[1:] == (0.50, 0.55)
    assert apg_tier(150000)[1:] == (0.55, 0.60)
    assert apg_tier(300000)[1:] == (0.55, 0.65)


def test_max_bid_range_and_net():
    b = compute_max_bid(100000)  # tier $100k-$250k? no: 100000 -> $100K-$250K (>=100k) -> .55/.60
    # costs 10k; net_cap = 100000-10000-10000 = 80000; threshold_high=0.60*100000=60000 -> bind APG ceiling
    assert b["bid_low"] == 55000.0 and b["bid_high"] == 60000.0
    assert b["binding"] == "APG % ceiling"
    assert b["net_at_high"] == 30000.0  # 100000-60000-10000


def test_max_bid_net_floor_binds_on_small_arv():
    b = compute_max_bid(40000)  # costs 4000; net_cap=40000-4000-10000=26000; threshold_high=0.55*40000=22000
    assert b["bid_high"] == 22000.0 and b["binding"] == "APG % ceiling"
    b2 = compute_max_bid(30000)  # costs 3000; net_cap=30000-3000-10000=17000; threshold_high=0.55*30000=16500
    assert b2["binding"] == "APG % ceiling"


# ---- import validation ----
def test_validate_ok():
    ok, c, _ = validate_comp_row({"subject_apn": "11-43-1", "comp_apn": "C1", "sale_price": "48,000",
                                  "sale_date": "2025-09-15", "acreage": "0.2"})
    assert ok and c["sale_price"] == 48000.0 and c["provenance"] == "manual_entry"


def test_validate_missing_subject():
    ok, _c, err = validate_comp_row({"comp_apn": "C1", "sale_price": "1000"})
    assert not ok and "subject_apn" in err


def test_validate_example_skipped():
    ok, _c, err = validate_comp_row({"subject_apn": "EXAMPLE-0", "comp_apn": "C1"})
    assert not ok and "example" in err


def test_validate_bad_price_and_date():
    ok, _c, err = validate_comp_row({"subject_apn": "S", "comp_apn": "C", "sale_price": "-5"})
    assert not ok and "sale_price" in err
    ok2, _c2, err2 = validate_comp_row({"subject_apn": "S", "comp_apn": "C", "sale_date": "09/15/2025"})
    assert not ok2 and "sale_date" in err2


def test_validate_needs_comp_identifier():
    ok, _c, err = validate_comp_row({"subject_apn": "S", "sale_price": "1000"})
    assert not ok and "comp_apn or comp_address" in err

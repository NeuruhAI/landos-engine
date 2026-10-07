"""
test_valuation.py — VALUATION LAB pure logic (no live DB):
  - acreage curve (non-linear $/acre taper)
  - land_value: honest-null with no comps, grounded + itemized with comps, unverified-overlay
    range widening
  - compute_mao: the (ARV × pct) − repairs − fee formula + honest-null
  - the county+acreage POOL selector (landos.parcel_sale): vacant-class filtering, class-family
    preference, acreage banding, IQR outlier trim, honest-null under 3 comps
The live endpoint + the 5-parcel Cass proof are pinned in the commit receipt.
"""
import app.valuation as V


# ---- acreage curve ---------------------------------------------------------
def test_acreage_curve_small_is_flat_big_tapers_and_floors():
    assert V.acreage_curve_factor(3) == 1.0            # <= REF: full $/acre
    assert V.acreage_curve_factor(None) == 1.0
    assert V.acreage_curve_factor(40) < 1.0            # bulk discount kicks in
    assert V.acreage_curve_factor(100000) >= V.CURVE_FLOOR   # never collapses past the floor


# ---- land_value ------------------------------------------------------------
def test_land_value_honest_null_without_comps():
    v = V.land_value(None, 5.0)
    assert v["value"] is None and v["confidence"] == "none"
    assert "not grounded" in v["reason"]
    assert v["adjustments"]                            # ledger still returned (auditable absence)


def test_land_value_grounded_and_itemized():
    v = V.land_value(10000.0, 4.0, in_sfha=False, dist_to_water_m=1000, buildable_fraction=1.0)
    assert v["value"] == 40000.0                       # 10000 * 4 * 1.0 (all neutral factors)
    assert v["confidence"] in ("high", "medium")       # few/no unverified overlays
    names = {a["name"] for a in v["adjustments"]}
    assert {"acreage_curve", "buildable_acres", "flood_sfha", "water_proximity", "road_frontage"} <= names


def test_land_value_sfha_and_water_move_the_number():
    dry = V.land_value(10000.0, 2.0, in_sfha=False, dist_to_water_m=5000, buildable_fraction=1.0)
    wet = V.land_value(10000.0, 2.0, in_sfha=True, dist_to_water_m=5000, buildable_fraction=1.0)
    near = V.land_value(10000.0, 2.0, in_sfha=False, dist_to_water_m=100, buildable_fraction=1.0)
    assert wet["value"] < dry["value"]                 # SFHA discounts
    assert near["value"] > dry["value"]                # lakefront premium


def test_land_value_unverified_overlays_widen_range_and_lower_confidence():
    v = V.land_value(10000.0, 2.0)                     # everything unverified
    assert v["confidence"] == "low"
    lo, hi = v["range"]
    assert (hi - lo) / v["value"] > 0.3               # wide because unverified count is high


# ---- house MAO -------------------------------------------------------------
def test_compute_mao_formula_and_carry():
    cfg = {"pct": 0.70, "fee_pct": 0.06, "fee_flat": 0.0, "repairs": 5000.0, "source": "x"}
    r = V.compute_mao(200000.0, cfg)
    # (200000*0.70) - 5000 - (0.06*200000) = 140000 - 5000 - 12000 = 123000
    assert r["value"] == 123000.0 and r["pct"] == 0.70 and r["fee"] == 12000.0
    assert r["config_source"] == "x"


def test_compute_mao_honest_null_without_arv():
    r = V.compute_mao(None, {"pct": 0.70, "source": "default"})
    assert r["value"] is None and "not grounded" in r["reason"]


# ---- pool selector: class family + IQR trim + banding ----------------------
def test_class_pref_maps_family_to_vacant_classes():
    assert V._class_pref("402") == ["402"]            # residential -> vacant res
    assert V._class_pref("101") == ["102", "101"]     # ag family
    assert V._class_pref(None) is None                # unknown -> caller uses all vacant


def test_iqr_trim_drops_the_outlier():
    # 8 clean comps + one lakefront-style outlier: enough points for a stable IQR fence.
    pool = [{"per_ac": v} for v in (4800, 4900, 5000, 5000, 5100, 5100, 5200, 5200, 900000)]
    trimmed = V._iqr_trim(pool)
    assert 900000 not in {c["per_ac"] for c in trimmed}
    assert len(trimmed) == 8


def test_iqr_trim_noop_under_eight():
    # small bands are left to the (already outlier-robust) median, not an unreliable IQR
    pool = [{"per_ac": 5000}, {"per_ac": 5100}, {"per_ac": 900000}]
    assert V._iqr_trim(pool) == pool


def test_band_and_trim_bands_to_subject_acreage():
    pool = [{"per_ac": 5000, "size": 1.0}, {"per_ac": 5200, "size": 1.2},
            {"per_ac": 4800, "size": 0.9}, {"per_ac": 3000, "size": 50.0}]  # 50ac is out of band
    used, note = V._band_and_trim(pool, subject_acres=1.0)   # band 0.5–2 ac
    assert all(0.5 <= c["size"] <= 2.0 for c in used)
    assert "0.5-2x" in note


class _PoolCur:
    """Fake cursor: returns a fixed parcel_sale row set regardless of params."""
    def __init__(self, rows):
        self._rows = rows
    def execute(self, sql, params=None):
        self._params = params
    def fetchall(self):
        return self._rows


def _sale(apn, price, acre, cls):
    return {"parcel_apn": apn, "sale_price": price, "acreage": acre,
            "class_code": cls, "sale_date": "2019-01-01", "source": "test"}


def test_pool_selector_grounds_a_median_over_vacant_comps():
    rows = [_sale("A", 10000, 1.0, "402"), _sale("B", 11000, 1.1, "402"),
            _sale("C", 9000, 0.9, "402"), _sale("D", 12000, 1.2, "402")]
    out = V.comps_by_county_acreage(_PoolCur(rows), "26027", 1.0,
                                    subject_class="402", exclude_apn="SUBJ")
    assert out["grounded"] is True and out["source"] == "landos.parcel_sale"
    assert 9000 <= out["comp_median"] <= 12000
    assert out["n"] >= 3


def test_pool_selector_honest_null_under_three():
    rows = [_sale("A", 10000, 1.0, "402")]
    out = V.comps_by_county_acreage(_PoolCur(rows), "26027", 1.0, subject_class=None)
    assert out["grounded"] is False and out["comp_median"] is None


def test_pool_selector_no_county_is_null():
    out = V.comps_by_county_acreage(_PoolCur([]), None, 1.0)
    assert out["grounded"] is False and out["n"] == 0

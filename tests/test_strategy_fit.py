"""
test_strategy_fit.py — alt_use_fitness grounding/NULL discipline + banding + the
do-not-pretend-coverage (across-state-line) rule.
"""
from app.strategy_fit import alt_use_fitness, ALT_USE_VERSION, substation_band

AS_OF = "2026-06-19T00:00:00+00:00"
SUBS_IN = [{"lat": 41.70, "lng": -86.25, "state": "IN"}]   # one IN substation


def _lead(**kw):
    base = dict(distress_tokens=[], distress_present=True, in_opportunity_zone=False,
                oz_present=True, situs_lat=41.70, situs_lng=-86.25, parcel_state="IN")
    base.update(kw)
    return base


def test_band_thresholds():
    assert substation_band(0.5) == (30, "<1mi")
    assert substation_band(3) == (22, "1-5mi")
    assert substation_band(10) == (12, "5-15mi")
    assert substation_band(20) == (5, "15-30mi")
    assert substation_band(50) == (0, ">=30mi")


def test_double_stack_near_substation_caps_inputs():
    a = alt_use_fitness(_lead(distress_tokens=["environmental_part201"], in_opportunity_zone=True),
                        SUBS_IN, "sha", AS_OF)
    assert a["version"] == ALT_USE_VERSION
    assert a["inputs"]["contamination"]["value"] is True
    assert a["inputs"]["contamination"]["kind"] == "part201"
    assert a["inputs"]["opportunity_zone"]["value"] is True
    assert a["inputs"]["substation_proximity"]["grounded"] is True
    assert a["score"] == 100   # 40 + 30 + 30


def test_present_false_contributes_zero_not_missing():
    # tax_delinquent = distress observed but NOT contamination -> grounded true, value false, +0
    a = alt_use_fitness(_lead(distress_tokens=["tax_delinquent"], in_opportunity_zone=False),
                        [], None, AS_OF)
    c = a["inputs"]["contamination"]
    assert c["grounded"] is True and c["value"] is False
    oz = a["inputs"]["opportunity_zone"]
    assert oz["grounded"] is True and oz["value"] is False
    assert a["score"] == 0


def test_oz_missing_is_null_not_zero_data():
    a = alt_use_fitness(_lead(oz_present=False, in_opportunity_zone=None), [], None, AS_OF)
    oz = a["inputs"]["opportunity_zone"]
    assert oz["grounded"] is False and oz["value"] is None   # MISSING, omitted from sum


def test_mi_parcel_across_state_line_ungrounded():
    # MI parcel, only IN substations in feed -> nearest is across the line -> not grounded, +0
    a = alt_use_fitness(_lead(parcel_state="MI", distress_tokens=["brownfield"], in_opportunity_zone=True),
                        SUBS_IN, "sha", AS_OF)
    sub = a["inputs"]["substation_proximity"]
    assert sub["grounded"] is False and sub["weight"] == 0
    assert sub["nearest_mi"] is not None          # distance recorded (flagged), not scored
    assert sub["coverage_scope"] == "nw_indiana_only"
    assert a["score"] == 70                        # 40 contam + 30 OZ, no substation


def test_no_substation_feed_ungrounded():
    a = alt_use_fitness(_lead(distress_tokens=["brownfield"], in_opportunity_zone=True), [], None, AS_OF)
    assert a["inputs"]["substation_proximity"]["grounded"] is False
    assert a["score"] == 70


def test_cap_at_100():
    a = alt_use_fitness(_lead(distress_tokens=["environmental_part201"], in_opportunity_zone=True),
                        SUBS_IN, "sha", AS_OF)
    assert a["score"] <= 100

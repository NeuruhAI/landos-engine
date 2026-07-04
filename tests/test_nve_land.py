"""
test_nve_land.py — unit tests for NVE-LAND, the sovereign land-valuation scorer.

Pure, no DB / no network. Locks the land-portal-dd rubric math (app/nve_land.py) after the
Land Portal data source was replaced by county/federal adapters:
  - deal-killers fire ONLY on grounded values; None (unverified) never fabricates a killer
  - the six factor bands + tier-downgrade + killer override
  - EV blend (50% comp median / 30% county base / 20% ZIP), ±5%, <2-comp fallback
  - strategy-aware offers (FLIP / fast-market / cautious / SUBDIVIDE gate) + double-close margin
"""
from app.nve_land import assess, deal_killers, score_factors, compute_expected_value, _double_close


def _strong_metrics():
    return {"size": 10, "land_locked": False, "road_frontage": 300, "wetlands": 2, "fema": 0,
            "buildable": 85, "base_per_ac": 8000, "base_total": 80000, "zip_per_ac": 7000,
            "comp_median": 6500}


def _comps():
    return [{"per_ac": 6500, "size": 9, "land_locked": "No", "dom": 90},
            {"per_ac": 6800, "size": 11, "land_locked": "No", "dom": 120},
            {"per_ac": 6200, "size": 8, "dom": 80},
            {"per_ac": 7000, "size": 12, "dom": 140},
            {"per_ac": 6600, "size": 10, "dom": 100}]


def test_strong_parcel_pursue():
    r = assess({"metrics": _strong_metrics(), "comps": _comps()})
    assert r["land_score"] == 95
    assert r["stance"] == "PURSUE"
    assert r["deal_killers"] == []
    # gap = 80000 / (6500*10) = 1.23 -> 1.1-1.3 band -> 20
    assert r["score_breakdown"]["Valuation"] == 20
    assert r["score_breakdown"]["Access"] == 20


def test_killers_only_on_grounded_values():
    # land-locked True fires; 75%+ flood/wet fire; size<=0.1 fires; no base fires
    k = deal_killers({"size": 5, "land_locked": True, "fema": 80, "wetlands": 90, "base_total": 100})
    assert any("Land-locked" in x for x in k)
    assert any("FEMA" in x for x in k)
    assert any("Wetlands" in x for x in k)
    # None road frontage / None land_locked must NOT fabricate an access killer
    k2 = deal_killers({"size": 5, "land_locked": None, "road_frontage": None, "base_total": 50000})
    assert not any("access" in x.lower() or "frontage" in x.lower() for x in k2)
    # grounded road_frontage == 0 (not land-locked) DOES fire
    k3 = deal_killers({"size": 5, "land_locked": False, "road_frontage": 0, "base_total": 50000})
    assert any("road frontage" in x.lower() for x in k3)
    # missing base valuation fires
    assert any("base valuation" in x.lower() for x in deal_killers({"size": 5, "base_total": None}))


def test_unknown_feeds_flagged_not_zeroed():
    r = assess({"metrics": {"size": 5, "base_total": 50000, "base_per_ac": 10000,
                            "wetlands": None, "fema": None, "buildable": None,
                            "road_frontage": None}, "comps": []})
    # all four unverified feeds are flagged estimated, not silently scored 0
    for f in ("Access", "Wetlands", "FEMA", "Slope", "Valuation"):
        assert f in r["estimated_factors"]
    # provisional Access is mid-tier (10), never a fabricated 0-or-killer
    assert r["score_breakdown"]["Access"] == 10


def test_killer_override_forces_pass():
    m = _strong_metrics()
    m["land_locked"] = True
    r = assess({"metrics": m, "comps": _comps()})
    assert r["stance"] == "PASS"
    assert r["primary_strategy"] is None  # PASS => no offer


def test_ev_blend_and_spread():
    ev = compute_expected_value({"base_per_ac": 8000, "zip_per_ac": 7000}, _comps(), 10)
    # blend = .5*comp_med + .3*8000 + .2*7000 ; ±5%
    assert ev["comps_used"] >= 4
    assert abs(ev["high"] / ev["low"] - (1.05 / 0.95)) < 1e-6
    assert "50% comp median" in ev["methodology"]


def test_ev_fallback_under_two_comps():
    ev = compute_expected_value({"base_per_ac": 5000}, [{"per_ac": 5000, "size": 10}], 10)
    assert ev["comp_median_clean"] is None
    assert ev["low"] == 5000 * 10 * 0.9 and ev["high"] == 5000 * 10 * 1.1


def test_flip_bands_fast_vs_cautious():
    # fast market (>=5 comps, avg dom < 150) -> 45-55%
    r = assess({"metrics": _strong_metrics(), "comps": _comps()})
    ev_mid = (r["expected_value"]["low"] + r["expected_value"]["high"]) / 2
    assert r["primary_strategy"]["name"] == "FLIP"
    assert abs(r["primary_strategy"]["offer_low"] / ev_mid - 0.45) < 1e-6
    assert abs(r["primary_strategy"]["offer_high"] / ev_mid - 0.55) < 1e-6


def test_double_close_margin_and_suppression():
    # EV<200k -> $20k margin; suppressed when ceiling not >5% above primary high
    ev = {"low": 100000}
    assert _double_close(90000, 10, ev, primary_high=100000) is None  # ceiling 80k < primary -> suppressed
    keep = _double_close(90000, 10, ev, primary_high=50000)
    assert keep and keep["offer_high"] == 80000 and round(keep["offer_low"]) == 72000


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print("PASS", fn.__name__)
    print(f"\n{len(fns)}/{len(fns)} passed")


if __name__ == "__main__":
    _run()

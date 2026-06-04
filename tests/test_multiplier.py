from datetime import date
from app.multiplier import compute_multiplier

AS_OF = date(2026, 6, 3)
AUTH_OZ = {
    "overlay_type": "opportunity_zone", "name": "OZ T4",
    "provenance_status": "authoritative", "multiplier_weight": 0.30,
    "effective_date": "2026-12-31",
}
UNCONF = {
    "overlay_type": "brownfield", "name": "BF",
    "provenance_status": "sourced_unconfirmed", "multiplier_weight": 0.30,
    "effective_date": None,
}


def test_authoritative_applied():
    r = compute_multiplier([AUTH_OZ], as_of=AS_OF)
    assert r["multiplier"] == 1.30
    assert len(r["applied"]) == 1 and not r["logged"]


def test_unconfirmed_logged_not_applied():
    r = compute_multiplier([UNCONF], as_of=AS_OF)
    assert r["multiplier"] == 1.0
    assert not r["applied"] and len(r["logged"]) == 1
    assert r["logged"][0]["not_applied_reason"] == "sourced_unconfirmed"


def test_expired_authoritative_logged():
    r = compute_multiplier([AUTH_OZ], as_of=date(2027, 1, 1))
    assert r["multiplier"] == 1.0
    assert r["logged"][0]["not_applied_reason"] == "expired"


def test_clamp_lower_bound():
    r = compute_multiplier([{**AUTH_OZ, "multiplier_weight": 0.02}], as_of=AS_OF)
    assert r["multiplier"] == 1.1


def test_clamp_upper_bound():
    r = compute_multiplier([{**AUTH_OZ, "multiplier_weight": 0.9}], as_of=AS_OF)
    assert r["multiplier"] == 1.4


def test_no_hits_is_neutral():
    assert compute_multiplier([], as_of=AS_OF)["multiplier"] == 1.0


def test_additive_stacking():
    a = {**AUTH_OZ, "multiplier_weight": 0.1}
    b = {**AUTH_OZ, "overlay_type": "tif", "multiplier_weight": 0.1}
    assert compute_multiplier([a, b], as_of=AS_OF)["multiplier"] == 1.2

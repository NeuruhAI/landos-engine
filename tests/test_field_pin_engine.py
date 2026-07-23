"""
test_field_pin_engine.py — FIELD PIN V1 pure engine invariants (no DB).

Ray construction determinism, side->bearing law, fail-closed capture
validation, and receipt byte-stability. Mirrors the receipt.py doctrine:
everything here imports app.field_pins only (never app.main / app.db).
"""
import math

import pytest

from app.field_pins import (
    DEFAULT_OFFSET_M,
    RAY_LENGTH_M,
    FieldPinValidationError,
    build_observation,
    capture_receipt,
    destination_point,
    look_bearing,
    validate_capture,
)

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def valid_payload(**over):
    p = {
        "tenant_id": TENANT, "device_id": "phone-1",
        "lat": 41.6, "lon": -86.7, "observed_at": "2026-07-22T15:00:00Z",
        "heading_deg": 0.0, "heading_source": "gps_course", "side": "right",
        "category": "distress", "spoken_text": "roof caving on the right",
        "accuracy_m": 8.0, "idempotency_key": "cap-001",
    }
    p.update(over)
    return p


# ── ray geometry ─────────────────────────────────────────────────────────────
def test_destination_point_east_at_equator():
    lat, lon = destination_point(0.0, 0.0, 90.0, 111_195.0)  # ~1 degree of arc
    assert abs(lat) < 1e-6
    assert abs(lon - 1.0) < 1e-3


def test_look_bearing_side_law():
    assert look_bearing(0.0, "right") == 90.0
    assert look_bearing(0.0, "left") == 270.0
    assert look_bearing(45.0, "ahead") == 45.0
    assert look_bearing(350.0, "right") == 80.0     # wraps
    assert look_bearing(10.0, "left") == 280.0      # wraps
    assert look_bearing(90.0, "at") is None         # no look direction
    assert look_bearing(90.0, "unknown") is None
    assert look_bearing(None, "right") is None      # no heading, no ray


def test_build_observation_ray_and_projection():
    geo = build_observation(41.6, -86.7, 0.0, "right", DEFAULT_OFFSET_M)
    assert geo["look_bearing"] == 90.0
    # projected 25 m east, ray end 150 m east — both east of origin, same latitude
    assert geo["projected"][1] > -86.7 and abs(geo["projected"][0] - 41.6) < 1e-5
    assert geo["ray_end"][1] > geo["projected"][1]
    # deterministic: identical input -> identical rounded output
    assert geo == build_observation(41.6, -86.7, 0.0, "right", DEFAULT_OFFSET_M)


def test_build_observation_honest_none_without_direction():
    for side, heading in (("at", 90.0), ("unknown", 90.0), ("right", None)):
        geo = build_observation(41.6, -86.7, heading, side, DEFAULT_OFFSET_M)
        assert geo["ray_end"] is None
        assert geo["projected"] == (41.6, -86.7)    # anchored at origin


def test_antimeridian_and_pole_stay_in_range():
    lat, lon = destination_point(41.6, 179.9999, 90.0, RAY_LENGTH_M)
    assert -180.0 <= lon < 180.0
    lat2, lon2 = destination_point(89.9999, 0.0, 0.0, RAY_LENGTH_M)
    assert math.isfinite(lat2) and math.isfinite(lon2) and -90.0 <= lat2 <= 90.0


# ── fail-closed validation ───────────────────────────────────────────────────
@pytest.mark.parametrize("over", [
    {"tenant_id": "not-a-uuid"},
    {"device_id": ""},
    {"lat": 91.0}, {"lat": -91.0}, {"lon": 181.0}, {"lon": -181.0},
    {"lat": float("nan")},
    {"accuracy_m": -1.0}, {"accuracy_m": 99999.0},
    {"heading_deg": 360.0}, {"heading_deg": -5.0},
    {"heading_source": "ouija"},
    {"heading_deg": None},                       # required unless source=unknown
    {"side": "behind"},
    {"speed_mps": -3.0}, {"speed_mps": 500.0},
    {"assumed_offset_m": -1.0}, {"assumed_offset_m": 151.0},
    {"category": ""}, {"category": "x" * 101},
    {"spoken_text": "x" * 4001},
    {"observed_at": None}, {"observed_at": "yesterday-ish"},
    {"observed_at": "2026-07-22T15:00:00"},      # naive timestamp refused
    {"idempotency_key": ""}, {"idempotency_key": "k" * 201},
])
def test_validate_capture_fails_closed(over):
    with pytest.raises(FieldPinValidationError):
        validate_capture(valid_payload(**over))


def test_validate_capture_normalizes():
    v = validate_capture(valid_payload(lat=41.60000004, lon=-86.70000004))
    assert v["lat"] == 41.6 and v["lon"] == -86.7          # 7-dp determinism
    assert v["assumed_offset_m"] == DEFAULT_OFFSET_M       # default applied
    assert v["observed_at"].tzinfo is not None             # normalized to aware UTC


def test_validate_capture_unknown_heading_source_permits_no_heading():
    v = validate_capture(valid_payload(heading_source="unknown", heading_deg=None, side="unknown"))
    assert v["heading_deg"] is None and v["side"] == "unknown"


# ── deterministic receipt ────────────────────────────────────────────────────
def test_receipt_is_byte_stable_and_content_bound():
    obs = {"lat": 41.6, "lon": -86.7, "side": "right", "category": "distress"}
    r1 = capture_receipt("11111111-1111-1111-1111-111111111111", obs)
    r2 = capture_receipt("11111111-1111-1111-1111-111111111111", dict(obs))
    assert r1 == r2                                        # replay-stable
    r3 = capture_receipt("11111111-1111-1111-1111-111111111111", {**obs, "side": "left"})
    assert r3["receipt_sha256"] != r1["receipt_sha256"]    # content-bound
    r4 = capture_receipt("22222222-2222-2222-2222-222222222222", obs)
    assert r4["receipt_sha256"] != r1["receipt_sha256"]    # pin-bound

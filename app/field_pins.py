"""
field_pins.py — FIELD PIN PRODUCTION V1: capture -> ray -> parcel intelligence.

The windshield-sensor spine behind POST /field-pins. A field pin is a RAY, not a
point (024): "from HERE, on THIS heading, I saw something on my RIGHT." This
module owns the production contract around the reviewed SQL objects
(024 field_pins / 025 fn_resolve_field_pin / 023 fn_parcel_plat /
039 fn_field_pin_lake_michigan):

  * validation is FAIL-CLOSED (bad coordinates / vocab / lengths -> 422-class
    error, nothing stored);
  * the observation ray is built HERE, deterministically, and preserved on the
    row (origin + heading + side -> 150 m look-ray; honest None when heading or
    side cannot support one);
  * resolution defers to landos.fn_resolve_field_pin — ray_intersect first,
    reviewed nearest_parcel fallback only where 025 permits, honest 'none'
    otherwise. An unresolved pin is a VALID terminal row, never dropped, and
    /resolve re-runs the ladder when geometry lands later;
  * idempotency_key replay returns the SAME pin + byte-identical receipt;
  * near-duplicate captures (same tenant/device/category, close in time+space,
    different key) are stored but linked via duplicate_of_pin_id;
  * Lake Michigan proximity comes ONLY from fn_field_pin_lake_michigan —
    UNWIRED / UNAVAILABLE are surfaced verbatim, never papered over.

HARD RULE — NO LEAD MINTING: this module never writes acquisition_leads,
owner_leads, or any lead/candidate table. A field pin is a SIGNAL; minting is a
human decision elsewhere. It also never contacts owners and never touches
listing platforms.

Pure helpers live up top (no DB import — unit-testable without a database,
same doctrine as receipt.py / run_manifest.py). Service functions take an open
psycopg connection from the caller (main.py owns the pool).
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

RECEIPT_VERSION = "field-pin.1"
RAY_LENGTH_M = 150.0          # 024: "origin -> 150 m; THIS is what resolves"
DEFAULT_OFFSET_M = 25.0       # 024: assumed_offset_m default
DUPLICATE_WINDOW_S = 120      # near-duplicate: same tenant/device/category ...
DUPLICATE_RADIUS_M = 25.0     # ... within 120 s and 25 m of an earlier capture
EARTH_RADIUS_M = 6_371_008.8

HEADING_SOURCES = ("gps_course", "fix_delta", "compass", "unknown")
SIDES = ("left", "right", "ahead", "at", "unknown")
MAX_TEXT = 4000


class FieldPinValidationError(ValueError):
    """Fail-closed capture validation: nothing is stored when this raises."""


# ---------------------------------------------------------------------------
# PURE GEOMETRY (no DB) — deterministic ray construction.
# ---------------------------------------------------------------------------
def destination_point(lat: float, lon: float, bearing_deg: float, distance_m: float) -> tuple[float, float]:
    """Great-circle forward point: from (lat, lon) travel distance_m on bearing_deg.
    Returns (lat, lon) rounded to 7 decimals (~1 cm) so the same input always
    yields the same stored coordinates."""
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    theta = math.radians(bearing_deg)
    delta = distance_m / EARTH_RADIUS_M
    phi2 = math.asin(math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(theta))
    lam2 = lam1 + math.atan2(
        math.sin(theta) * math.sin(delta) * math.cos(phi1),
        math.cos(delta) - math.sin(phi1) * math.sin(phi2),
    )
    lam2 = (lam2 + 3 * math.pi) % (2 * math.pi) - math.pi   # normalize to [-180, 180)
    return (round(math.degrees(phi2), 7), round(math.degrees(lam2), 7))


def look_bearing(heading_deg: Optional[float], side: str) -> Optional[float]:
    """The direction the human was LOOKING. right = heading+90, left = heading-90,
    ahead = heading. 'at' and 'unknown' have no look direction; no heading, no ray."""
    if heading_deg is None or side in ("at", "unknown"):
        return None
    if side == "right":
        return (heading_deg + 90.0) % 360.0
    if side == "left":
        return (heading_deg - 90.0) % 360.0
    return heading_deg % 360.0  # ahead


def build_observation(lat: float, lon: float, heading_deg: Optional[float], side: str,
                      offset_m: float) -> Dict[str, Any]:
    """Deterministic observation geometry. Returns projected (display dot) and the
    ray endpoint, each as (lat, lon) or None. A pin with no look direction keeps
    projected = origin (the 025 nearest fallback anchors there) and ray = None —
    an honest 'cannot ray' state, not a fabricated direction."""
    bearing = look_bearing(heading_deg, side)
    if bearing is None:
        return {"look_bearing": None, "projected": (lat, lon), "ray_end": None}
    return {
        "look_bearing": round(bearing, 4),
        "projected": destination_point(lat, lon, bearing, offset_m),
        "ray_end": destination_point(lat, lon, bearing, RAY_LENGTH_M),
    }


# ---------------------------------------------------------------------------
# PURE VALIDATION (no DB) — fail closed before anything is stored.
# ---------------------------------------------------------------------------
def validate_capture(p: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize + validate a capture request dict. Raises FieldPinValidationError
    on any out-of-contract value; returns the normalized dict otherwise."""
    def bad(msg: str) -> None:
        raise FieldPinValidationError(msg)

    out: Dict[str, Any] = {}
    try:
        out["tenant_id"] = str(uuid.UUID(str(p.get("tenant_id"))))
    except Exception:  # noqa: BLE001
        bad("tenant_id must be a UUID")
    device = str(p.get("device_id") or "").strip()
    if not device or len(device) > 200:
        bad("device_id is required (<=200 chars)")
    out["device_id"] = device

    try:
        lat = float(p["lat"]); lon = float(p["lon"])
    except Exception:  # noqa: BLE001
        bad("lat/lon are required numbers")
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        bad("lat/lon out of range")
    if math.isnan(lat) or math.isnan(lon):
        bad("lat/lon out of range")
    out["lat"], out["lon"] = round(lat, 7), round(lon, 7)

    acc = p.get("accuracy_m")
    if acc is not None:
        acc = float(acc)
        if math.isnan(acc) or acc < 0 or acc > 10_000:
            bad("accuracy_m out of range")
    out["accuracy_m"] = acc

    heading = p.get("heading_deg")
    if heading is not None:
        heading = float(heading)
        if math.isnan(heading) or not (0.0 <= heading < 360.0):
            bad("heading_deg must be in [0, 360)")
    out["heading_deg"] = heading

    hs = str(p.get("heading_source") or "").strip()
    if hs not in HEADING_SOURCES:
        bad(f"heading_source must be one of {HEADING_SOURCES}")
    if hs != "unknown" and heading is None:
        bad("heading_deg is required unless heading_source='unknown'")
    out["heading_source"] = hs

    side = str(p.get("side") or "").strip()
    if side not in SIDES:
        bad(f"side must be one of {SIDES}")
    out["side"] = side

    speed = p.get("speed_mps")
    if speed is not None:
        speed = float(speed)
        if math.isnan(speed) or speed < 0 or speed > 150:
            bad("speed_mps out of range")
    out["speed_mps"] = speed

    offset = p.get("assumed_offset_m")
    offset = DEFAULT_OFFSET_M if offset is None else float(offset)
    if math.isnan(offset) or not (0.0 <= offset <= RAY_LENGTH_M):
        bad("assumed_offset_m out of range")
    out["assumed_offset_m"] = offset

    category = str(p.get("category") or "").strip()
    if not category or len(category) > 100:
        bad("category is required (<=100 chars)")
    out["category"] = category

    spoken = p.get("spoken_text")
    if spoken is not None:
        spoken = str(spoken)
        if len(spoken) > MAX_TEXT:
            bad(f"spoken_text too long (>{MAX_TEXT})")
    out["spoken_text"] = spoken

    observed_at = p.get("observed_at")
    if not observed_at:
        bad("observed_at (ISO timestamp) is required")
    try:
        parsed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
    except ValueError:
        bad("observed_at must be an ISO-8601 timestamp")
        raise  # unreachable; keeps type-checkers honest
    if parsed.tzinfo is None:
        bad("observed_at must carry a timezone (use Z or an offset)")
    out["observed_at"] = parsed.astimezone(timezone.utc)

    idem = p.get("idempotency_key")
    if idem is not None:
        idem = str(idem).strip()
        if not idem or len(idem) > 200:
            bad("idempotency_key must be 1..200 chars when provided")
    out["idempotency_key"] = idem

    label = p.get("operator_label")
    out["operator_label"] = (str(label)[:200] if label is not None else None)
    return out


# ---------------------------------------------------------------------------
# PURE RECEIPT (no DB) — deterministic, replay-stable.
# ---------------------------------------------------------------------------
def capture_receipt(pin_id: str, observation: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic capture receipt: canonical JSON of the stored observation
    facts, SHA-256'd. The same pin (idempotent replay) always yields the same
    receipt_sha256 — byte-stable, same idiom as receipt.py section hashes."""
    body = {"receipt_version": RECEIPT_VERSION, "pin_id": str(pin_id), "observation": observation}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "receipt_version": RECEIPT_VERSION,
        "pin_id": str(pin_id),
        "receipt_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _observation_facts(v: Dict[str, Any], geo: Dict[str, Any]) -> Dict[str, Any]:
    """The exact fact set the receipt hashes — stored-column values only, so a
    replay (which re-reads the same row) reproduces the identical hash."""
    return {
        "tenant_id": v["tenant_id"], "device_id": v["device_id"],
        "observed_at": v["observed_at"], "lat": v["lat"], "lon": v["lon"],
        "accuracy_m": v["accuracy_m"], "heading_deg": v["heading_deg"],
        "heading_source": v["heading_source"], "side": v["side"],
        "speed_mps": v["speed_mps"], "assumed_offset_m": v["assumed_offset_m"],
        "category": v["category"], "spoken_text": v["spoken_text"],
        "idempotency_key": v["idempotency_key"],
        "look_bearing": geo["look_bearing"],
        "projected": list(geo["projected"]) if geo["projected"] else None,
        "ray_end": list(geo["ray_end"]) if geo["ray_end"] else None,
        "ray_length_m": RAY_LENGTH_M if geo["ray_end"] else None,
    }


# ---------------------------------------------------------------------------
# SERVICE (DB) — the caller owns the connection; we own the contract.
# ---------------------------------------------------------------------------
def _point_wkt(lat_lon: tuple[float, float]) -> str:
    lat, lon = lat_lon
    return f"POINT({lon} {lat})"


def _line_wkt(a: tuple[float, float], b: tuple[float, float]) -> str:
    return f"LINESTRING({a[1]} {a[0]}, {b[1]} {b[0]})"


def _row_observation(row: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild the receipt fact set from a stored row (replay path)."""
    geo = build_observation(
        float(row["lat"]), float(row["lon"]),
        float(row["heading_deg"]) if row["heading_deg"] is not None else None,
        row["side"], float(row["assumed_offset_m"]),
    )
    v = {
        "tenant_id": str(row["tenant_id"]), "device_id": row["device_id"],
        "observed_at": row["observed_at_iso"], "lat": float(row["lat"]), "lon": float(row["lon"]),
        "accuracy_m": float(row["origin_accuracy_m"]) if row["origin_accuracy_m"] is not None else None,
        "heading_deg": float(row["heading_deg"]) if row["heading_deg"] is not None else None,
        "heading_source": row["heading_source"], "side": row["side"],
        "speed_mps": float(row["speed_mps"]) if row["speed_mps"] is not None else None,
        "assumed_offset_m": float(row["assumed_offset_m"]),
        "category": row["category"], "spoken_text": row["spoken_text"],
        "idempotency_key": row["idempotency_key"],
    }
    return _observation_facts(v, geo)


_ROW_SQL = """
select pin_id, tenant_id, device_id, operator_label, category, spoken_text,
       heading_deg, heading_source, side, speed_mps, assumed_offset_m,
       origin_accuracy_m, idempotency_key, duplicate_of_pin_id,
       resolved_county_fips, resolved_apn, resolution_method,
       resolution_confidence, resolved_at,
       st_y(origin) as lat, st_x(origin) as lon,
       to_char(observed_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"') as observed_at_iso
from landos.field_pins where {where}
"""


def _resolution_view(conn, res: Dict[str, Any]) -> Dict[str, Any]:
    """Shape 025's resolver jsonb into the API's resolution block, adding the
    situs address evidence (parcel_owner.situs_address) for resolved parcels."""
    address = None
    owner_mailing = None
    if res.get("apn"):
        with conn.cursor() as cur:
            cur.execute(
                "select situs_address, owner_mailing from landos.parcel_owner "
                "where county_fips = %s and parcel_apn = %s",
                (res.get("county_fips"), res.get("apn")),
            )
            row = cur.fetchone()
            if row:
                address = row["situs_address"]
                owner_mailing = row["owner_mailing"]
    return {
        "apn": res.get("apn"),
        "county_fips": res.get("county_fips"),
        "address": address,                      # situs address of record, honest-null
        "owner": res.get("owner"),               # 025: name or 'not on file'
        "owner_flag": res.get("owner_flag"),     # private | govt | not_on_file
        "owner_mailing": owner_mailing,
        "acres": res.get("acres"),
        "resolution_method": res.get("resolution_method"),
        "confidence": res.get("confidence"),
        "coverage": res.get("coverage"),         # dark_county | covered_no_parcel (unresolved only)
        "in_crm": res.get("in_crm"),
    }


def _plat(conn, county: Optional[str], apn: Optional[str]) -> Optional[Dict[str, Any]]:
    """Subject + neighbor GeoJSON via the reviewed fn_parcel_plat. None when the
    pin is unresolved (there is no subject parcel to plat)."""
    if not county or not apn:
        return None
    with conn.cursor() as cur:
        cur.execute("select landos.fn_parcel_plat(%s, %s) as plat", (county, apn))
        return cur.fetchone()["plat"]


def _lake(conn, pin_id: str) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("select landos.fn_field_pin_lake_michigan(%s::uuid) as lake", (pin_id,))
        return cur.fetchone()["lake"]


def _resolve(conn, pin_id: str) -> Dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("select landos.fn_resolve_field_pin(%s::uuid) as res", (pin_id,))
        return cur.fetchone()["res"]


def _envelope(conn, row: Dict[str, Any], res: Dict[str, Any], *, replay: bool,
              duplicate_of: Optional[str]) -> Dict[str, Any]:
    resolution = _resolution_view(conn, res)
    return {
        "ok": True,
        "pin_id": str(row["pin_id"]),
        "replay": replay,
        "duplicate_of_pin_id": duplicate_of,
        "receipt": capture_receipt(str(row["pin_id"]), _row_observation(row)),
        "resolution": resolution,
        "plat": _plat(conn, resolution["county_fips"], resolution["apn"]),
        "lake_michigan": _lake(conn, str(row["pin_id"])),
    }


def capture_pin(conn, payload: Dict[str, Any]) -> Dict[str, Any]:
    """The POST /field-pins spine. Validates fail-closed, replays idempotently,
    stores the observation + ray, resolves, and returns the full envelope.
    Resolution trouble NEVER discards the stored pin: the capture commits with
    an honest unresolved state and a resolution_error note instead."""
    v = validate_capture(payload)

    # Idempotent replay: same (tenant, key) -> the SAME pin, same receipt. No
    # second row, no re-insert, no new resolution stamp.
    if v["idempotency_key"]:
        with conn.cursor() as cur:
            cur.execute(
                _ROW_SQL.format(where="tenant_id = %s and idempotency_key = %s"),
                (v["tenant_id"], v["idempotency_key"]),
            )
            row = cur.fetchone()
        if row:
            res = _stamped_resolution(conn, row)
            return _envelope(conn, row, res, replay=True,
                             duplicate_of=str(row["duplicate_of_pin_id"]) if row["duplicate_of_pin_id"] else None)

    geo = build_observation(v["lat"], v["lon"], v["heading_deg"], v["side"], v["assumed_offset_m"])

    # Near-duplicate linkage (never a drop): same tenant+device+category within
    # the window and radius links to the ORIGINAL capture.
    duplicate_of = None
    with conn.cursor() as cur:
        cur.execute(
            """
            select pin_id from landos.field_pins
            where tenant_id = %s and device_id = %s and category = %s
              and duplicate_of_pin_id is null
              and abs(extract(epoch from (observed_at - %s::timestamptz))) <= %s
              and st_dwithin(origin::geography, st_setsrid(st_geomfromtext(%s), 4326)::geography, %s)
            order by observed_at asc limit 1
            """,
            (v["tenant_id"], v["device_id"], v["category"], v["observed_at"],
             DUPLICATE_WINDOW_S, _point_wkt((v["lat"], v["lon"])), DUPLICATE_RADIUS_M),
        )
        dup = cur.fetchone()
        if dup:
            duplicate_of = str(dup["pin_id"])

    ray_wkt = _line_wkt((v["lat"], v["lon"]), geo["ray_end"]) if geo["ray_end"] else None
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into landos.field_pins
              (tenant_id, device_id, operator_label, observed_at,
               origin, origin_accuracy_m, heading_deg, heading_source, speed_mps,
               side, assumed_offset_m, projected, ray, category, spoken_text,
               idempotency_key, duplicate_of_pin_id)
            values
              (%s, %s, %s, %s,
               st_setsrid(st_geomfromtext(%s), 4326), %s, %s, %s, %s,
               %s, %s, st_setsrid(st_geomfromtext(%s), 4326),
               st_setsrid(st_geomfromtext(%s), 4326),
               %s, %s, %s, %s)
            on conflict (tenant_id, idempotency_key) where idempotency_key is not null
            do nothing
            returning pin_id
            """,
            (v["tenant_id"], v["device_id"], v["operator_label"], v["observed_at"],
             _point_wkt((v["lat"], v["lon"])), v["accuracy_m"], v["heading_deg"],
             v["heading_source"], v["speed_mps"], v["side"], v["assumed_offset_m"],
             _point_wkt(geo["projected"]), ray_wkt,
             v["category"], v["spoken_text"], v["idempotency_key"], duplicate_of),
        )
        inserted = cur.fetchone()

    if inserted is None and v["idempotency_key"]:
        # Concurrent replay lost the insert race: read the winner and return it.
        with conn.cursor() as cur:
            cur.execute(
                _ROW_SQL.format(where="tenant_id = %s and idempotency_key = %s"),
                (v["tenant_id"], v["idempotency_key"]),
            )
            row = cur.fetchone()
        res = _stamped_resolution(conn, row)
        return _envelope(conn, row, res, replay=True,
                         duplicate_of=str(row["duplicate_of_pin_id"]) if row["duplicate_of_pin_id"] else None)

    pin_id = str(inserted["pin_id"])

    # Resolve — trouble here never discards the capture. The savepoint
    # (conn.transaction() nested inside the request transaction) contains any
    # SQL failure to the resolution attempt, so the INSERT above still commits.
    try:
        with conn.transaction():
            res = _resolve(conn, pin_id)
    except Exception as exc:  # noqa: BLE001 - preserve the pin, report honestly
        res = {"ok": False, "resolution_method": None, "resolution_error": type(exc).__name__}

    with conn.cursor() as cur:
        cur.execute(_ROW_SQL.format(where="pin_id = %s::uuid"), (pin_id,))
        row = cur.fetchone()
    envelope = _envelope(conn, row, res if res.get("ok") else {}, replay=False, duplicate_of=duplicate_of)
    if not res.get("ok"):
        envelope["resolution"]["resolution_error"] = res.get("resolution_error", "resolver_failed")
    return envelope


def _stamped_resolution(conn, row: Dict[str, Any]) -> Dict[str, Any]:
    """Replay path: rebuild the resolution block from the STAMPED columns (no
    re-resolution on replay — the stored decision stands until /resolve)."""
    if row["resolution_method"] in ("ray_intersect", "nearest_parcel") and row["resolved_apn"]:
        # Re-read presentation facts through the reviewed read path.
        return _presented_from_stamp(conn, row)
    return {
        "ok": True, "apn": None, "county_fips": None, "owner": None, "owner_flag": None,
        "acres": None, "resolution_method": row["resolution_method"],
        "confidence": float(row["resolution_confidence"]) if row["resolution_confidence"] is not None else None,
        "in_crm": {"present": False},
    }


def _presented_from_stamp(conn, row: Dict[str, Any]) -> Dict[str, Any]:
    county, apn = row["resolved_county_fips"], row["resolved_apn"]
    with conn.cursor() as cur:
        cur.execute(
            "select owner_name, vintage_flag from landos.parcel_owner "
            "where county_fips = %s and parcel_apn = %s", (county, apn))
        o = cur.fetchone()
    if not o or not (o["owner_name"] or "").strip():
        owner, flag = "not on file", "not_on_file"
    elif o["vintage_flag"] == "govt":
        owner, flag = o["owner_name"], "govt"
    else:
        owner, flag = o["owner_name"], "private"
    with conn.cursor() as cur:
        cur.execute(
            "select jsonb_build_object('present', true, 'lead_id', lead_id, 'stage', stage, "
            "'card_state', card_state) as crm from public.acquisition_leads "
            "where county_fips = %s and parcel_apn = %s limit 1", (county, apn))
        c = cur.fetchone()
    return {
        "ok": True, "apn": apn, "county_fips": county, "owner": owner, "owner_flag": flag,
        "acres": None,
        "resolution_method": row["resolution_method"],
        "confidence": float(row["resolution_confidence"]) if row["resolution_confidence"] is not None else None,
        "in_crm": (c["crm"] if c else {"present": False}),
    }


def reresolve_pin(conn, pin_id: str) -> Dict[str, Any]:
    """POST /field-pins/{id}/resolve — re-run the reviewed ladder. This is how a
    dark-county pin resolves retroactively once parcel geometry lands (025 is an
    idempotent re-stamp). Unknown pin -> honest pin_not_found."""
    try:
        uuid.UUID(str(pin_id))
    except Exception as exc:  # noqa: BLE001
        raise FieldPinValidationError("pin_id must be a UUID") from exc
    try:
        with conn.transaction():
            res = _resolve(conn, str(pin_id))
    except Exception as exc:  # noqa: BLE001 - the stored pin is never at risk
        return {"ok": False, "error": "resolve_failed", "detail": type(exc).__name__}
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "resolve_failed")}
    with conn.cursor() as cur:
        cur.execute(_ROW_SQL.format(where="pin_id = %s::uuid"), (str(pin_id),))
        row = cur.fetchone()
    return _envelope(conn, row, res, replay=False,
                     duplicate_of=str(row["duplicate_of_pin_id"]) if row["duplicate_of_pin_id"] else None)


def get_pin(conn, pin_id: str) -> Dict[str, Any]:
    """GET /field-pins/{id} — the stored pin, its stamped resolution, plat and
    lake status. Read-only; never re-resolves."""
    try:
        uuid.UUID(str(pin_id))
    except Exception as exc:  # noqa: BLE001
        raise FieldPinValidationError("pin_id must be a UUID") from exc
    with conn.cursor() as cur:
        cur.execute(_ROW_SQL.format(where="pin_id = %s::uuid"), (str(pin_id),))
        row = cur.fetchone()
    if not row:
        return {"ok": False, "error": "pin_not_found"}
    res = _stamped_resolution(conn, row)
    return _envelope(conn, row, res, replay=False,
                     duplicate_of=str(row["duplicate_of_pin_id"]) if row["duplicate_of_pin_id"] else None)

"""
score_addresses.py — score a CSV of addresses with a named scoring profile and
write a ranked CSV with the base score, policy-tailwind multiplier, every overlay
hit (with its provenance flag), and the deal-killer gate flags.

    python -m scripts.score_addresses benton_harbor_edge data/benton_harbor_calibration.csv

Pipeline per row:
  1. resolve centroid (CSV lat/lng, else Census geocoder)
  2. four edge sub-scores: CSV overrides, else transparent geo-derived v1 formulas
  3. base_score = score_edge(...)                       (app/scoring.py)
  4. overlay hits via point-in-polygon                  (app/overlays.py)
  5. multiplier with the PROVENANCE GATE                (app/multiplier.py)
       - authoritative (e.g. OZ) -> applied; unconfirmed/stub -> logged at x1.0
  6. deal-killer gates                                  (app/gates.py)
  7. final_score = clamp(base * multiplier, 0, 100); 0 if any gate fires (KILLED)

v1 geo-derived sub-scores (documented; no rich parcel attrs available via REST):
  location_score    closeness to the downtown core + waterfront (distance decay)
  future_potential  0.40 base + 0.35 if in an authoritative QOZ + redevelopment proximity
  dev_fit           neutral 0.5 (LOW CONFIDENCE — pending zoning/lot data)
  prep_lift_inverse inverse of cleanup burden from EGLE brownfield proximity
Every formula input is emitted in the output CSV (dist_*_km, in_qoz, nearest_brownfield_m)
so the math is auditable; any sub-score can be overridden per row in the input CSV.
"""
import csv
import json
import math
import os
import sys
import urllib.parse
import urllib.request
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from app.scoring import score_edge, EDGE_DEFAULT_WEIGHTS, EDGE_SUBSCORES
from app.overlays import overlay_hits, _haversine_m
from app.multiplier import compute_multiplier
from app.gates import evaluate_gates, GATE_KEYS

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

# Documented geo anchors for the v1 geo-derived sub-scores (Benton Harbor, MI).
DOWNTOWN = (42.1163, -86.4541)     # E Main St core
WATERFRONT = (42.1235, -86.4885)   # Lake Michigan shoreline / Jean Klock Park

# Show this parcel as the calibration row before the ranked table.
CALIBRATION_FIRST = "1850 pipestone"

OUT_FIELDS = [
    "rank", "address", "apn", "base_score",
    "location_score", "future_potential", "dev_fit", "prep_lift_inverse",
    "multiplier", "final_score", "overlay_hits",
    "environmental_flag", "title_uncured", "land_bank_rofr", "occupied",
    "gates_failed", "status",
    "dist_downtown_km", "dist_waterfront_km", "in_qoz", "nearest_brownfield_m",
]


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _b(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "t") if v is not None else False


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def load_weights(cur, profile: str) -> Tuple[Dict[str, Any], bool]:
    cur.execute(
        "select weights from landos.scoring_weights "
        "where tenant_id=%s and profile=%s and active=true limit 1",
        (TENANT, profile),
    )
    row = cur.fetchone()
    if row and row["weights"]:
        w = row["weights"]
        return (json.loads(w) if isinstance(w, str) else w), True
    return EDGE_DEFAULT_WEIGHTS, False


def load_overlays(cur) -> List[Dict[str, Any]]:
    cur.execute(
        """
        select overlay_type, name, geometry, source, provenance_status,
               effective_date, multiplier_weight, buffer_m, minx, miny, maxx, maxy
        from landos.overlay_polygons
        where tenant_id = %s or tenant_id is null
        """,
        (TENANT,),
    )
    return cur.fetchall()


def geocode(addr: str) -> Tuple[float, float]:
    """Best-effort Census geocoder (used only when the CSV omits lat/lng)."""
    url = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress?" + urllib.parse.urlencode(
        {"address": addr, "benchmark": "Public_AR_Current", "format": "json"}
    )
    req = urllib.request.Request(url, headers={"User-Agent": "landos-engine/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    matches = d["result"]["addressMatches"]
    if not matches:
        raise ValueError(f"no geocode match for {addr!r}")
    c = matches[0]["coordinates"]
    return c["y"], c["x"]  # lat, lng


def _nearest_brownfield_m(lat: float, lng: float, overlays: List[Dict[str, Any]]) -> Optional[float]:
    best = None
    for ov in overlays:
        if ov["overlay_type"] != "brownfield":
            continue
        g = ov.get("geometry") or {}
        if g.get("type") != "Point":
            continue
        c = g["coordinates"]
        d = _haversine_m(lat, lng, c[1], c[0])
        best = d if best is None else min(best, d)
    return best


def derive_subscores(lat, lng, hits, overlays) -> Tuple[Dict[str, float], Dict[str, Any]]:
    in_qoz = any(
        h["overlay_type"] == "opportunity_zone" and h["provenance_status"] == "authoritative"
        for h in hits
    )
    dd = _haversine_m(lat, lng, *DOWNTOWN) / 1000.0
    dw = _haversine_m(lat, lng, *WATERFRONT) / 1000.0
    nb = _nearest_brownfield_m(lat, lng, overlays)
    nb_km = (nb / 1000.0) if nb is not None else float("inf")

    location = 0.6 * math.exp(-dd / 3.0) + 0.4 * math.exp(-dw / 5.0)
    future = 0.40 + (0.35 if in_qoz else 0.0) + 0.25 * math.exp(-nb_km / 1.0)
    dev = 0.5  # LOW CONFIDENCE neutral — no zoning/lot data via REST
    prep_inv = 1.0 - 0.6 * math.exp(-nb_km / 0.3)

    subs = {
        "location_score": round(_clamp01(location), 4),
        "future_potential": round(_clamp01(future), 4),
        "dev_fit": round(_clamp01(dev), 4),
        "prep_lift_inverse": round(_clamp01(prep_inv), 4),
    }
    audit = {
        "dist_downtown_km": round(dd, 3),
        "dist_waterfront_km": round(dw, 3),
        "in_qoz": in_qoz,
        "nearest_brownfield_m": (round(nb, 1) if nb is not None else ""),
    }
    return subs, audit


def score_row(r: Dict[str, Any], weights, overlays, as_of) -> Dict[str, Any]:
    addr = (r.get("address") or "").strip()
    apn = (r.get("apn") or "").strip()
    lat, lng = _f(r.get("lat")), _f(r.get("lng"))
    if lat is None or lng is None:
        lat, lng = geocode(addr)

    hits = overlay_hits(lat, lng, overlays)
    mult = compute_multiplier(hits, as_of=as_of)
    applied_keys = {(h["overlay_type"], h["name"]) for h in mult["applied"]}

    derived, audit = derive_subscores(lat, lng, hits, overlays)
    subs = dict(derived)
    for k in EDGE_SUBSCORES:  # per-row overrides
        ov = _f(r.get(k))
        if ov is not None:
            subs[k] = _clamp01(ov)

    edge = score_edge(
        {"base": subs, "distress": {"tax_delinquent": _b(r.get("tax_delinquent"))}}, weights
    )
    gate_flags = {k: _b(r.get(k)) for k in GATE_KEYS}
    gates = evaluate_gates(gate_flags)

    base = edge["base_score"]
    m = mult["multiplier"]
    final = 0.0 if gates["killed"] else round(max(0.0, min(100.0, base * m)), 2)

    hit_str = "; ".join(
        f'{h["overlay_type"]}:{h["name"]}:{h["provenance_status"]}:'
        f'{"applied" if (h["overlay_type"], h["name"]) in applied_keys else "logged"}'
        for h in hits
    ) or "none"

    out = {
        "address": addr, "apn": apn, "base_score": base,
        **{k: subs[k] for k in EDGE_SUBSCORES},
        "multiplier": m, "final_score": final, "overlay_hits": hit_str,
        **gate_flags,
        "gates_failed": ",".join(gates["gates_failed"]),
        "killed": gates["killed"],
        "status": "KILLED" if gates["killed"] else "scored",
        **audit,
    }
    return out


def _fmt_calibration(row: Dict[str, Any]) -> str:
    lines = [
        "================  CALIBRATION ROW  ================",
        f'  {row["address"]}   (APN {row["apn"] or "n/a"})',
        f'  BASE SCORE      {row["base_score"]:.2f}',
        f'    location_score    {row["location_score"]:.3f}',
        f'    future_potential  {row["future_potential"]:.3f}',
        f'    dev_fit           {row["dev_fit"]:.3f}   (low-confidence proxy)',
        f'    prep_lift_inverse {row["prep_lift_inverse"]:.3f}',
        f'  MULTIPLIER      x{row["multiplier"]:.2f}',
        f'  OVERLAY HITS    {row["overlay_hits"]}',
        f'  GATES           {row["gates_failed"] or "none"}   [{row["status"]}]',
        f'  FINAL SCORE     {row["final_score"]:.2f}',
        f'  (in_qoz={row["in_qoz"]}, downtown {row["dist_downtown_km"]}km, '
        f'waterfront {row["dist_waterfront_km"]}km, nearest_brownfield={row["nearest_brownfield_m"]}m)',
        "==================================================",
    ]
    return "\n".join(lines)


def main(profile: str, csv_path: str) -> None:
    as_of = date.today()
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            weights, from_db = load_weights(cur, profile)
            overlays = load_overlays(cur)

    if not from_db:
        print(f"NOTE: no active '{profile}' weights in DB — using EDGE_DEFAULT_WEIGHTS fallback.")
    print(f"Loaded {len(overlays)} overlay(s) for tenant.")

    with open(csv_path) as f:
        rows = [score_row(r, weights, overlays, as_of) for r in csv.DictReader(f)]

    scored = sorted((x for x in rows if not x["killed"]), key=lambda x: x["final_score"], reverse=True)
    killed = [x for x in rows if x["killed"]]
    for i, x in enumerate(scored, 1):
        x["rank"] = i
    for x in killed:
        x["rank"] = ""
    ranked = scored + killed

    out_path = os.path.splitext(os.path.abspath(csv_path))[0] + "_ranked.csv"
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ranked)

    # Calibration row first.
    cal = next((x for x in ranked if CALIBRATION_FIRST in x["address"].lower()), None)
    if cal:
        print("\n" + _fmt_calibration(cal) + "\n")

    print(f"================  RANKED ({len(ranked)})  ================")
    for x in ranked:
        rk = f'{x["rank"]:>2}' if x["rank"] != "" else " -"
        print(f'{rk}. {x["final_score"]:>6.2f}  base {x["base_score"]:>6.2f} x{x["multiplier"]:.2f}  '
              f'{x["status"]:<6} {x["address"]}')
    print("=" * 56)
    print(f"Ranked CSV written: {out_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python -m scripts.score_addresses <profile> <addresses.csv>")
        raise SystemExit(1)
    main(sys.argv[1], sys.argv[2])

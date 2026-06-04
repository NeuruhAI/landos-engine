"""
lead_queue.py — build a ranked lead queue from stacked signals through the
benton_harbor_edge scorer.

    python -m scripts.lead_queue benton_harbor_edge --county berrien

Pulls every parcel with >=1 signal (optionally filtered to a county via
signal_sources.county), resolves centroid/PIN, runs the benton_harbor_edge base
sub-scores + authoritative-OZ policy multiplier + deal-killer gates, then applies
a CORRELATION BOOST from the signal stack (count + distinct types, weighted by
signal strength). Ranks desc, writes a ranked CSV, prints the top 10. KILLED rows
(any gate fired) are segregated at the bottom.

final_score = clamp(base_score * overlay_multiplier * correlation_boost, 0, 100)
              (0 if any deal-killer gate fires)
"""
import argparse
import csv
import os
from collections import defaultdict
from datetime import date
from typing import Any, Dict, List

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from app.scoring import score_edge
from app.overlays import overlay_hits
from app.multiplier import compute_multiplier
from app.gates import evaluate_gates, GATE_KEYS
# reuse the geo-derived sub-score logic, loaders, and helpers from score_addresses
from scripts.score_addresses import (
    derive_subscores, load_weights, load_overlays, geocode, _f, _b, TENANT,
)

CORR_CAP = 1.40  # correlation boost ceiling
GATE_SEVERITY = {
    "environmental_flag": "curable-PhaseII",
    "title_uncured": "high",
    "land_bank_rofr": "high",
    "occupied": "medium",
}
OUT_FIELDS = [
    "rank", "address", "apn", "signal_types", "signal_count", "base_score",
    "multiplier", "correlation_boost", "final_score", "overlay_hits", "gate_flags",
]


def correlation_boost(sigs: List[Dict[str, Any]]):
    """Stacking layer: 1.0 for a lone signal; grows with signal count + distinct
    types (+ a small bump for cumulative strength), capped at CORR_CAP."""
    n = len(sigs)
    types = sorted({s["signal_type"] for s in sigs})
    strength = sum(float(s.get("signal_strength") or 0) for s in sigs)
    boost = 1.0 + 0.08 * (n - 1) + 0.12 * (len(types) - 1) + 0.05 * max(0.0, strength - 1.0)
    return round(min(CORR_CAP, max(1.0, boost)), 4), n, types


def load_signals(cur, county):
    q = [
        "select s.parcel_apn, s.address, s.lat, s.lng, s.signal_type, s.signal_strength,",
        "       s.signal_date, s.source_id, s.raw_meta, src.county",
        "from landos.signals s join landos.signal_sources src on src.source_id = s.source_id",
        "where s.tenant_id = %s",
    ]
    params: List[Any] = [TENANT]
    if county:
        q.append("and src.county ilike %s")
        params.append(county)
    cur.execute("\n".join(q), params)
    by_parcel = defaultdict(list)
    for r in cur.fetchall():
        by_parcel[(r["parcel_apn"] or r["address"] or "?")].append(r)
    return by_parcel


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("profile")
    ap.add_argument("--county", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    as_of = date.today()

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            weights, from_db = load_weights(cur, args.profile)
            overlays = load_overlays(cur)
            by_parcel = load_signals(cur, args.county)

    print(f"profile={args.profile}  weights={'db' if from_db else 'fallback'}  "
          f"overlays={len(overlays)}  parcels_with_signals={len(by_parcel)}")

    rows: List[Dict[str, Any]] = []
    skipped = 0
    for _key, sigs in by_parcel.items():
        rep = sigs[0]
        apn = rep["parcel_apn"] or ""
        addr = rep["address"] or ""
        lat, lng = _f(rep["lat"]), _f(rep["lng"])
        if lat is None or lng is None:
            try:
                lat, lng = geocode(addr)
            except Exception:  # noqa: BLE001 - cannot place this parcel; count + skip
                lat = lng = None
        if lat is None or lng is None:
            skipped += 1
            continue

        hits = overlay_hits(lat, lng, overlays)
        mult = compute_multiplier(hits, as_of=as_of)
        applied = {(h["overlay_type"], h["name"]) for h in mult["applied"]}
        subs, _audit = derive_subscores(lat, lng, hits, overlays)
        edge = score_edge(
            {"base": subs,
             "distress": {"tax_delinquent": any(s["signal_type"] == "tax_delinquent" for s in sigs)}},
            weights,
        )
        cb, scount, stypes = correlation_boost(sigs)

        # gate flags come from a source's raw_meta if it supplies them; the Tier-1
        # tax source carries none today -> all clear (title/environmental/occupancy
        # gate sources are a documented follow-on, not fabricated here).
        gate_flags = {k: any(_b((s.get("raw_meta") or {}).get(k)) for s in sigs) for k in GATE_KEYS}
        gates = evaluate_gates(gate_flags)

        base, m = edge["base_score"], mult["multiplier"]
        final = 0.0 if gates["killed"] else round(max(0.0, min(100.0, base * m * cb)), 2)
        hit_str = "; ".join(
            f'{h["overlay_type"]}:{h["name"]}:{h["provenance_status"]}:'
            f'{"applied" if (h["overlay_type"], h["name"]) in applied else "logged"}'
            for h in hits
        ) or "none"
        gate_bits = ([f"{k}:{GATE_SEVERITY[k]}" for k in gates["gates_failed"]]
                     + [f"{k}:{GATE_SEVERITY[k]}(flag)" for k in gates.get("flags", [])])
        gate_str = ",".join(gate_bits) or "none"

        rows.append({
            "address": addr, "apn": apn,
            "signal_types": "|".join(stypes), "signal_count": scount,
            "base_score": base, "multiplier": m, "correlation_boost": cb,
            "final_score": final, "overlay_hits": hit_str, "gate_flags": gate_str,
            "killed": gates["killed"],
        })

    scored = sorted((r for r in rows if not r["killed"]), key=lambda r: r["final_score"], reverse=True)
    killed = [r for r in rows if r["killed"]]
    for i, r in enumerate(scored, 1):
        r["rank"] = i
    for r in killed:
        r["rank"] = ""
    ranked = scored + killed

    out = args.out or os.path.join(
        os.path.expanduser("~/Documents/landos-engine/data"),
        f"lead_queue_{(args.county or 'all').lower()}.csv",
    )
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(ranked)

    print(f"\nscored={len(scored)}  killed={len(killed)}  skipped_no_coords={skipped}")
    print(f"================  TOP 10 — {args.profile} / county={args.county or 'all'}  ================")
    print(f"{'#':>2}  {'final':>6} {'base':>6} {'xOZ':>4} {'xcor':>4} {'sig':>3}  address")
    for r in ranked[:10]:
        rk = f'{r["rank"]:>2}' if r["rank"] != "" else " -"
        print(f'{rk}  {r["final_score"]:>6.2f} {r["base_score"]:>6.2f} {r["multiplier"]:>4.2f} '
              f'{r["correlation_boost"]:>4.2f} {r["signal_count"]:>3}  {r["address"][:42]}  '
              f'[{r["signal_types"]}] {r["overlay_hits"][:34]}')
    print("=" * 72)
    print(f"Ranked CSV: {out}")


if __name__ == "__main__":
    main()

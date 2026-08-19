"""
resolve_signal_parcel_identity.py — turn a real signal into a REAL parcel.

    python -m scripts.resolve_signal_parcel_identity [--json]

The nine in-territory estate-sale events carry the sale's own coordinates but
identify themselves as `estate-<listing_id>` — a stable idempotency anchor for
the SALE, not a parcel. Graduation policy v1 will not promote a signal past
OBSERVED_SIGNAL without a real parcel identity, and that is the correct refusal:
treating a listing id as a property is how a listing becomes a fake parcel.

NOTHING NEW IS BUILT. This drives the resolver that already exists:

    app/resolvers/address_to_apn.py :: AddressApnResolver

which takes a point, SPATIAL-INTERSECTS it against the county parcel layer, and
returns a PIN only when EXACTLY ONE parcel intersects. Zero or several is a
refusal with a reason, never a pick. Results cache in landos.address_apn_cache,
so a replay costs nothing and returns the same answer.

COUNTY COVERAGE IS REPORTED, NOT ASSUMED. The resolver is Berrien-specific
(Parcels_Future). St. Joseph IN (18141) has no entry in PROVIDER_CHAINS at all —
every configured chain is 26xxx — so its signals resolve to NO_RESOLVER_FOR_COUNTY
and stay OBSERVED_SIGNAL. That is a coverage gap to close, not a row to guess at.

Read-only: this writes nothing but the resolver's own cache.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import psycopg

from app.config import DATABASE_URL
from app.resolvers.address_to_apn import PARCELS_QUERY, AddressApnResolver

TENANT = os.environ.get("TENANT_ID", "1840ac90-54bd-4b3d-8baf-227c8d84ab5a")
OUT_DIR = REPO / "data" / "signal_identity_runs"

#: county_slug -> the resolver that is actually configured for it.
RESOLVER_BY_COUNTY = {"berrien-mi": "berrien_parcels_spatial_intersect"}
COUNTY_FIPS = {"berrien-mi": "26021", "st-joseph-in": "18141"}

#: Berrien publishes placeholder polygons for water, right-of-way and unassigned
#: area. A point can legitimately intersect one, and it is NOT a property —
#: accepting it would mint a parcel that owns nothing.
#:
#: The shape was verified against the layer rather than guessed
#: (2026-08-19). PIN 99-56-9999-9999-99-9 returns Acres 0.0 with no
#: Propaddrnu, no AltPin and no owner fields, while a real neighbour
#: 11-02-0014-0015-03-0 returns Acres 8.0, AltPin 15-03 and Propaddrnu 9250.
#: The tell is the 99 jurisdiction prefix and the 9999-9999 block.
SENTINEL_PIN = re.compile(r"^99-|-9999-9999-")

#: geo_precision='exact' ONLY. A spatial intersect from a CITY CENTROID is not
#: identity resolution — three Niles listings share one centroid and would all
#: "resolve" to whichever parcel happens to sit under the town marker. Measured
#: 2026-08-19 before this guard existed: six centroid rows produced parcel PINs,
#: two of them Berrien's all-9s sentinel. Those were fabricated identities.
SIGNALS_SQL = """
select listing_id, city, state, postal_code, county_slug, street_address,
       lat, lng, geo_precision, provenance, last_seen_at
  from recipe.sale_events
 where in_territory and lat is not null and lng is not null
   and geo_precision = 'exact'
 order by listing_id
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="signal -> real parcel identity")
    ap.add_argument("--json", action="store_true", help="print the receipt body")
    args = ap.parse_args()

    run_id = f"signal-identity-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    results = []

    with psycopg.connect(DATABASE_URL) as conn:
        cur = conn.cursor()
        cur.execute(SIGNALS_SQL)
        signals = cur.fetchall()
        resolver = AddressApnResolver(cur, TENANT)

        for (listing_id, city, state, postal, county_slug, street,
             lat, lng, geo, provenance, last_seen) in signals:
            record = {
                "SIGNAL_ID": listing_id,
                "STATE": state,
                "COUNTY": county_slug,
                "COUNTY_FIPS": COUNTY_FIPS.get(county_slug),
                "SOURCE_ADDRESS": street or f"{city}, {state} {postal}".strip(),
                "SOURCE_LAT": float(lat),
                "SOURCE_LNG": float(lng),
                "SOURCE_GEO_PRECISION": geo,
                "OBSERVED_AT": str(last_seen),
            }
            resolver_name = RESOLVER_BY_COUNTY.get(county_slug)
            if not resolver_name:
                results.append({
                    **record,
                    "IDENTITY_RESOLVER": None,
                    "REAL_PARCEL_APN": None,
                    "PARCEL_MATCH_COUNT": None,
                    "PARCEL_MATCH_CONFIDENCE": None,
                    "PARCEL_PROVENANCE": None,
                    "IDENTITY_VERDICT": "NO_RESOLVER_FOR_COUNTY",
                    "DETAIL": f"no parcel resolver configured for {county_slug}",
                })
                continue

            out = resolver.resolve(lat=float(lat), lng=float(lng))
            reason = out.get("reason") or ""
            # The resolver states the count in its reason; keep its words.
            apn = out.get("apn")
            if apn and SENTINEL_PIN.match(str(apn).strip()):
                results.append({
                    **record, "IDENTITY_RESOLVER": resolver_name,
                    "REAL_PARCEL_APN": None, "PARCEL_MATCH_COUNT": 1,
                    "PARCEL_MATCH_CONFIDENCE": None, "PARCEL_PROVENANCE": None,
                    "IDENTITY_VERDICT": "SENTINEL_PARCEL_REJECTED",
                    "DETAIL": f"intersected the county's catch-all parcel {apn}",
                })
                continue
            if apn:
                verdict, count = "RESOLVED", 1
            elif "ambiguous" in reason:
                verdict, count = "AMBIGUOUS_MULTIPLE_PARCELS", None
            elif "no parcel intersects" in reason:
                verdict, count = "NO_PARCEL_AT_POINT", 0
            else:
                verdict, count = "RESOLVER_UNAVAILABLE", None

            results.append({
                **record,
                "IDENTITY_RESOLVER": resolver_name,
                "REAL_PARCEL_APN": out.get("apn"),
                "PARCEL_MATCH_COUNT": count,
                "PARCEL_MATCH_CONFIDENCE": out.get("confidence"),
                "PARCEL_PROVENANCE": f"{PARCELS_QUERY} · {reason}" if out.get("apn") else None,
                "IDENTITY_VERDICT": verdict,
                "DETAIL": reason,
            })
        conn.commit()          # the resolver's cache writes ride this transaction

    tally = Counter(r["IDENTITY_VERDICT"] for r in results)
    receipt = {
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "signals_total": len(results),
        "parcel_identities_resolved": tally.get("RESOLVED", 0),
        "verdicts": dict(tally),
        "results": results,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{run_id}.json"
    path.write_text(json.dumps(receipt, indent=2, default=str), encoding="utf-8")

    for r in results:
        print(f"  {r['SIGNAL_ID']}  {r['COUNTY']:<14} {r['IDENTITY_VERDICT']:<26} "
              f"apn={r['REAL_PARCEL_APN'] or '-'}  {r['DETAIL'][:46]}")
    print()
    print(json.dumps({"signals_total": len(results),
                      "parcel_identities_resolved": tally.get("RESOLVED", 0),
                      "verdicts": dict(tally), "receipt": str(path)}, indent=2))
    if args.json:
        print(json.dumps(receipt, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

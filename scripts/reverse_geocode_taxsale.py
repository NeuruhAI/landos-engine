"""
reverse_geocode_taxsale.py — CC2 step 4c (Phase 1): SITUS address backfill.

    python -m scripts.reverse_geocode_taxsale [--dry-run]

The 87 discovered tax-sale leads have centroid coords (step 4b) but situs_address NULL (the
county layer's address fields were blank). This reverse-geocodes each centroid to a street
address via Nominatim and writes owner_leads.situs_address (owner_leads has only the one
situs_address column, so the full "123 Road, City, ST zip" string goes there).

$0 — Nominatim is free/keyless. NO paid geocoder, NO ParcelProviderChain. stdlib urllib,
honest UA, >=1.1s spacing (their 1 req/sec policy), drop-not-fight on a 429/block.

NB: this is the SITUS (property) address APPROXIMATED from the parcel centroid — good enough to
label the pin. The AUTHORITATIVE situs + the OWNER MAILING address (the real path to mail these
absentee owners) live on the county assessor record (raw_meta PROVALWBLK / TAXINFOURL) — that is
a separate scrape pass (step 4d), not this.

Touches ONLY the 87 (provenance gate). Idempotent: the situs_address IS NULL guard -> re-run
geocodes 0. Honest NULL if the reverse returns nothing usable — never a fabricated address.
"""
import argparse
import json
import time
import urllib.parse
import urllib.request

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

PROVENANCE = "discovered_taxsale"
UA = "landos-engine/1.0 (+situs-reverse-geocode; contact jeramieshicks@gmail.com)"
SPACING_S = 1.2   # Nominatim usage policy is <=1 req/sec; 1.2s is courteous


def reverse(lat, lng):
    """Centroid -> 'house road, city, ST zip' (as much as resolves), else None (honest NULL)."""
    params = {"format": "jsonv2", "lat": lat, "lon": lng, "addressdetails": 1}
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    if "error" in d:                          # rate/auth wall -> drop, don't fight
        raise SystemExit(f"Nominatim wall (drop-not-fight): {d['error']}")
    a = d.get("address") or {}
    iso = a.get("ISO3166-2-lvl4") or ""
    state = iso.split("-")[-1] if iso else a.get("state")
    street = " ".join(x for x in [a.get("house_number"), a.get("road")] if x) or None
    city = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet")
    tail = " ".join(x for x in [state, a.get("postcode")] if x) or None
    if not (street or city):                  # nothing usable -> honest NULL
        return None
    return ", ".join(p for p in [street, city, tail] if p)


def main(dry_run=False):
    geocoded = stayed_null = 0
    preview = []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("select lead_id, parcel_apn, situs_lat, situs_lng from landos.owner_leads "
                    "where provenance=%s and situs_lat is not null and situs_address is null "
                    "order by parcel_apn", (PROVENANCE,))
        targets = cur.fetchall()
        for i, t in enumerate(targets):
            if i:
                time.sleep(SPACING_S)
            addr = reverse(float(t["situs_lat"]), float(t["situs_lng"]))
            if not addr:
                stayed_null += 1
                continue
            preview.append((t["parcel_apn"], addr))
            if dry_run:
                continue
            cur.execute("update landos.owner_leads set situs_address=%s "
                        "where lead_id=%s and provenance=%s and situs_address is null",
                        (addr, t["lead_id"], PROVENANCE))
            geocoded += cur.rowcount

        if dry_run:
            conn.rollback()
            print(f"\n=== REVERSE-GEOCODE DRY-RUN — {len(preview)} resolved, {stayed_null} NULL. "
                  f"NO writes. ===")
            for apn, addr in preview[:10]:
                print(f"  {apn}  {addr}")
            if len(preview) > 10:
                print(f"  … +{len(preview) - 10} more")
            return
        conn.commit()

    print(f"\nreverse-geocode summary: targets={len(targets)} geocoded={geocoded} "
          f"stayed_null={stayed_null} (SITUS approximated from parcel centroid; authoritative "
          f"situs + owner-mailing = county assessor record, step 4d). "
          f"Next: python -m scripts.sync_to_crm")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="CC2 step 4c Phase 1 — situs reverse-geocode (Nominatim, free)")
    ap.add_argument("--dry-run", action="store_true", help="resolve + preview; write nothing")
    main(dry_run=ap.parse_args().dry_run)

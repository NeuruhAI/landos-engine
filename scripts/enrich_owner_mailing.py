"""
enrich_owner_mailing.py — CC2 step 4d: OWNER MAILING from the county parcel layer.

    python -m scripts.enrich_owner_mailing [--dry-run]

The path to actually MAIL the 87 discovered tax-sale owners. The county assessor pages in
raw_meta (PROVALWBLK / TAXINFOURL) are client-rendered SPAs (no $0 scrape), but the county's
ArcGIS parcel layer (Parcel25 — same org as the 4b cert-sale layer) carries the owner name +
MAILING address as queryable attributes. This reads the mailing address from there and writes
owner_leads.owner_mailing_address / _city / _state / _zip.

$0 — free public ArcGIS REST, no token. stdlib urllib, honest UA, >=1.2s spacing across chunks,
drop-not-fight on an auth wall. Touches ONLY the 87 (provenance gate). Idempotent
(owner_mailing_address IS NULL guard). Honest NULL where the layer has no mailing.

NB: most of these tax-sale owners mail from OUT OF STATE (Nashville TN, Vandalia MI, ...) — i.e.
absentee. sync_to_crm.py surfaces that on the card (parcel_meta.owner_mailing + owner_out_of_state).
PROP_ADDR (authoritative situs) is blank in the county layer for these parcels, so the 4c
reverse-geocoded situs stands.
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
LAYER = ("https://services.arcgis.com/OjftlhRHkAABcyiF/arcgis/rest/services/"
         "Parcel25/FeatureServer/0/query")
UA = "landos-engine/1.0 (+owner-mailing-enrich; contact jeramieshicks@gmail.com)"
CHUNK = 40        # APNs per query (keeps the GET URL well under limits)
SPACING_S = 1.2


def _clean(v):
    v = str(v).strip() if v is not None else ""
    return v or None


def _query(apns):
    where = "PARCELSTAT IN (" + ",".join("'%s'" % a.replace("'", "''") for a in apns) + ")"
    params = {"where": where,
              "outFields": "PARCELSTAT,MAILINGADD,MAILINGA_1,MAILINGCIT,MAILINGSTA,MAILINGZIP",
              "returnGeometry": "false", "f": "json"}
    req = urllib.request.Request(LAYER + "?" + urllib.parse.urlencode(params),
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.load(r)
    if "error" in d:                          # token/auth wall -> drop, don't fight
        raise SystemExit(f"ArcGIS error (drop-not-fight): {d['error']}")
    return d.get("features", [])


def main(dry_run=False):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("select lead_id, parcel_apn from landos.owner_leads "
                    "where provenance=%s and owner_mailing_address is null order by parcel_apn",
                    (PROVENANCE,))
        targets = cur.fetchall()
        apns = [t["parcel_apn"] for t in targets]

        mailing = {}
        for i in range(0, len(apns), CHUNK):
            if i:
                time.sleep(SPACING_S)
            for f in _query(apns[i:i + CHUNK]):
                a = f["attributes"]
                ps = a.get("PARCELSTAT")
                addr = ", ".join(x for x in [_clean(a.get("MAILINGADD")),
                                             _clean(a.get("MAILINGA_1"))] if x) or None
                if ps and addr:
                    mailing[ps] = {"address": addr, "city": _clean(a.get("MAILINGCIT")),
                                   "state": _clean(a.get("MAILINGSTA")), "zip": _clean(a.get("MAILINGZIP"))}
        print(f"  targets={len(targets)} fetched_mailing={len(mailing)}")

        enriched = stayed_null = 0
        preview = []
        for t in targets:
            m = mailing.get(t["parcel_apn"])
            if not m:
                stayed_null += 1
                continue
            preview.append((t["parcel_apn"], m))
            if dry_run:
                continue
            cur.execute(
                "update landos.owner_leads set owner_mailing_address=%s, owner_mailing_city=%s, "
                "owner_mailing_state=%s, owner_mailing_zip=%s "
                "where lead_id=%s and provenance=%s and owner_mailing_address is null",
                (m["address"], m["city"], m["state"], m["zip"], t["lead_id"], PROVENANCE))
            enriched += cur.rowcount

        oos = sum(1 for _, m in preview if (m["state"] or "").strip().upper() not in ("IN", ""))
        if dry_run:
            conn.rollback()
            print(f"\n=== OWNER-MAILING DRY-RUN — {len(preview)} resolved ({oos} out-of-state), "
                  f"{stayed_null} NULL. NO writes. ===")
            for apn, m in preview[:8]:
                print(f"  {apn}  {m['address']}, {m['city']}, {m['state']} {m['zip']}")
            if len(preview) > 8:
                print(f"  … +{len(preview) - 8} more")
            return
        conn.commit()

    print(f"\nowner-mailing summary: targets={len(targets)} enriched={enriched} "
          f"stayed_null={stayed_null} out_of_state_absentee={oos} (the path to mail these owners). "
          f"Next: python -m scripts.sync_to_crm")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="CC2 step 4d — owner mailing from the county parcel layer (free ArcGIS)")
    ap.add_argument("--dry-run", action="store_true", help="resolve + preview; write nothing")
    main(dry_run=ap.parse_args().dry_run)

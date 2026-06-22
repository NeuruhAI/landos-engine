"""
mint_taxsale_leads.py — CC2 step 4: the NO-SPEND MINTER (AXON Emergence Loop).

    python -m scripts.mint_taxsale_leads [--dry-run]

Mints the St. Joseph county tax-sale parcels (loaded by scripts/load_signals.py) into
landos.owner_leads straight from the staged county data already in
discovery_signals.raw_payload, then matches each tax-sale signal to its new lead so the
emergence scorer can light it.

$0. Pure DB read -> compute -> DB write. NO network, NO API, NO ParcelProviderChain.

These are NEW inventory (certificate-sale parcels NOT in the curated 136). DISCOVERED, not
underwritten: the county layer gives no situs street / lat-lng / mailing, so those land as
honest NULL (never fabricated). final_lead_score stays NULL (no composite run); emerging_score
is the ONLY score they get, written next by score_emergence.py.

Idempotent: ON CONFLICT (tenant_id, county_fips, parcel_apn) DO NOTHING — re-run mints 0 dups;
only signals still status='new' are (re)matched, so re-run re-matches 0. The whole batch is one
DELETE (provenance='discovered_taxsale', source='county_taxsale_18141') if it must be removed.
"""
import argparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.score_absentee import classify_owner_type   # the EXISTING entity/individual classifier

APG_TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"   # St. Joseph 18141 is APG territory
SOURCE = "county_taxsale_18141"
PROVENANCE = "discovered_taxsale"

SIGNALS = """
    select signal_id, parcel_apn, owner_name, confidence, county_fips,
           raw_payload, status, matched_lead_id
    from landos.discovery_signals
    where source_type = 'taxsale' and signal_class = 'temporal'
    order by parcel_apn
"""

# Honest NULLs in the VALUES: situs_address / situs_lat / situs_lng / owner_mailing_address /
# absentee_flag / absentee_score / final_lead_score / emerging_score. discovery_seed=false (real
# parcels). emerging_score is written by the scorer, not the minter.
MINT = """
    insert into landos.owner_leads
        (tenant_id, county_fips, parcel_apn, owner_name, land_use_class, acreage,
         owner_type, situs_address, situs_lat, situs_lng, owner_mailing_address,
         absentee_flag, absentee_score, final_lead_score, emerging_score,
         discovery_seed, source, provenance, confidence_score, raw_meta)
    values (%(tenant_id)s, %(county_fips)s, %(parcel_apn)s, %(owner_name)s,
            %(land_use_class)s, %(acreage)s, %(owner_type)s,
            null, null, null, null,
            null, null, null, null,
            false, %(source)s, %(provenance)s, %(confidence_score)s, %(raw_meta)s)
    on conflict (tenant_id, county_fips, parcel_apn) do nothing
    returning lead_id
"""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main(dry_run=False):
    minted = skipped_conflict = signals_matched = 0
    preview = []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(SIGNALS)
        signals = cur.fetchall()
        for s in signals:
            rp = s["raw_payload"] or {}
            owner_name = s["owner_name"]
            row = {
                "tenant_id": APG_TENANT,
                "county_fips": s["county_fips"],
                "parcel_apn": s["parcel_apn"],
                "owner_name": owner_name,
                "land_use_class": (rp.get("raw") or {}).get("PROPTYPE") or rp.get("prop_type"),
                "acreage": _num(rp.get("acreage")),
                "owner_type": classify_owner_type(owner_name),
                "source": SOURCE,
                "provenance": PROVENANCE,
                "confidence_score": s["confidence"],
                "raw_meta": Jsonb(rp),
            }
            if dry_run:
                preview.append((row["parcel_apn"], owner_name, row["owner_type"],
                                row["land_use_class"], row["acreage"]))
                continue
            cur.execute(MINT, row)
            got = cur.fetchone()
            if got:
                lead_id = got["lead_id"]
                minted += 1
            else:
                # parcel already an owner_lead — find it so the signal still matches (idempotent)
                cur.execute("select lead_id from landos.owner_leads "
                            "where tenant_id=%s and county_fips=%s and parcel_apn=%s",
                            (APG_TENANT, s["county_fips"], s["parcel_apn"]))
                lead_id = cur.fetchone()["lead_id"]
                skipped_conflict += 1
            # match the SOURCE signal to its lead — only if not already matched (re-run matches 0)
            if s["status"] != "matched" or s["matched_lead_id"] is None:
                cur.execute("update landos.discovery_signals "
                            "set matched_lead_id=%s, status='matched' where signal_id=%s",
                            (lead_id, s["signal_id"]))
                signals_matched += cur.rowcount

        if dry_run:
            conn.rollback()
            print(f"\n=== MINT DRY-RUN — {len(signals)} taxsale signal(s) would mint, NO writes ===")
            for apn, own, ot, luc, ac in preview[:10]:
                print(f"  {apn}  {own!r}  [{ot}]  {luc}  {ac} ac")
            if len(preview) > 10:
                print(f"  … +{len(preview) - 10} more")
            print("\nHARD STOP — dry-run only. Re-run without --dry-run to mint.")
            return
        conn.commit()

    print(f"\nmint summary: minted={minted} skipped_conflict={skipped_conflict} "
          f"signals_matched={signals_matched} "
          f"(tenant=APG, source={SOURCE!r}, provenance={PROVENANCE!r}; "
          f"situs / lat-lng / mailing / absentee / final_lead_score = honest NULL).")
    print("Next: `python -m scripts.score_emergence` then `python -m scripts.sync_to_crm`.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CC2 step 4 — no-spend minter (taxsale -> owner_leads)")
    ap.add_argument("--dry-run", action="store_true", help="count + preview; write nothing")
    main(dry_run=ap.parse_args().dry_run)

"""
mint_taxsale_inmarket.py — IN-MARKET-gated tax-sale minter (AXON cycle, additive).

    python -m scripts.mint_taxsale_inmarket [--dry-run]

Why this exists: the shipped scripts/mint_taxsale_leads.py is county-AGNOSTIC — it mints EVERY
taxsale+temporal signal in landos.discovery_signals. That was safe when only St. Joseph (18141)
was loaded. CC3 has since staged free out-of-market county sales as a SCALE-PROOF (Delaware 18035
/ Muncie, Vanderburgh 18163 / Evansville — 1203 signals); per the firehose directive those are
"do NOT mint (out-of-market)". This minter mints ONLY the in-market county allowlist, so the daily
cycle keeps the real board (St. Joseph etc.) fresh without flooding it with out-of-market parcels.

Reuses the shipped minter's exact write path (INSERT, owner-type classifier, tenant/source/
provenance, signal-match) — it ONLY adds a county_fips allowlist to the signal selection, so the
minted rows are byte-identical to what mint_taxsale_leads would produce for those counties. The
shipped minter is left untouched (additive-only). Idempotent (ON CONFLICT DO NOTHING; re-match 0).

IN-MARKET = APG's actual footprint. Extend INMARKET_COUNTIES when APG enters a new market; do NOT
add a county here just because CC3 can pull it (that is the scale-proof, not the buy-box).
"""
import argparse

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.score_absentee import classify_owner_type
from scripts.mint_taxsale_leads import APG_TENANT, SOURCE, PROVENANCE, MINT, _num

# St. Joseph County, IN (18141, APG territory) + Berrien County, MI (26021, curated set + Belinda).
INMARKET_COUNTIES = {"18141", "26021"}

SIGNALS_INMARKET = """
    select signal_id, parcel_apn, owner_name, confidence, county_fips,
           raw_payload, status, matched_lead_id
    from landos.discovery_signals
    where source_type = 'taxsale' and signal_class = 'temporal'
      and county_fips = any(%s)
    order by parcel_apn
"""


def main(dry_run=False):
    minted = skipped_conflict = signals_matched = 0
    preview = []
    counties = sorted(INMARKET_COUNTIES)
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(SIGNALS_INMARKET, (counties,))
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
                preview.append((row["county_fips"], row["parcel_apn"], owner_name, row["owner_type"]))
                continue
            cur.execute(MINT, row)
            got = cur.fetchone()
            if got:
                lead_id = got["lead_id"]
                minted += 1
            else:
                cur.execute("select lead_id from landos.owner_leads "
                            "where tenant_id=%s and county_fips=%s and parcel_apn=%s",
                            (APG_TENANT, s["county_fips"], s["parcel_apn"]))
                lead_id = cur.fetchone()["lead_id"]
                skipped_conflict += 1
            if s["status"] != "matched" or s["matched_lead_id"] is None:
                cur.execute("update landos.discovery_signals "
                            "set matched_lead_id=%s, status='matched' where signal_id=%s",
                            (lead_id, s["signal_id"]))
                signals_matched += cur.rowcount

        if dry_run:
            conn.rollback()
            print(f"\n=== IN-MARKET MINT DRY-RUN — counties={counties}, {len(signals)} signal(s), NO writes ===")
            for cf, apn, own, ot in preview[:10]:
                print(f"  [{cf}] {apn}  {own!r}  [{ot}]")
            if len(preview) > 10:
                print(f"  … +{len(preview) - 10} more")
            return
        conn.commit()

    print(f"\nin-market mint summary: counties={counties} signals={len(signals)} "
          f"minted={minted} skipped_conflict={skipped_conflict} signals_matched={signals_matched} "
          f"(out-of-market taxsale signals are intentionally left unminted — scale-proof only).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="in-market-gated taxsale minter (additive)")
    ap.add_argument("--dry-run", action="store_true", help="count + preview; write nothing")
    main(dry_run=ap.parse_args().dry_run)

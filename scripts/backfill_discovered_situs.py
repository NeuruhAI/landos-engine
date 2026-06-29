"""
backfill_discovered_situs.py — situs_address for discovered leads that ship one (AXON, additive).

    python -m scripts.backfill_discovered_situs [--dry-run]

The St. Joseph 87 had blank county addresses (handled by reverse_geocode_taxsale.py). The newer
county layers (Delaware, Vanderburgh, future TaxSaleResources) DO carry a property address in
raw_meta.raw — so we can write situs_address straight from the staged data, $0 and with NO
network/reverse-geocode at all. Pulls from the common field shapes seen across county layers:
  * single combined field:  Property_Address
  * split fields:           USER_Property_Street (+ City/Zip)  /  StAddr + City + Postal
Whitespace is normalized. Touches ONLY provenance LIKE 'discovered%' rows with situs_address
NULL (idempotent). Honest NULL when the layer carried no usable address — never fabricated.
"""
import argparse
import re

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL


def _norm(v):
    if v is None:
        return None
    s = re.sub(r"\s+", " ", str(v)).strip()
    return s or None


def compose_situs(rm):
    """owner_leads.raw_meta (== the staged detail_json) -> 'street, city ST zip' or None."""
    if not rm:
        return None
    raw = rm.get("raw") or {}
    # 1) combined field (Vanderburgh: 'Property_Address')
    combined = _norm(raw.get("Property_Address") or raw.get("PropertyAddress") or rm.get("situs_address"))
    if combined:
        return combined
    # 2) split fields (Delaware: USER_Property_Street/City/Zip; or Esri StAddr/City/Postal)
    street = _norm(raw.get("USER_Property_Street") or raw.get("StAddr") or raw.get("Property_Street"))
    city = _norm(raw.get("USER_Property_City") or raw.get("City") or raw.get("Property_City"))
    zp = _norm(raw.get("USER_Property_Zip") or raw.get("Postal") or raw.get("Property_Zip"))
    if not (street or city):
        return None
    tail = " ".join(x for x in [city, zp] if x) or None
    return ", ".join(p for p in [street, tail] if p)


def main(dry_run=False):
    written = stayed_null = 0
    preview = []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute("select lead_id, parcel_apn, raw_meta from landos.owner_leads "
                    "where provenance like 'discovered%' and situs_address is null "
                    "and raw_meta is not null")
        targets = cur.fetchall()
        for t in targets:
            addr = compose_situs(t["raw_meta"])
            if not addr:
                stayed_null += 1
                continue
            preview.append((t["parcel_apn"], addr))
            if dry_run:
                continue
            cur.execute("update landos.owner_leads set situs_address=%s "
                        "where lead_id=%s and situs_address is null", (addr, t["lead_id"]))
            written += cur.rowcount
        if dry_run:
            conn.rollback()
            print(f"\n=== SITUS-BACKFILL DRY-RUN — {len(preview)} would write, {stayed_null} stay NULL. ===")
            for apn, addr in preview[:10]:
                print(f"  {apn}  {addr}")
            if len(preview) > 10:
                print(f"  … +{len(preview) - 10} more")
            return
        conn.commit()
    print(f"\nsitus-backfill summary: targets={len(targets)} written={written} "
          f"stayed_null={stayed_null} ($0, from staged raw_meta). Next: sync_to_crm")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="situs_address backfill from staged raw_meta (additive)")
    ap.add_argument("--dry-run", action="store_true", help="compose + preview; write nothing")
    main(dry_run=ap.parse_args().dry_run)

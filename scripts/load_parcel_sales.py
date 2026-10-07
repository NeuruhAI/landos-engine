"""
load_parcel_sales.py — load a county's recorded-sale POOL into landos.parcel_sale (the
Valuation Lab comp fabric). Source-agnostic via a per-county colmap, same doctrine as
scripts/load_parcel_owner.py. ENGINE-side (the DB write); landos-ingest only STAGES the file.

    python -m scripts.load_parcel_sales --county cass                 # dry-run (counts, no write)
    python -m scripts.load_parcel_sales --county cass --apply         # upsert

Idempotent on the (county_fips, parcel_apn, sale_date, sale_price) natural key. MISSING stays
NULL, never 0. Never fabricates a sale or a price. A price of 0/blank is NOT a sale -> skipped
(a $0 quitclaim is not arms-length; the selector also floors $/acre).
"""
import argparse
import csv
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, Optional

import psycopg
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL

TENANT_AGNOSTIC = None  # public record — tenant_id stays NULL

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MSCOUT = os.path.abspath(os.path.join(REPO, "..", "mineral-scout"))

# per-county staged source + column map. A new county = a new entry, never a rewrite.
SOURCES: Dict[str, Dict[str, Any]] = {
    "cass": {
        "county_fips": "26027",
        "csv": os.path.join(MSCOUT, "data", "cass", "cass_comps.csv"),
        "manifest": os.path.join(MSCOUT, "manifests", "cass_comps.json"),
        "source": "cass_arcgis:Cassopolis_Area_Land_Data",
        "colmap": {
            "parcel_apn": "Parcel_Num", "owner_name": "Owner", "class_code": "Class",
            "acreage": "Acreage", "sev": "State_Equa", "taxable_value": "Taxable_Va",
            "sale_price": "Sale_Price", "sale_date": "Sale_Date",
            "deed_liber": "Liber", "deed_page": "Page",
        },
    },
}


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    s = str(v).replace(",", "").replace("$", "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _date(v: Any) -> Optional[str]:
    """MM/DD/YYYY (the AGOL export form) -> ISO date; anything unparseable -> None."""
    s = (v or "").strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _txt(v: Any) -> Optional[str]:
    s = (v or "").strip()
    return s or None


UPSERT = """
    insert into landos.parcel_sale
      (tenant_id, county_fips, parcel_apn, sale_price, sale_date, acreage, class_code,
       sev, taxable_value, deed_liber, deed_page, owner_name, source, provenance)
    values (%(tenant)s, %(fips)s, %(apn)s, %(price)s, %(date)s, %(acre)s, %(cls)s,
            %(sev)s, %(tax)s, %(liber)s, %(page)s, %(owner)s, %(source)s, %(prov)s)
    on conflict (county_fips, parcel_apn, sale_date, sale_price) do update set
      acreage = excluded.acreage, class_code = excluded.class_code, sev = excluded.sev,
      taxable_value = excluded.taxable_value, deed_liber = excluded.deed_liber,
      deed_page = excluded.deed_page, owner_name = excluded.owner_name,
      source = excluded.source, provenance = excluded.provenance
"""


def load(county: str, apply: bool, csv_override: Optional[str] = None,
         manifest_override: Optional[str] = None) -> Dict[str, int]:
    cfg = dict(SOURCES.get(county) or {})
    if not cfg:
        raise SystemExit(f"unknown county {county!r}. Known: {', '.join(SOURCES)}")
    if csv_override:
        cfg["csv"] = csv_override
    if manifest_override:
        cfg["manifest"] = manifest_override
    fips, cm = cfg["county_fips"], cfg["colmap"]
    prov = {"loader": "load_parcel_sales", "source": cfg["source"]}
    if os.path.exists(cfg.get("manifest", "")):
        with open(cfg["manifest"]) as fh:
            man = json.load(fh)
        prov["manifest_sha256"] = man.get("sha256")
        prov["source_url"] = man.get("source_url")

    read = kept = skipped_noprice = skipped_noapn = 0
    rows = []
    with open(cfg["csv"], newline="") as fh:
        for r in csv.DictReader(fh):
            read += 1
            apn = _txt(r.get(cm["parcel_apn"]))
            price = _num(r.get(cm["sale_price"]))
            if not apn:
                skipped_noapn += 1
                continue
            if not price or price <= 0:      # a $0/blank transfer is not an arms-length sale
                skipped_noprice += 1
                continue
            rows.append({
                "tenant": TENANT_AGNOSTIC, "fips": fips, "apn": apn, "price": price,
                "date": _date(r.get(cm["sale_date"])), "acre": _num(r.get(cm["acreage"])),
                "cls": _txt(r.get(cm["class_code"])), "sev": _num(r.get(cm["sev"])),
                "tax": _num(r.get(cm["taxable_value"])),
                "liber": _txt(r.get(cm.get("deed_liber"))), "page": _txt(r.get(cm.get("deed_page"))),
                "owner": _txt(r.get(cm["owner_name"])), "source": cfg["source"],
                "prov": Jsonb(prov),
            })
            kept += 1

    stats = {"read": read, "kept": kept, "skipped_noprice": skipped_noprice,
             "skipped_noapn": skipped_noapn, "written": 0}
    if apply and rows:
        with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
            cur.execute("set statement_timeout = '180s'")
            cur.executemany(UPSERT, rows)
            stats["written"] = cur.rowcount
            conn.commit()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="load a county recorded-sale pool -> landos.parcel_sale")
    ap.add_argument("--county", required=True, help=f"one of: {', '.join(SOURCES)}")
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run counts only)")
    ap.add_argument("--csv", help="override the staged CSV path (else the SOURCES default)")
    ap.add_argument("--manifest", help="override the manifest path (provenance sha/url)")
    a = ap.parse_args()
    s = load(a.county, a.apply, csv_override=a.csv, manifest_override=a.manifest)
    print(f"county={a.county} fips={SOURCES[a.county]['county_fips']}  "
          f"read={s['read']} kept={s['kept']} "
          f"skipped(no_price={s['skipped_noprice']}, no_apn={s['skipped_noapn']})  "
          f"{'WROTE ' + str(s['written']) if a.apply else 'DRY-RUN (no write)'}")


if __name__ == "__main__":
    main()

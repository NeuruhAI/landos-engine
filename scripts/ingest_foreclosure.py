"""
ingest_foreclosure.py — ingest a county sheriff-sale / mortgage-foreclosure intake CSV
as a `foreclosure` distress signal + enrich owners via ReportAll.

    python -m scripts.ingest_foreclosure \
        --file data/incoming/stjoseph_in_sheriff_2026-06_intake.csv --county 18141

Court-record fields in the CSV (owner_name, situs_address, legal_desc, parcel_apn) are
AUTHORITATIVE. parcel_apn is the county tax APN (IN = 71-form = ReportAll alt_id_1; this
is our canonical match key — never re-derived). ReportAll is queried only to ADD the
owner MAILING address (needed for absentee) + market values.

IN APN RESOLUTION (the known trap): ReportAll indexes the 018-form on `parcel_id`, so a
71-form query returns count=0. We resolve each parcel by owner-search + EXACT alt_id_1
match (owner names are authoritative court records), which yields the ReportAll record
incl. its 018-form parcel_id (stored in reportall_parcel_id). Berrien-style (strip_prefix)
counties query parcel_id directly. No fabrication: a parcel that can't be resolved is
upserted with court fields only (mailing null -> absentee 'unknown'), and counted.

Idempotent: upsert owner_leads by (tenant, county_fips, parcel_apn); one foreclosure
signal per parcel into landos.signals. Gated: no skip-trace, no push, $0.
"""
import argparse
import csv
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.normalizers.reportall_owner import (
    COUNTY_CONFIG, ReportAllOwnerNormalizer, reportall_parcel_id, _num, _parse_csz,
)
from scripts.owner_leads import county_alias_to_fips

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
SIGNAL_STRENGTH = 0.9  # sheriff sale = at-auction foreclosure: strong, time-bound distress

_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def _situs_locality(csv_situs: str) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort (city, zip) from a CSV situs string 'street, City, ST zip'. City is
    the comma-field after the street; zip is the first 5-digit run. Never guesses."""
    city = None
    parts = [p.strip() for p in (csv_situs or "").split(",") if p.strip()]
    if len(parts) >= 2:
        city = parts[1]
    m = _ZIP.search(csv_situs or "")
    return (city or None, (m.group(1) if m else None))


def _search_terms(owner_name: str) -> List[str]:
    """Owner-search terms, narrow first: 'LAST FIRST' then 'LAST'/entity lead word."""
    raw = (owner_name or "").strip()
    if not raw:
        return []
    head = raw.split(",")[0].strip()           # surname or entity lead (before 1st comma)
    rest = raw.split(",")[1].strip() if "," in raw else ""
    first = rest.split()[0] if rest else ""
    terms = []
    if head and first:
        terms.append(f"{head} {first}".upper())
    if head:
        terms.append(head.upper())
        terms.append(head.split()[0].upper())  # first word only (broadest)
    # de-dupe preserve order
    seen, out = set(), []
    for t in terms:
        if t and t not in seen:
            seen.add(t); out.append(t)
    return out


def resolve_record(norm: ReportAllOwnerNormalizer, owner_name: str,
                   target_apn: str, max_pages: int = 6) -> Optional[Dict[str, Any]]:
    """Owner-search + exact alt_id_1 match -> ReportAll record (or None)."""
    for term in _search_terms(owner_name):
        page = 1
        while page <= max_pages:
            data = norm._search({"owner": term, "rpp": 50, "page": page},
                                f"owner~{term} p{page}")
            results = data.get("results") or []
            for r in results:
                if (r.get("alt_id_1") or "").strip() == target_apn:
                    return r
            if len(results) < 50:
                break
            page += 1
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--county", required=True)
    ap.add_argument("--sale-period", default=None, help="override; else read from CSV")
    args = ap.parse_args()

    fips = county_alias_to_fips(args.county)
    if not fips:
        raise SystemExit(f"Unknown county {args.county!r}.")
    region = COUNTY_CONFIG[fips]["region"]
    id_mode = COUNTY_CONFIG[fips].get("id_mode", "strip_prefix")

    rows = list(csv.DictReader(open(args.file)))
    # dedupe by APN (authoritative county tax APN)
    by_apn: Dict[str, Dict[str, str]] = {}
    for r in rows:
        apn = (r.get("parcel_apn") or "").strip()
        if apn:
            by_apn.setdefault(apn, r)
    print(f"county={args.county} fips={fips} region={region!r} id_mode={id_mode}")
    print(f"intake rows={len(rows)}  unique APNs={len(by_apn)}")

    norm = ReportAllOwnerNormalizer(TENANT, fips)  # used for _search + client key
    norm._require_key()

    leads: List[Dict[str, Any]] = []
    signals: List[Dict[str, Any]] = []
    resolved_n = 0
    sale_period = args.sale_period

    for apn, row in by_apn.items():
        owner_csv = (row.get("owner_name") or "").strip() or None
        situs_csv = (row.get("situs_address") or "").strip() or None
        legal_csv = (row.get("legal_desc") or "").strip() or None
        sale_period = sale_period or (row.get("sale_period") or "").strip() or None

        # IN: resolve via owner-search + alt_id_1 match; strip_prefix: direct parcel_id
        rec = None
        if id_mode == "reportall_native":
            rec = resolve_record(norm, owner_csv or "", apn)
        else:
            data = norm._query(reportall_parcel_id(apn, norm.strip_prefix))
            res = data.get("results") or []
            rec = res[0] if res else None

        if rec:
            resolved_n += 1
            m_city = (rec.get("mail_placename") or "").strip() or None
            m_state = (rec.get("mail_statename") or "").strip() or None
            m_zip = (rec.get("mail_zipcode") or "").strip() or None
            if not (m_city and m_state):
                c, s, z = _parse_csz(rec.get("mail_address3"))
                m_city = m_city or c; m_state = m_state or s; m_zip = m_zip or z
            reportall_pid = (rec.get("parcel_id") or "").strip() or None
            situs_lat = _num(rec.get("latitude")); situs_lng = _num(rec.get("longitude"))
            acreage = _num(rec.get("acreage")) or _num(rec.get("acreage_calc"))
            land_use = (rec.get("land_use_class") or "").strip() or None
            mkt = {k: rec.get(k) for k in ("mkt_val_land", "mkt_val_bldg", "mkt_val_tot")}
        else:
            m_city = m_state = m_zip = reportall_pid = None
            situs_lat = situs_lng = acreage = land_use = None
            mkt = {}

        # situs locality: CSV is authoritative (full street); fill city/zip from CSV
        s_city, s_zip = _situs_locality(situs_csv or "")
        leads.append({
            "county_fips": fips,
            "parcel_apn": apn,                          # authoritative county APN (71-form)
            "reportall_parcel_id": reportall_pid,       # ReportAll 018-form ingest id
            "situs_address": situs_csv,                 # authoritative court record
            "situs_lat": situs_lat, "situs_lng": situs_lng,
            "owner_name": owner_csv,                    # authoritative court record
            "owner_mailing_address": (rec.get("mail_address1").strip() if rec and rec.get("mail_address1") else None),
            "owner_mailing_city": m_city, "owner_mailing_state": m_state, "owner_mailing_zip": m_zip,
            "land_use_class": land_use, "acreage": acreage,
            "provenance": f"sheriff_sale:{region}:{sale_period}; reportall_parcel_id={reportall_pid}",
            "raw_meta": {
                "reportall_parcel_id": reportall_pid,
                "alt_id_1": apn,
                "situs_city": s_city, "situs_county": COUNTY_CONFIG[fips]["region"].split(" County")[0],
                "situs_state": COUNTY_CONFIG[fips]["state_abbr"], "situs_zip": s_zip,
                "legal_desc": legal_csv,
                "mkt_val_land": mkt.get("mkt_val_land"), "mkt_val_bldg": mkt.get("mkt_val_bldg"),
                "mkt_val_tot": mkt.get("mkt_val_tot"),
                "intake_source": "sheriff_sale", "sale_period": sale_period,
                "reportall_resolved": bool(rec),
            },
        })
        signals.append({"apn": apn, "legal": legal_csv})

    # ---- write: signal_sources -> owner_leads -> signals -> audit-log ----
    source_id = f"sheriff_{args.county.lower().replace(' ', '').replace('.', '').replace(',', '_')}_{sale_period}"
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """insert into landos.signal_sources
                   (source_id, source_type, name, county, fetch_method, provenance,
                    enabled, tenant_id, last_run, updated_at)
                   values (%s,'foreclosure',%s,%s,'csv_intake',%s,true,%s, now(), now())
                   on conflict (source_id) do update set last_run=now(), updated_at=now()""",
                (source_id, f"Sheriff sale {region} {sale_period}", region,
                 f"county sheriff-sale intake {sale_period}", TENANT),
            )
            n_leads = norm.upsert(cur, leads)
            n_sig = 0
            for s in signals:
                cur.execute(
                    """insert into landos.signals
                       (tenant_id, parcel_apn, signal_type, signal_strength, signal_date,
                        source_id, raw_meta, updated_at)
                       values (%s,%s,'foreclosure',%s,%s,%s,%s, now())
                       on conflict (tenant_id, source_id, parcel_apn, signal_type) do update set
                         signal_strength=excluded.signal_strength, signal_date=excluded.signal_date,
                         raw_meta=excluded.raw_meta, updated_at=now()""",
                    (TENANT, s["apn"], SIGNAL_STRENGTH, date(2026, 6, 1), source_id,
                     Jsonb({"signal_source": "sheriff_sale", "sale_period": sale_period})),
                )
                n_sig += 1
            cur.execute(
                """insert into landos.owner_leads_import_log
                   (tenant_id, source, county_fips, row_count, status, notes)
                   values (%s,'sheriff_sale',%s,%s,'ok',%s)""",
                (TENANT, fips, n_leads,
                 f"{n_leads} foreclosure leads ({resolved_n}/{len(by_apn)} ReportAll-resolved), "
                 f"{n_sig} signals, {norm.calls_made} API calls, period={sale_period}"),
            )
        conn.commit()

    print(f"Done. owner_leads upserted={n_leads}  foreclosure signals={n_sig}  "
          f"ReportAll-resolved={resolved_n}/{len(by_apn)}  API calls={norm.calls_made}")
    unresolved = [d["parcel_apn"] for d in leads if not d["raw_meta"]["reportall_resolved"]]
    if unresolved:
        print(f"  unresolved (court fields kept, mailing null, NOT fabricated): {unresolved}")


if __name__ == "__main__":
    main()

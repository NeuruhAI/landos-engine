"""
reportall_owner.py — Owner-layer normalizer: pulls parcel OWNERSHIP + mailing from
the ReportAll USA Parcel Data API into landos.owner_leads.

This follows the same contract shape as base.py (fetch -> normalize -> upsert +
audit-log), but its destination is the owner-lead store (landos.owner_leads), not
the signal store, so it is a standalone class rather than a Normalizer subclass.

MULTI-COUNTY BY DESIGN. Nothing Berrien-specific is hardcoded in the logic: a
county is one row in COUNTY_CONFIG keyed by its FIPS code. Adding a county = adding
a config row (region string + APN-prefix rule), NOT new code.

CRITICAL APN MAPPING (verified against the live API):
  ReportAll stores the Michigan PIN WITHOUT the leading county prefix segment.
  Berrien's county prefix is "11-", so CSV APN "11-54-0340-0145-00-9" is stored by
  ReportAll as parcel_id "54-0340-0145-00-9", and the full CSV form appears in their
  `alt_id_2` field. Some source rows (e.g. tax-delinquent lists) already arrive
  prefix-less ("07-5620-0075-00-8") and are queried as-is. We always KEEP the
  original CSV APN as owner_leads.parcel_apn so it joins exactly to landos.signals.

REAL DATA ONLY. A parcel that returns count=0 is recorded as a miss and skipped —
never invented. A genuine API/auth failure (bad client key, HTTP error, status
"error") raises immediately so the run STOPS and audit-logs the exact message.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from psycopg.types.json import Jsonb

API_URL = "https://reportallusa.com/api/parcels.php"
API_VERSION = "9"

# county_fips -> how to query ReportAll for that county.
#   region        : ReportAll `region` clause text
#   state_abbr    : situs state (for absentee out-of-state comparison)
#   strip_prefix  : leading APN segment ReportAll drops for this county ("" = none)
# Berrien (26021) is just the first entry; this is the ONLY county-specific data.
COUNTY_CONFIG: Dict[str, Dict[str, str]] = {
    "26021": {"region": "Berrien County, MI", "state_abbr": "MI", "strip_prefix": "11-"},
}

RawHit = Tuple[str, Optional[Dict[str, Any]]]  # (original_csv_apn, reportall_result_or_None)


def _num(v: Any) -> Optional[float]:
    try:
        f = float(str(v).replace(",", "").strip())
        return f
    except (TypeError, ValueError):
        return None


def reportall_parcel_id(csv_apn: str, strip_prefix: str) -> str:
    """Map a source/CSV APN to the parcel_id ReportAll stores (drop county prefix)."""
    apn = (csv_apn or "").strip()
    if strip_prefix and apn.startswith(strip_prefix):
        return apn[len(strip_prefix):]
    return apn


class ReportAllOwnerNormalizer:
    source = "reportall"

    def __init__(self, tenant_id: str, county_fips: str, apns: List[str],
                 client_key: Optional[str] = None, timeout: int = 30):
        if county_fips not in COUNTY_CONFIG:
            raise ValueError(
                f"No COUNTY_CONFIG entry for county_fips={county_fips!r}. "
                f"Add a row (region + strip_prefix) to enable this county."
            )
        self.tenant_id = tenant_id
        self.county_fips = county_fips
        self.cfg = COUNTY_CONFIG[county_fips]
        # de-dupe while preserving order; drop blanks (never query an empty APN)
        seen = set()
        self.apns = [a.strip() for a in apns
                     if a and a.strip() and not (a.strip() in seen or seen.add(a.strip()))]
        self.client_key = client_key or os.environ.get("REPORTALL_CLIENT_KEY")
        self.timeout = timeout
        self.calls_made = 0
        self.misses: List[str] = []  # APNs that returned count=0

    # ---- fetch ---------------------------------------------------------------
    def _query(self, parcel_id: str) -> Dict[str, Any]:
        params = urllib.parse.urlencode({
            "client": self.client_key,
            "v": API_VERSION,
            "region": self.cfg["region"],
            "parcel_id": parcel_id,
        })
        url = f"{API_URL}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "landos-engine/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"ReportAll HTTP {e.code} for parcel_id={parcel_id}: {detail}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"ReportAll network error for parcel_id={parcel_id}: {e.reason}")
        self.calls_made += 1
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"ReportAll non-JSON response for parcel_id={parcel_id}: {body[:200]}")
        if data.get("status") != "OK":
            # auth/quota/bad-request — fatal, stop the whole run with the exact message
            raise RuntimeError(
                f"ReportAll error for parcel_id={parcel_id}: {data.get('message', data)}"
            )
        return data

    def fetch(self) -> List[RawHit]:
        if not self.client_key:
            raise RuntimeError(
                "REPORTALL_CLIENT_KEY is not set (env or constructor). Cannot query "
                "ReportAll. Set it in .env before running."
            )
        out: List[RawHit] = []
        for csv_apn in self.apns:
            pid = reportall_parcel_id(csv_apn, self.cfg["strip_prefix"])
            data = self._query(pid)
            results = data.get("results") or []
            if not results:
                self.misses.append(csv_apn)
                out.append((csv_apn, None))
                continue
            out.append((csv_apn, results[0]))
        return out

    # ---- normalize -----------------------------------------------------------
    def normalize(self, raw: List[RawHit]) -> List[Dict[str, Any]]:
        leads: List[Dict[str, Any]] = []
        for csv_apn, r in raw:
            if not r:
                continue  # miss: no real record -> never fabricate
            # mailing: prefer ReportAll's structured fields, fall back to mail_address3
            m_city = (r.get("mail_placename") or "").strip() or None
            m_state = (r.get("mail_statename") or "").strip() or None
            m_zip = (r.get("mail_zipcode") or "").strip() or None
            if not (m_city and m_state):
                # mail_address3 looks like "BENTON HARBOR MI 49022"
                parsed = _parse_csz(r.get("mail_address3"))
                m_city = m_city or parsed[0]
                m_state = m_state or parsed[1]
                m_zip = m_zip or parsed[2]
            acreage = _num(r.get("acreage"))
            if not acreage:  # "0.0000" -> use the GIS-computed acreage
                acreage = _num(r.get("acreage_calc"))
            leads.append({
                "county_fips": self.county_fips,
                "parcel_apn": csv_apn,  # canonical: matches landos.signals
                "situs_address": (r.get("address") or "").strip() or None,
                "situs_lat": _num(r.get("latitude")),
                "situs_lng": _num(r.get("longitude")),
                "owner_name": (r.get("owner") or "").strip() or None,
                "owner_mailing_address": (r.get("mail_address1") or "").strip() or None,
                "owner_mailing_city": m_city,
                "owner_mailing_state": m_state,
                "owner_mailing_zip": m_zip,
                "land_use_class": (r.get("land_use_class") or "").strip() or None,
                "acreage": acreage,
                "provenance": f"reportall:{self.cfg['region']}:parcel_id={r.get('parcel_id')}",
                "raw_meta": {
                    "reportall_parcel_id": r.get("parcel_id"),
                    "alt_id_2": r.get("alt_id_2"),
                    "robust_id": r.get("robust_id"),
                    # situs locality kept here for the absentee scorer (no dedicated columns)
                    "situs_city": (r.get("addr_city") or r.get("census_place") or "").strip() or None,
                    "situs_county": (r.get("county_name") or "").strip() or None,
                    "situs_state": (r.get("state_abbr") or "").strip() or None,
                    "situs_zip": (r.get("addr_zip") or r.get("census_zip") or "").strip() or None,
                    "land_use_code": r.get("land_use_code"),
                    "county_link": r.get("county_link"),
                    "last_updated": r.get("last_updated"),
                },
            })
        return leads

    # ---- upsert + audit-log --------------------------------------------------
    def upsert(self, cur, leads: List[Dict[str, Any]]) -> int:
        n = 0
        for d in leads:
            cur.execute(
                """
                insert into landos.owner_leads
                  (tenant_id, county_fips, parcel_apn, situs_address, situs_lat, situs_lng,
                   owner_name, owner_mailing_address, owner_mailing_city,
                   owner_mailing_state, owner_mailing_zip, land_use_class, acreage,
                   source, provenance, raw_meta, updated_at)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                on conflict (tenant_id, county_fips, parcel_apn) do update set
                  situs_address=excluded.situs_address, situs_lat=excluded.situs_lat,
                  situs_lng=excluded.situs_lng, owner_name=excluded.owner_name,
                  owner_mailing_address=excluded.owner_mailing_address,
                  owner_mailing_city=excluded.owner_mailing_city,
                  owner_mailing_state=excluded.owner_mailing_state,
                  owner_mailing_zip=excluded.owner_mailing_zip,
                  land_use_class=excluded.land_use_class, acreage=excluded.acreage,
                  source=excluded.source, provenance=excluded.provenance,
                  raw_meta=excluded.raw_meta, updated_at=now()
                """,
                (self.tenant_id, d["county_fips"], d["parcel_apn"], d["situs_address"],
                 d["situs_lat"], d["situs_lng"], d["owner_name"], d["owner_mailing_address"],
                 d["owner_mailing_city"], d["owner_mailing_state"], d["owner_mailing_zip"],
                 d["land_use_class"], d["acreage"], self.source, d["provenance"],
                 Jsonb(d["raw_meta"])),
            )
            n += 1
        return n

    def log_import(self, cur, row_count: int, status: str, notes: str) -> None:
        cur.execute(
            "insert into landos.owner_leads_import_log "
            "(tenant_id, source, county_fips, row_count, status, notes) "
            "values (%s,%s,%s,%s,%s,%s)",
            (self.tenant_id, self.source, self.county_fips, row_count, status, notes),
        )

    def run(self, conn) -> int:
        """fetch -> normalize -> upsert -> audit-log, committed together. On any
        fatal fetch error the failure is audit-logged and re-raised (run STOPS)."""
        with conn.cursor() as cur:
            try:
                raw = self.fetch()
                leads = self.normalize(raw)
            except Exception as e:  # noqa: BLE001 - audit the failure, then surface it
                self.log_import(cur, 0, "error",
                                f"{e.__class__.__name__}: {e} (calls={self.calls_made})")
                conn.commit()
                raise
            n = self.upsert(cur, leads)
            self.log_import(
                cur, n, "ok" if n else "empty",
                f"reportall {self.cfg['region']}: {n} leads from {len(self.apns)} APNs, "
                f"{len(self.misses)} miss, {self.calls_made} API calls",
            )
        conn.commit()
        return n


def _parse_csz(value: Any) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Parse a 'CITY ST ZIP' tail (e.g. 'BENTON HARBOR MI 49022') -> (city, st, zip).
    Conservative: returns Nones if it cannot confidently split. Never guesses a state."""
    if not value:
        return (None, None, None)
    parts = str(value).strip().split()
    if len(parts) < 2:
        return (None, None, None)
    zipc = None
    if parts[-1].replace("-", "").isdigit() and len(parts[-1]) >= 5:
        zipc = parts[-1]
        parts = parts[:-1]
    state = None
    if parts and len(parts[-1]) == 2 and parts[-1].isalpha():
        state = parts[-1].upper()
        parts = parts[:-1]
    city = " ".join(parts).title() if parts else None
    return (city, state, zipc)

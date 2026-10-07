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

import hashlib
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
#   id_mode       : how the ingest key and the canonical match key (parcel_apn) relate:
#     "strip_prefix"    — ReportAll parcel_id IS the human county APN (minus a prefix).
#                         Ingest by parcel_id; parcel_apn = the (de-prefixed) input APN.
#                         Berrien MI. Requires "strip_prefix".
#     "reportall_native"— ReportAll parcel_id is an INTERNAL id (e.g. 018-form); the
#                         human county tax APN lives in alt_id_1 (e.g. 71-...-026).
#                         Ingest BY parcel_id (discovery pull); parcel_apn = alt_id_1
#                         (the canonical match key); the 018-form goes to the dedicated
#                         reportall_parcel_id column. St. Joseph IN. No strip_prefix.
# This is the ONLY county-specific data; adding a county = adding a row here.
COUNTY_CONFIG: Dict[str, Dict[str, str]] = {
    "26021": {"region": "Berrien County, MI", "state_abbr": "MI",
              "id_mode": "strip_prefix", "strip_prefix": "11-"},
    "18141": {"region": "St. Joseph County, IN", "state_abbr": "IN",
              "id_mode": "reportall_native"},
}

RawHit = Tuple[str, Optional[Dict[str, Any]]]  # (original_csv_apn, reportall_result_or_None)


def _num(v: Any) -> Optional[float]:
    try:
        f = float(str(v).replace(",", "").strip())
        return f
    except (TypeError, ValueError):
        return None


def reportall_target_key(county_fips: str, extra: Dict[str, Any]) -> str:
    """The free-first target a ReportAll query is about: the parcel id or owner it names,
    else the query itself (a bulk discovery pull is a target too, and it is paid)."""
    if extra.get("parcel_id"):
        return f"parcel:{county_fips}:{str(extra['parcel_id']).strip()}"
    if extra.get("owner"):
        return f"owner:{county_fips}:{str(extra['owner']).strip().upper()}"
    digest = json.dumps(extra, sort_keys=True, default=str)
    return f"reportall_query:{county_fips}:{hashlib.sha256(digest.encode()).hexdigest()[:16]}"


def reportall_parcel_id(csv_apn: str, strip_prefix: str) -> str:
    """Map a source/CSV APN to the parcel_id ReportAll stores (drop county prefix)."""
    apn = (csv_apn or "").strip()
    if strip_prefix and apn.startswith(strip_prefix):
        return apn[len(strip_prefix):]
    return apn


class ReportAllOwnerNormalizer:
    source = "reportall"

    def __init__(self, tenant_id: str, county_fips: str, apns: Optional[List[str]] = None,
                 client_key: Optional[str] = None, timeout: int = 30,
                 discover_limit: int = 150):
        if county_fips not in COUNTY_CONFIG:
            raise ValueError(
                f"No COUNTY_CONFIG entry for county_fips={county_fips!r}. "
                f"Add a row (region + id_mode) to enable this county."
            )
        self.tenant_id = tenant_id
        self.county_fips = county_fips
        self.cfg = COUNTY_CONFIG[county_fips]
        self.id_mode = self.cfg.get("id_mode", "strip_prefix")
        self.strip_prefix = self.cfg.get("strip_prefix", "")
        # de-dupe while preserving order; drop blanks (never query an empty APN)
        seen = set()
        self.apns = [a.strip() for a in (apns or [])
                     if a and a.strip() and not (a.strip() in seen or seen.add(a.strip()))]
        self.client_key = client_key or os.environ.get("REPORTALL_CLIENT_KEY")
        self.timeout = timeout
        self.discover_limit = discover_limit
        self.calls_made = 0
        self.misses: List[str] = []  # APNs that returned count=0

    # ---- fetch ---------------------------------------------------------------
    def _require_key(self) -> None:
        if not self.client_key:
            raise RuntimeError(
                "REPORTALL_CLIENT_KEY is not set (env or constructor). Cannot query "
                "ReportAll. Set it in .env before running."
            )

    def _search(self, extra: Dict[str, Any], label: str) -> Dict[str, Any]:
        """Low-level ReportAll call: region + extra attribute clause(s). Raises on
        HTTP / network / non-OK (fatal — the run STOPS with the exact message).

        ReportAll is metered: every call first clears the FREE-FIRST guard
        (app/free_first/guard.py) for the parcel / owner it asks about. A blocked
        call raises PaidCallBlocked (a RuntimeError) before any request is built."""
        from app.free_first.guard import paid_call
        with paid_call("reportall", items=[("parcel.owner_identity", reportall_target_key(self.county_fips, extra))],
                       est_cost_usd=None, caller="app/normalizers/reportall_owner.py:ReportAllOwnerNormalizer._search"):
            params = urllib.parse.urlencode({
                "client": self.client_key, "v": API_VERSION,
                "region": self.cfg["region"], **extra,
            })
            req = urllib.request.Request(f"{API_URL}?{params}",
                                         headers={"User-Agent": "landos-engine/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", errors="replace")[:300]
                raise RuntimeError(f"ReportAll HTTP {e.code} for {label}: {detail}")
            except urllib.error.URLError as e:
                raise RuntimeError(f"ReportAll network error for {label}: {e.reason}")
            self.calls_made += 1
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                raise RuntimeError(f"ReportAll non-JSON response for {label}: {body[:200]}")
            if data.get("status") != "OK":
                raise RuntimeError(f"ReportAll error for {label}: {data.get('message', data)}")
        return data

    def _query(self, parcel_id: str) -> Dict[str, Any]:
        return self._search({"parcel_id": parcel_id}, f"parcel_id={parcel_id}")

    # discovery seed terms for a brand-new county with no lead/signal source yet:
    # common surnames for individuals + "LLC" for entity owners (absentee variety).
    SEED_OWNER_TERMS = ["SMITH", "JOHNSON", "MILLER", "BROWN", "DAVIS",
                        "WILLIAMS", "JONES", "WILSON", "LLC"]

    def fetch(self) -> List[RawHit]:
        """strip_prefix mode: query each known APN by parcel_id (the human county APN)."""
        self._require_key()
        out: List[RawHit] = []
        for csv_apn in self.apns:
            pid = reportall_parcel_id(csv_apn, self.strip_prefix)
            data = self._query(pid)
            results = data.get("results") or []
            if not results:
                self.misses.append(csv_apn)
                out.append((csv_apn, None))
                continue
            out.append((csv_apn, results[0]))
        return out

    def discover(self) -> List[RawHit]:
        """reportall_native mode: no pre-existing APN source for the county, so seed by
        owner-search pulls (paginated, varied terms), keyed by ReportAll parcel_id. Each
        record already carries alt_id_1 (the county APN) — no per-parcel re-query needed.
        Dedups by parcel_id, capped at discover_limit. Returns (parcel_id, record) tuples."""
        self._require_key()
        seen: set = set()
        out: List[RawHit] = []
        rpp = 50
        terms = self.SEED_OWNER_TERMS
        # ROUND-ROBIN: cap each term at its fair share so NO single common surname
        # (e.g. SMITH) fills the whole quota and starves later terms like "LLC".
        per_term = max(1, -(-self.discover_limit // len(terms)))  # ceil(limit/terms)

        def pull(term: str, cap: int, tag: str) -> None:
            got, page = 0, 1
            while got < cap and len(out) < self.discover_limit:
                data = self._search({"owner": term, "rpp": rpp, "page": page},
                                    f"owner~{term} {tag}p{page}")
                results = data.get("results") or []
                if not results:
                    break
                for r in results:
                    pid = r.get("parcel_id")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    out.append((pid, r))
                    got += 1
                    if got >= cap or len(out) >= self.discover_limit:
                        break
                if len(results) < rpp:
                    break  # last page for this term
                page += 1

        # pass 1: fair share per term (guarantees every term, incl LLC, contributes)
        for term in terms:
            if len(out) >= self.discover_limit:
                break
            pull(term, per_term, "")
        # pass 2: if short (some terms had few matches), top up unconstrained
        for term in terms:
            if len(out) >= self.discover_limit:
                break
            pull(term, self.discover_limit, "topup ")
        return out[: self.discover_limit]

    # ---- normalize -----------------------------------------------------------
    def normalize(self, raw: List[RawHit]) -> List[Dict[str, Any]]:
        leads: List[Dict[str, Any]] = []
        for ingest_key, r in raw:
            if not r:
                continue  # miss: no real record -> never fabricate
            # ingest-id vs match-id split (see COUNTY_CONFIG.id_mode):
            #   strip_prefix     -> parcel_apn = the (de-prefixed) input APN; no separate
            #                       ingest id (ReportAll parcel_id == the human APN).
            #   reportall_native -> parcel_apn = alt_id_1 (human county APN, the match key);
            #                       reportall_parcel_id = the 018-form ingest id.
            if self.id_mode == "reportall_native":
                parcel_apn = (r.get("alt_id_1") or "").strip() or None
                reportall_pid = (r.get("parcel_id") or "").strip() or None
                if not parcel_apn:
                    # no county APN -> nothing to match on; skip, never fabricate a key
                    self.misses.append(reportall_pid or "<no parcel_id>")
                    continue
            else:
                parcel_apn = ingest_key
                reportall_pid = (r.get("parcel_id") or "").strip() or None
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
                "parcel_apn": parcel_apn,  # canonical match key (human county APN)
                "reportall_parcel_id": reportall_pid,  # ReportAll-native ingest id
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
                    "alt_id_1": r.get("alt_id_1"),
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
                  (tenant_id, county_fips, parcel_apn, reportall_parcel_id,
                   situs_address, situs_lat, situs_lng,
                   owner_name, owner_mailing_address, owner_mailing_city,
                   owner_mailing_state, owner_mailing_zip, land_use_class, acreage,
                   source, provenance, raw_meta, updated_at)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                on conflict (tenant_id, county_fips, parcel_apn) do update set
                  reportall_parcel_id=excluded.reportall_parcel_id,
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
                (self.tenant_id, d["county_fips"], d["parcel_apn"], d.get("reportall_parcel_id"),
                 d["situs_address"], d["situs_lat"], d["situs_lng"], d["owner_name"],
                 d["owner_mailing_address"], d["owner_mailing_city"], d["owner_mailing_state"],
                 d["owner_mailing_zip"], d["land_use_class"], d["acreage"], self.source,
                 d["provenance"], Jsonb(d["raw_meta"])),
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

    def collect(self) -> List[RawHit]:
        """Mode-aware raw pull: per-APN fetch (strip_prefix) or owner-search discovery
        (reportall_native, for a county with no pre-existing APN source)."""
        return self.discover() if self.id_mode == "reportall_native" else self.fetch()

    def run(self, conn) -> int:
        """collect -> normalize -> upsert -> audit-log, committed together. On any
        fatal error the failure is audit-logged and re-raised (run STOPS)."""
        with conn.cursor() as cur:
            try:
                raw = self.collect()
                leads = self.normalize(raw)
            except Exception as e:  # noqa: BLE001 - audit the failure, then surface it
                self.log_import(cur, 0, "error",
                                f"{e.__class__.__name__}: {e} (calls={self.calls_made})")
                conn.commit()
                raise
            n = self.upsert(cur, leads)
            n_input = len(self.apns) if self.id_mode != "reportall_native" else len(raw)
            self.log_import(
                cur, n, "ok" if n else "empty",
                f"reportall {self.cfg['region']} ({self.id_mode}): {n} leads from "
                f"{n_input} records, {len(self.misses)} miss, {self.calls_made} API calls",
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

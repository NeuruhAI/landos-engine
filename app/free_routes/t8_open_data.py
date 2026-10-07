"""T8: county open data → signals and mailing refresh (never phones). Wake's daily
delinquent extract (automated) and Charlotte code-enforcement cases (ArcGIS)."""
from __future__ import annotations

import os
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .arcgis_cases import CHARLOTTE_SERVICES, fetch_cases_by_parcel
from .attempts import Attempt
from .estate_rows import index_by_pin, norm_pin
from .receipts import RouteReceipt
from .wake_delinquent import PROVENANCE_URL as WAKE_URL, SOURCE as WAKE_SOURCE, read_extract, summarize

WAKE_EXTRACT_BASE = "https://services.wake.gov/collection_extracts/"


def latest_wake_extract_name(today: datetime) -> str:
    return f"real_estate_delq853_{today.strftime('%m%d%Y')}.zip"


def download_wake_extract(dest_dir: str, today: datetime, fetch=None) -> str:
    """Download today's delinquent extract (falls back to yesterday's name when today's
    is not published yet). Returns the local path."""
    os.makedirs(dest_dir, exist_ok=True)
    fetch = fetch or (lambda url, path: urllib.request.urlretrieve(url, path))
    for delta in (0, 1, 2):
        d = datetime.fromtimestamp(today.timestamp() - delta * 86400, tz=timezone.utc)
        name = latest_wake_extract_name(d)
        path = os.path.join(dest_dir, name)
        if os.path.exists(path):
            return path
        try:
            fetch(WAKE_EXTRACT_BASE + name, path)
            return path
        except Exception:  # noqa: BLE001 — try the previous day's file
            continue
    raise RuntimeError("no Wake delinquent extract reachable for the last three days")


def match_wake(estate_rows: List[dict], extract_path: str, *, run_id: str, now_iso: str, receipt_path: Optional[str]) -> Dict:
    """Join the delinquent accounts to Wake estate rows on PIN. Returns signals rows,
    attempts, and the receipt."""
    accounts = read_extract(extract_path)
    summ = summarize(accounts)
    by_pin: Dict[str, list] = {}
    for a in accounts:
        by_pin.setdefault(norm_pin(a.pin), []).append(a)
    wake_rows = [r for r in estate_rows if (r.get("fips") or "") == "37183"]
    est_by_pin = index_by_pin(wake_rows)
    signals: List[dict] = []; attempts: List[Attempt] = []; hits = 0
    for pin, rows in est_by_pin.items():
        accs = by_pin.get(pin, [])
        for r in rows:
            if accs:
                hits += 1
                total_due = round(sum(a.total_due or 0 for a in accs), 2)
                years = sorted({a.tax_year for a in accs})
                a0 = accs[0]
                signals.append({"candidate_id": r["candidate_id"], "route_id": "T8", "source": WAKE_SOURCE, "source_as_of": summ["source_as_of"],
                                "observed_at": now_iso, "signal": "TAX_DELINQUENT", "total_due": total_due, "tax_years": ";".join(years),
                                "years_due_count": len(years), "mailing_address_on_bill": a0.mailing_address, "care_of": a0.care_of,
                                "deed_book": a0.deed_book, "deed_page": a0.deed_page, "deed_date": a0.deed_date or "",
                                "provenance_url": WAKE_URL, "confidence": "HIGH"})
                attempts.append(Attempt(r["candidate_id"], "T8", run_id, "HIT", WAKE_SOURCE, summ["source_as_of"], now_iso, receipt_path,
                                        {"total_due": total_due, "tax_years": years, "deed_ref": f"{a0.deed_book}/{a0.deed_page}"}))
            else:
                attempts.append(Attempt(r["candidate_id"], "T8", run_id, "NO_HIT", WAKE_SOURCE, summ["source_as_of"], now_iso, receipt_path, {}))
    receipt = RouteReceipt("T8", "County open data — Wake daily delinquent extract", "AUTOMATED", "Wake County Tax Administration collection extract",
                           "Free public extract refreshed daily; no terms shown on the download page.", WAKE_URL,
                           rows_tried=len(wake_rows), hits=hits, new_contacts=0, new_heirs=0, source_as_of=summ["source_as_of"], observed_at=now_iso,
                           counties=["Wake"], evidence={"extract": os.path.basename(extract_path), **summ})
    return {"signals": signals, "attempts": attempts, "receipt": receipt, "summary": summ}


def match_charlotte_code(estate_rows: List[dict], *, run_id: str, now_iso: str, receipt_path: Optional[str], fetch=None, delay_s: float = 0.5) -> Dict:
    meck = [r for r in estate_rows if (r.get("fips") or "") == "37119" and (r.get("apn") or "").strip()]
    ids = sorted({(r.get("apn") or "").strip() for r in meck})
    kwargs = {"delay_s": delay_s}
    if fetch is not None:
        kwargs["fetch"] = fetch
    cases = fetch_cases_by_parcel(CHARLOTTE_SERVICES["all"], ids, **kwargs) if ids else []
    by_parcel: Dict[str, list] = {}
    for c in cases:
        by_parcel.setdefault(str(c.get("ParcelId") or "").strip(), []).append(c)
    signals: List[dict] = []; attempts: List[Attempt] = []; hits = 0
    src = "CHARLOTTE_CODE_ENFORCEMENT_ARCGIS"
    for r in meck:
        cs = by_parcel.get((r.get("apn") or "").strip(), [])
        if cs:
            hits += 1
            latest = max(cs, key=lambda c: c.get("DateCreated") or "")
            signals.append({"candidate_id": r["candidate_id"], "route_id": "T8", "source": src, "source_as_of": latest.get("DateCreated"),
                            "observed_at": now_iso, "signal": "CODE_ENFORCEMENT_CASE", "case_count": len(cs), "latest_case": latest.get("CaseNumber"),
                            "case_type": latest.get("CaseType"), "case_status": latest.get("CaseStatus"), "provenance_url": CHARLOTTE_SERVICES["all"], "confidence": "HIGH"})
            attempts.append(Attempt(r["candidate_id"], "T8", run_id, "HIT", src, latest.get("DateCreated"), now_iso, receipt_path, {"cases": len(cs)}))
        else:
            attempts.append(Attempt(r["candidate_id"], "T8", run_id, "NO_HIT", src, None, now_iso, receipt_path, {}))
    receipt = RouteReceipt("T8", "County open data — Charlotte code enforcement (ArcGIS)", "AUTOMATED", "City of Charlotte HNS CodeEnforcementCasesAll",
                           "Public ArcGIS REST service, no copyright text, maxRecordCount 750; polite paging.", CHARLOTTE_SERVICES["all"],
                           rows_tried=len(meck), hits=hits, new_contacts=0, new_heirs=0, source_as_of=now_iso[:10], observed_at=now_iso, counties=["Mecklenburg"],
                           evidence={"parcels_queried": len(ids), "cases": len(cases)})
    return {"signals": signals, "attempts": attempts, "receipt": receipt}

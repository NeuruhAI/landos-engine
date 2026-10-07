"""One-click URLs for the routes whose terms forbid scripts (T2, T3, T4, T5, T7, T10)
and the worklist writer every H-12 receipt points at. Pure functions: the operator
clicks, reads, and pastes back; DeedSonar never fetches these pages."""
from __future__ import annotations

import csv
import os
from typing import Dict, Iterable, List, Optional
from urllib.parse import quote_plus, urlencode

ECOURTS_PORTAL = "https://portal-nc.tylertech.cloud/Portal/"
NCNOTICES_SEARCH = "https://www.ncnotices.com/Search.aspx"
LEGACY_SEARCH = "https://www.legacy.com/obituaries/search"
SOS_SEARCH = "https://www.sosnc.gov/online_services/search/by_title/_Business_Registration"
ROD_SITES: Dict[str, str] = {
    "Wake": "https://services.wake.gov/booksweb/",
    "Mecklenburg": "https://meckrod.manatron.com/",
    "Durham": "https://rodweb.dconc.gov/",
}
PEOPLE_SEARCH = {
    "truepeoplesearch": "https://www.truepeoplesearch.com/results?{q}",
    "fastpeoplesearch": "https://www.fastpeoplesearch.com/name/{slug}",
    "google": "https://www.google.com/search?{q}",
}


def _clean(name: str) -> str:
    return " ".join((name or "").replace(",", " ").split())


def t2_notice_search(decedent_name: str, county: str) -> Dict[str, str]:
    """ncnotices.com is an ASP.NET postback form (no GET query string confirmed), so
    the one-click URL is the search page plus the exact terms the operator types."""
    return {"url": NCNOTICES_SEARCH, "keywords": f'"{_clean(decedent_name)}"', "keyword_mode": "Exact Phrase",
            "county": county, "date_range": "last 24 months", "category_hint": "Notice to Creditors / Estate"}


def t3_special_proceedings_search(name: str, county: str) -> Dict[str, str]:
    return {"url": ECOURTS_PORTAL, "smart_search": _clean(name), "case_type": "Special Proceeding",
            "county": county, "note": "human-verification challenge re-arms about every ten minutes; never defeated"}


def t1_estate_search(decedent_name: str, county: str) -> Dict[str, str]:
    return {"url": ECOURTS_PORTAL, "smart_search": _clean(decedent_name), "case_type": "Estate",
            "county": county, "download": "AOC-E-201 (application) and AOC-E-505 (inventory) PDFs"}


def t4_obituary_search(decedent_name: str, city: str = "", state: str = "NC") -> Dict[str, str]:
    parts = _clean(decedent_name).split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""
    q = urlencode({"firstName": first, "lastName": last, "location": f"{city}, {state}".strip(", ")})
    g = urlencode({"q": f'"{_clean(decedent_name)}" obituary {city} {state} "survived by"'})
    return {"legacy_url": f"{LEGACY_SEARCH}?{q}", "google_url": f"https://www.google.com/search?{g}",
            "paste_back": "the 'survived by' paragraph"}


def t5_sos_search(entity_name: str) -> Dict[str, str]:
    return {"url": SOS_SEARCH, "search_by": "Company Name (exact)", "entity_name": _clean(entity_name),
            "record": "SOSID, status, registered agent, principal office, mailing office, company officials"}


def t7_rod_search(owner_name: str, county: str) -> Dict[str, str]:
    return {"url": ROD_SITES.get(county, ""), "county": county, "search_by": "grantor/grantee name",
            "name": _clean(owner_name), "record": "book/page, instrument date, grantor, grantee, mailing address on the instrument, mortgage holder, liens"}


def t10_people_search(person_name: str, city: str = "", state: str = "NC") -> Dict[str, str]:
    name = _clean(person_name)
    slug = "-".join(name.lower().split()) + (f"_{city.lower().replace(' ', '-')}-{state.lower()}" if city else "")
    return {
        "truepeoplesearch": PEOPLE_SEARCH["truepeoplesearch"].format(q=urlencode({"name": name, "citystatezip": f"{city} {state}".strip()})),
        "fastpeoplesearch": PEOPLE_SEARCH["fastpeoplesearch"].format(slug=slug),
        "google": PEOPLE_SEARCH["google"].format(q=urlencode({"q": f'"{name}" {city} {state} phone'})),
    }


WORKLIST_COLUMNS = ["route_id", "global_rank", "candidate_id", "name", "role", "county", "situs_address", "apn",
                    "url", "instructions", "paste_back"]


def write_worklist(path: str, rows: Iterable[Dict[str, str]]) -> int:
    """Write the operator worklist for one H-12 route (0600, outside Git)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    n = 0
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=WORKLIST_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in WORKLIST_COLUMNS})
            n += 1
    os.chmod(path, 0o600)
    return n

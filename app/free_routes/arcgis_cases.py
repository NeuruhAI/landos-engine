"""T8 (Charlotte / any ArcGIS FeatureServer with a parcel-id field): fetch code-
enforcement cases for a set of parcel ids, polite paging under maxRecordCount,
JSON only, no auth, no rate control evaded. Public services publish no copyright
text; the receipt records the service URL as provenance_url."""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Callable, Dict, Iterable, List, Optional

CHARLOTTE_SERVICES = {
    "all": "https://gis.charlottenc.gov/arcgis/rest/services/HNS/CodeEnforcementCasesAll/MapServer/0",
    "open": "https://gis.charlottenc.gov/arcgis/rest/services/HNS/CodeEnforcementNewAndOpenCases/MapServer/0",
    "closed": "https://gis.charlottenc.gov/arcgis/rest/services/HNS/CodeEnforcementClosedCases/MapServer/0",
    "demolish": "https://gis.charlottenc.gov/arcgis/rest/services/HNS/CodeEnforcementOrderstoDemolish/MapServer/0",
}
USER_AGENT = "DeedSonar-free-routes/1.0 (+public open data; contact via app.deedsonar.com)"
FIELDS = "CaseNumber,ParcelId,CaseType,FullAddress,CaseStatus,DateCreated,DateClosed,Conclusion"


def _http_get_json(url: str, timeout: int = 60) -> Dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _chunks(items: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


def fetch_cases_by_parcel(service_url: str, parcel_ids: Iterable[str], *, parcel_field: str = "ParcelId",
                          batch: int = 100, delay_s: float = 0.5, fetch: Callable[[str], Dict] = _http_get_json) -> List[Dict]:
    """WHERE ParcelId IN (...) in batches of `batch`, sleeping `delay_s` between calls.
    Returns feature attributes with epoch-ms dates converted to ISO dates."""
    ids = sorted({str(p).strip() for p in parcel_ids if str(p).strip()})
    out: List[Dict] = []
    for grp in _chunks(ids, batch):
        where = f"{parcel_field} IN ({','.join(repr(x) for x in grp)})"
        q = urllib.parse.urlencode({"where": where, "outFields": FIELDS, "returnGeometry": "false", "f": "json",
                                    "resultRecordCount": 750})
        data = fetch(f"{service_url}/query?{q}")
        for feat in data.get("features", []):
            a = dict(feat.get("attributes", {}))
            for k in ("DateCreated", "DateClosed"):
                v = a.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    a[k] = time.strftime("%Y-%m-%d", time.gmtime(v / 1000))
            out.append(a)
        if delay_s:
            time.sleep(delay_s)
    return out


def count_all(service_url: str, fetch: Callable[[str], Dict] = _http_get_json) -> Optional[int]:
    q = urllib.parse.urlencode({"where": "1=1", "returnCountOnly": "true", "f": "json"})
    return fetch(f"{service_url}/query?{q}").get("count")

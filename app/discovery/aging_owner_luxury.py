"""
aging_owner_luxury.py — DiscoveryAdapter: get AHEAD of the estate sale.

THE THESIS. estate_sale_ext catches the household dissolving; this catches it BEFORE
it does. A 65+ owner (senior-exemption proxy) who has held a dated house in a luxury
zip for a long time is the pre-downsizing pipeline: the move to assisted living, the
kids' inheritance sale, the teardown listing are all still ahead. Reach them first.
Pure assessor/roll math on owned data. $0.

    signal: {type: aging_owner_luxury, parcel_apn, zip,
             senior:bool, tenure_years, year_built, confidence}

    candidate := senior-exemption present (65+ proxy)
                 AND tenure_years >= min_tenure (long hold)
                 AND zip in the luxury-zip set
                 AND year_built <= max_year_built (dated build)

CENSUS FIRST. Every gate needs a captured field. This adapter NEVER fabricates: it
censuses each parcel and DISARMS where the field is absent.

    LIVE STATE 2026-07-08: DISARMED everywhere. The 65+ proxy (senior/homestead
    exemption) is NOT captured anywhere on the spine (no exemption column — same
    finding as exemption_drop); year_built and tenure are also absent (same as
    lot_value_arbitrage). Only zip + the luxury-zip set (option_sets, mig 066) exist.
    So there is nothing to arm on yet — honestly.

    REAL FIRST TASK (shared unblock): the ReportAll capture in
    docs/enrich_priority_reportall_capture.md — stow the senior/homestead exemption
    flag, yr_blt, and last-sale date into raw_meta. That ONE capture arms this,
    lot_value_arbitrage, AND exemption_drop. This adapter then lights up with no code
    change (gate logic proven vs synthetic parcels below).

MATCHING & LEAD CREATION belong to the pipeline — this adapter's only job is the signal.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from .base import DiscoveryAdapter, DiscoverySignal
from .lot_value_arbitrage import _num, _tenure_from_sale_date, load_luxury_zips

DEFAULT_MIN_TENURE_YEARS = 25
DEFAULT_MAX_YEAR_BUILT = 1985


def _senior(parcel: Dict[str, Any]) -> Optional[bool]:
    """The 65+ proxy: a senior (or senior+homestead) exemption on the roll. Returns
    None when the exemption field isn't captured (silence), True/False only when it
    is — absence must never read as 'not senior' and fabricate a rejection OR a hit."""
    v = parcel.get("senior_exemption")
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("", "none", "null"):
        return None
    return s in ("1", "true", "t", "yes", "y", "senior", "senior_citizen")


def _confidence(tenure_years: float, year_built: Optional[int]) -> float:
    """Score the pre-downsizing candidate. Longer holds and older structures raise
    the odds the household turns over soon. Capped; senior+luxury are gates, so
    confidence grades the remaining runway."""
    base = 0.80
    if tenure_years >= 40:
        base += 0.08
    elif tenure_years >= 30:
        base += 0.05
    if year_built is not None and year_built <= 1960:
        base += 0.04
    return round(min(base, 0.95), 2)


def evaluate(parcel: Dict[str, Any], luxury_zips: set,
             min_tenure_years: float = DEFAULT_MIN_TENURE_YEARS,
             max_year_built: int = DEFAULT_MAX_YEAR_BUILT) -> Optional[Dict[str, Any]]:
    """PURE gate. Candidate only when ALL of: senior-exemption present and True,
    tenure captured and >= min, zip in the luxury set, year_built captured and <= max.
    Any missing capture -> None (honest silence)."""
    if _senior(parcel) is not True:
        return None                                  # not captured or not senior
    zip_ = (parcel.get("zip") or "").strip()
    if not zip_ or zip_ not in luxury_zips:
        return None
    yb = parcel.get("year_built")
    yb = int(yb) if yb not in (None, "") and str(yb).isdigit() else None
    if yb is None or yb > max_year_built:
        return None
    tenure = _num(parcel.get("tenure_years"))
    if tenure is None or tenure < min_tenure_years:
        return None
    return {"parcel_apn": parcel.get("parcel_apn"), "county_fips": parcel.get("county_fips"),
            "zip": zip_, "senior": True, "tenure_years": tenure, "year_built": yb,
            "confidence": _confidence(tenure, yb)}


# ---------------------------------------------------------------------------
# CENSUS
# ---------------------------------------------------------------------------
def census(parcels: List[Dict[str, Any]], luxury_zips: set,
           min_tenure_years: float = DEFAULT_MIN_TENURE_YEARS,
           max_year_built: int = DEFAULT_MAX_YEAR_BUILT) -> Dict[str, Dict[str, Any]]:
    """Per-county readiness. COVERED requires the load-bearing 65+ proxy captured on
    >=1 parcel AND a luxury-zip set. Reports each capture independently so the open
    gap is legible."""
    by_county: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in parcels:
        by_county[p.get("county_fips")].append(p)

    luxury_defined = bool(luxury_zips)
    out: Dict[str, Dict[str, Any]] = {}
    for county, ps in by_county.items():
        senior_cap = sum(1 for p in ps if _senior(p) is not None)
        yb_cap = sum(1 for p in ps if str(p.get("year_built") or "").isdigit())
        ten_cap = sum(1 for p in ps if _num(p.get("tenure_years")) is not None)
        in_lux = sum(1 for p in ps if (p.get("zip") or "").strip() in luxury_zips) if luxury_defined else 0
        candidates = sum(1 for p in ps if evaluate(p, luxury_zips, min_tenure_years, max_year_built))
        covered = senior_cap > 0 and luxury_defined
        entry = {"county_fips": county, "parcels": len(ps),
                 "senior_exemption_captured": senior_cap, "year_built_captured": yb_cap,
                 "tenure_captured": ten_cap, "luxury_zip_defined": luxury_defined,
                 "in_luxury_zip": in_lux, "candidates": candidates, "covered": covered}
        if not covered:
            gaps = []
            if senior_cap == 0:
                gaps.append("senior/homestead exemption (65+ proxy) NOT captured")
            if not luxury_defined:
                gaps.append("luxury-zip set undefined")
            if yb_cap == 0:
                gaps.append("year_built NOT captured")
            if ten_cap == 0:
                gaps.append("tenure NOT captured")
            entry["gap"] = "; ".join(gaps)
            entry["first_task"] = ("ingest-side: stow senior/homestead exemption flag + yr_blt + "
                                   "last-sale date from ReportAll (docs/enrich_priority_reportall_capture.md)")
        out[county] = entry
    return out


def load_parcel_records(cur, tenant_id: str) -> List[Dict[str, Any]]:
    """Load owner/parcel facts from owner_leads.raw_meta (tenant-scoped). Reads the
    keys the ReportAll capture SHOULD write: senior_exemption, yr_blt, last_sale_date
    (+ situs_zip already present). Today those are absent -> None -> census gap; the
    moment they land, this loader arms the adapter with NO code change."""
    cur.execute(
        """select county_fips, parcel_apn,
                  raw_meta->>'situs_zip'         as zip,
                  raw_meta->>'senior_exemption'  as senior_exemption,
                  raw_meta->>'yr_blt'            as year_built,
                  raw_meta->>'last_sale_date'    as last_sale_date
           from landos.owner_leads
           where tenant_id=%s and parcel_apn is not null and raw_meta is not null""",
        (tenant_id,))
    return [{"county_fips": r[0], "parcel_apn": r[1], "zip": r[2],
             "senior_exemption": r[3], "year_built": r[4],
             "tenure_years": _tenure_from_sale_date(r[5])} for r in cur.fetchall()]


def census_live(cur, tenant_id: str, luxury_zips: Optional[set] = None) -> Dict[str, Dict[str, Any]]:
    """Per-county pre-downsizing census against the live spine; luxury set defaults
    to the CRM config (option_sets)."""
    zips = luxury_zips if luxury_zips is not None else load_luxury_zips(cur, tenant_id)
    return census(load_parcel_records(cur, tenant_id), zips)


class AgingOwnerLuxuryAdapter(DiscoveryAdapter):
    """Derived DiscoveryAdapter: flags 65+/long-tenure owners of dated homes in luxury
    zips (pre-downsizing pipeline). Census-gated; DISARMED today (65+ proxy uncaptured)."""

    source_type = "aging_owner_luxury"
    county_fips = "*"
    access_mode = "derived"
    access_path = "landos.owner_leads.raw_meta (senior exemption / yr_blt / last-sale) + option_sets luxury_zip"
    recipe_hint = "R5"          # nurture / pre-downsizing outreach

    def __init__(self, tenant_id: str, parcels: Optional[List[Dict[str, Any]]] = None,
                 luxury_zips: Optional[set] = None,
                 min_tenure_years: float = DEFAULT_MIN_TENURE_YEARS,
                 max_year_built: int = DEFAULT_MAX_YEAR_BUILT, county_fips: str = "*"):
        super().__init__(tenant_id)
        self.parcels = parcels or []
        self.luxury_zips = set(luxury_zips or [])
        self.min_tenure_years = min_tenure_years
        self.max_year_built = max_year_built
        self.county_fips = county_fips
        self.last_census: Dict[str, Dict[str, Any]] = {}
        self.non_matches = 0

    def fetch(self) -> List[DiscoverySignal]:
        recs = self.parcels
        if self.county_fips != "*":
            recs = [p for p in recs if p.get("county_fips") == self.county_fips]
        self.last_census = census(recs, self.luxury_zips, self.min_tenure_years, self.max_year_built)

        signals: List[DiscoverySignal] = []
        self.non_matches = 0
        for p in recs:
            if not self.last_census.get(p.get("county_fips"), {}).get("covered"):
                continue                                # disarmed county — never fabricate
            ev = evaluate(p, self.luxury_zips, self.min_tenure_years, self.max_year_built)
            if not ev:
                self.non_matches += 1
                continue
            signals.append({
                "source_ref": f"{ev['county_fips']}:{ev['parcel_apn']}:aging_owner_luxury",
                "source_url": None, "owner_name": None, "signal_date": None,
                "confidence": ev["confidence"], "parcel_apn": ev["parcel_apn"],
                "raw_payload": {"type": "aging_owner_luxury", "parcel_apn": ev["parcel_apn"],
                                "zip": ev["zip"], "senior": True,
                                "tenure_years": ev["tenure_years"], "year_built": ev["year_built"]},
            })
        return signals

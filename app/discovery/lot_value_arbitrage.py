"""
lot_value_arbitrage.py — DiscoveryAdapter: Bryan's luxury teardown play as a query.

THE THESIS. In a luxury zip, when the ASSESSED LAND is worth most of the parcel's
total assessed value, the structure on it is functionally a teardown — the dirt is
the asset, the house is a liability the market will scrape. An old house on that
dirt, held a long time by an owner with a low basis, is the classic scrape-and-
rebuild lot: the builder's exit is the land, and the owner is a motivated seller
the moment someone names a number. This is pure assessor math on data we already
touch. $0. This adapter emits that signal.

    signal: {type: lot_value_arbitrage, parcel_apn, land_ratio, zip}
            (+ tier, year_built, tenure_years, confidence)

    candidate := land_assessed / total_assessed >= threshold   (land dominates)
                 AND zip in the luxury-zip set
                 AND year_built <= max_year_built                (old structure)
                 AND tenure_years >= min_tenure_years            (long hold)

CENSUS FIRST (the load-bearing discipline). Every gate above needs a field that
may not be captured for a county. This adapter is a DIFF over assessor facts, and
it NEVER fabricates the signal: it censuses each parcel's captured facts and
DISARMS (emits nothing, reports the gap) wherever the land/total split, year_built,
tenure, or luxury-zip set is absent — the same hunt_core "$0 census discipline"
as exemption_drop: covered only by evidence.

    LIVE STATE 2026-07-08 (this build): the spine census is a THREE-part gap, so
    EVERY parcel is disarmed today:
      1. LAND-vs-TOTAL SPLIT — ABSENT. No land_assessed / improvement_assessed
         field exists anywhere (typed col or JSONB). Only `sev` (State Equalized
         Value = TOTAL, ~50% cash value) is captured. The land_ratio, the defining
         term, is UNCOMPUTABLE. This is the hard blocker.
      2. year_built — ABSENT (0 typed, 0 JSONB key).
      3. tenure — ABSENT (no subject-parcel acquisition/deed date; purchase_price 0).
    Plus the luxury-zip SET is undefined on the spine (no segment/luxury config),
    and the situs zips actually captured are MI/IN tax-sale (small-metro/rural),
    not luxury markets. So there is nothing to arm on yet — honestly.

    THE REAL FIRST TASK is ingest-side and specific: the ReportAll parcel fetch
    that already runs for the enriched parcels (scripts/enrich_owner_chain.py,
    `county_rest_attributes` cached at ~line 273, then DISCARDED) returns
    land_value / improvement_value (or mkt_val_land / mkt_val_bldg), yr_blt, and a
    last-sale date. Stow those alongside the vintage. THEN this adapter lights up
    with NO code change. Separately, the luxury-zip set must be defined (config /
    option_sets field_key='luxury_zip', or a $/sqft threshold over comps). The diff
    logic below is proven by tests against synthetic parcels so it is ready the
    moment the fields land.

    EXPECTED PARCEL SHAPE (the ingest contract this adapter reads):
        {"county_fips": "26125", "parcel_apn": "...", "zip": "48009",
         "land_assessed": 420000, "total_assessed": 480000,
         "year_built": 1948, "tenure_years": 31}
    A parcel WITHOUT land_assessed/total_assessed is NOT-CAPTURED — it cannot
    evidence the ratio, so it never yields a candidate (silence, never a false hit).

MATCHING & LEAD CREATION belong to the pipeline (scripts/ingest_discovery.py),
per the DiscoveryAdapter contract — this adapter's only job is the signal.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .base import DiscoveryAdapter, DiscoverySignal

# tunable defaults (operator-owned). A teardown is land-dominant; >=0.50 means the
# land is the majority of assessed value, >=0.75 means the structure is nearly
# worthless dirt-relative. "old" and "long hold" reinforce scrape-likelihood.
DEFAULT_THRESHOLD = 0.50
DEFAULT_MAX_YEAR_BUILT = 1980
DEFAULT_MIN_TENURE_YEARS = 15

_NUM_RE = re.compile(r"^-?[0-9]+(\.[0-9]+)?$")


def _num(v: Any) -> Optional[float]:
    """Guarded numeric parse — raw assessor amounts arrive as 420000, '420,000',
    '$480,000', or 'TBA'. Strip $/,/space; return the number or None (never raise,
    never coerce a bad string to 0 — a 0 would fake a land_ratio)."""
    if v is None:
        return None
    s = re.sub(r"[$,\s]", "", str(v))
    return float(s) if _NUM_RE.match(s) else None


def land_ratio(land_assessed: Any, total_assessed: Any) -> Optional[float]:
    """PURE ratio. land / total, guarded: None when either is missing/unparseable,
    total <= 0, or land exceeds total by more than a rounding margin (bad data —
    never a >1 ratio). Returns a float in (0, ~1]."""
    land = _num(land_assessed)
    total = _num(total_assessed)
    if land is None or total is None or total <= 0 or land < 0:
        return None
    if land > total * 1.05:
        return None                              # land > total = corrupt row, not a signal
    return round(min(land / total, 1.0), 4)


def _tier(ratio: float) -> str:
    """How decisively land dominates value."""
    if ratio >= 0.75:
        return "high"                            # structure is nearly worthless dirt-relative
    return "med"


def _confidence(ratio: float, year_built: Optional[int], tenure_years: Optional[float]) -> float:
    """Score a teardown candidate. Primary = how far land dominates value; modest
    lifts for an older structure (more likely to scrape) and a long hold (low basis,
    more likely to sell). Capped so a flag never reads certainty."""
    base = 0.85 if ratio >= 0.90 else (0.75 if ratio >= 0.75 else 0.60)
    if year_built is not None:
        base += 0.07 if year_built <= 1960 else (0.04 if year_built <= 1980 else 0.0)
    if tenure_years is not None and tenure_years >= 25:
        base += 0.03
    return round(min(base, 0.95), 2)


def _has_valuation_capture(parcel: Dict[str, Any]) -> bool:
    """A parcel EVIDENCES the land ratio only if BOTH assessor parts parse. Absent
    or unparseable = not-captured (silence), NOT a computable ratio (which would
    fake the signal). This is the census gate in one predicate."""
    return (_num(parcel.get("land_assessed")) is not None
            and _num(parcel.get("total_assessed")) is not None
            and _num(parcel.get("total_assessed")) > 0)


def evaluate(parcel: Dict[str, Any], luxury_zips: set,
             threshold: float = DEFAULT_THRESHOLD,
             max_year_built: int = DEFAULT_MAX_YEAR_BUILT,
             min_tenure_years: float = DEFAULT_MIN_TENURE_YEARS) -> Optional[Dict[str, Any]]:
    """PURE gate for one parcel. Return a candidate event when ALL of: land ratio
    computable and >= threshold, zip in the luxury set, year_built captured and
    <= max (old), tenure captured and >= min (long hold). Any missing capture ->
    None (honest silence — the census, not an exception, explains why)."""
    ratio = land_ratio(parcel.get("land_assessed"), parcel.get("total_assessed"))
    if ratio is None or ratio < threshold:
        return None
    zip_ = (parcel.get("zip") or "").strip()
    if not zip_ or zip_ not in luxury_zips:
        return None
    yb = parcel.get("year_built")
    yb = int(yb) if yb not in (None, "") and str(yb).isdigit() else None
    if yb is None or yb > max_year_built:
        return None                              # not captured or not old -> not a candidate
    tenure = _num(parcel.get("tenure_years"))
    if tenure is None or tenure < min_tenure_years:
        return None                              # not captured or not a long hold
    return {
        "parcel_apn": parcel.get("parcel_apn"), "county_fips": parcel.get("county_fips"),
        "zip": zip_, "land_ratio": ratio, "tier": _tier(ratio),
        "year_built": yb, "tenure_years": tenure,
        "confidence": _confidence(ratio, yb, tenure),
    }


# ---------------------------------------------------------------------------
# CENSUS — measured, never assumed ($0). Reports WHICH of the gaps is open.
# ---------------------------------------------------------------------------
def census(parcels: List[Dict[str, Any]], luxury_zips: set,
           threshold: float = DEFAULT_THRESHOLD,
           max_year_built: int = DEFAULT_MAX_YEAR_BUILT,
           min_tenure_years: float = DEFAULT_MIN_TENURE_YEARS) -> Dict[str, Dict[str, Any]]:
    """Per-county census of teardown-arbitrage readiness. COVERED requires the
    load-bearing evidence: at least one parcel with the land/total split captured.
    Reports each capture independently (valuation / year_built / tenure / luxury
    zip) so the OPEN gap is legible — never a bare 'covered: false'."""
    by_county: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for p in parcels:
        by_county[p.get("county_fips")].append(p)

    out: Dict[str, Dict[str, Any]] = {}
    luxury_defined = bool(luxury_zips)
    for county, ps in by_county.items():
        n = len(ps)
        val_cap = sum(1 for p in ps if _has_valuation_capture(p))
        yb_cap = sum(1 for p in ps if str(p.get("year_built") or "").isdigit())
        ten_cap = sum(1 for p in ps if _num(p.get("tenure_years")) is not None)
        in_lux = sum(1 for p in ps if (p.get("zip") or "").strip() in luxury_zips) if luxury_defined else 0
        candidates = sum(
            1 for p in ps
            if evaluate(p, luxury_zips, threshold, max_year_built, min_tenure_years))
        covered = val_cap > 0 and luxury_defined
        entry = {
            "county_fips": county, "parcels": n,
            "valuation_split_captured": val_cap, "year_built_captured": yb_cap,
            "tenure_captured": ten_cap, "luxury_zip_defined": luxury_defined,
            "in_luxury_zip": in_lux, "candidates": candidates, "covered": covered,
        }
        if not covered:
            gaps = []
            if val_cap == 0:
                gaps.append("land/total assessed split NOT captured (only SEV=total)")
            if not luxury_defined:
                gaps.append("luxury-zip set undefined")
            if yb_cap == 0:
                gaps.append("year_built NOT captured")
            if ten_cap == 0:
                gaps.append("tenure NOT captured")
            entry["gap"] = "; ".join(gaps)
            entry["first_task"] = (
                "ingest-side: stow land_value/improvement_value + yr_blt + last-sale "
                "date from the ReportAll parcel fetch already run in "
                "scripts/enrich_owner_chain.py (county_rest_attributes, currently "
                "discarded); define the luxury-zip set (option_sets field_key='luxury_zip')")
        out[county] = entry
    return out


def load_parcel_records(cur, tenant_id: str) -> List[Dict[str, Any]]:
    """Load parcel assessor facts from the live spine into the adapter's record
    shape, REUSING the enrichment capture (landos.owner_leads.raw_meta). Today
    raw_meta carries `sev` (total) + `situs_zip` but NO land_value / yr_blt / last
    sale, so land_assessed/year_built/tenure come back None and the census reports
    the gap — this is the honest live wiring, not a stub."""
    cur.execute(
        """select county_fips, parcel_apn,
                  raw_meta->>'situs_zip'     as zip,
                  raw_meta->>'land_value'    as land_assessed,
                  raw_meta->>'sev'           as total_assessed,
                  raw_meta->>'yr_blt'        as year_built
           from landos.owner_leads
           where tenant_id = %s and parcel_apn is not null and raw_meta is not null""",
        (tenant_id,))
    return [
        {"county_fips": row[0], "parcel_apn": row[1], "zip": row[2],
         "land_assessed": row[3], "total_assessed": row[4],
         "year_built": row[5], "tenure_years": None}    # no subject-parcel sale date on the spine
        for row in cur.fetchall()
    ]


def census_live(cur, tenant_id: str, luxury_zips: Optional[set] = None) -> Dict[str, Dict[str, Any]]:
    """Per-county teardown-arbitrage census against the live spine."""
    return census(load_parcel_records(cur, tenant_id), luxury_zips or set())


class LotValueArbitrageAdapter(DiscoveryAdapter):
    """Derived DiscoveryAdapter: flags luxury-zip teardown lots by assessor math.
    access_mode='derived' — reads assessor facts ALREADY ingested (no external
    fetch), injected as parcels (the pipeline/runner loads them; tests inject
    synthetic ones). Census-gated: emits only where the land/total split is captured
    and a luxury-zip set is supplied; disarms + reports the gap everywhere else
    (today: everywhere)."""

    source_type = "lot_value_arbitrage"
    county_fips = "*"            # cross-county; the runner may pin a single county
    access_mode = "derived"     # derived from ingested assessor facts, not fetched
    access_path = "landos.owner_leads.raw_meta assessor split (ReportAll): land_value/sev/yr_blt"
    recipe_hint = "R6"          # luxury teardown / lot-value outreach

    def __init__(self, tenant_id: str, parcels: Optional[List[Dict[str, Any]]] = None,
                 luxury_zips: Optional[set] = None,
                 threshold: float = DEFAULT_THRESHOLD,
                 max_year_built: int = DEFAULT_MAX_YEAR_BUILT,
                 min_tenure_years: float = DEFAULT_MIN_TENURE_YEARS,
                 county_fips: str = "*"):
        super().__init__(tenant_id)
        self.parcels = parcels or []
        self.luxury_zips = set(luxury_zips or [])
        self.threshold = threshold
        self.max_year_built = max_year_built
        self.min_tenure_years = min_tenure_years
        self.county_fips = county_fips
        self.last_census: Dict[str, Dict[str, Any]] = {}
        self.non_matches = 0

    def fetch(self) -> List[DiscoverySignal]:
        """Census -> arm covered counties, disarm+log gaps elsewhere. Returns the
        lot_value_arbitrage signals (empty when every county is disarmed — today's
        state, since the land/total split is not captured anywhere)."""
        recs = self.parcels
        if self.county_fips != "*":
            recs = [p for p in recs if p.get("county_fips") == self.county_fips]
        self.last_census = census(recs, self.luxury_zips, self.threshold,
                                  self.max_year_built, self.min_tenure_years)

        signals: List[DiscoverySignal] = []
        self.non_matches = 0
        for p in recs:
            county = p.get("county_fips")
            if not self.last_census.get(county, {}).get("covered"):
                continue                                # disarmed county — never fabricate
            ev = evaluate(p, self.luxury_zips, self.threshold,
                          self.max_year_built, self.min_tenure_years)
            if not ev:
                self.non_matches += 1                   # diffable parcel, no arbitrage — logged
                continue
            signals.append({
                "source_ref": f"{ev['county_fips']}:{ev['parcel_apn']}:lot_value_arbitrage",
                "source_url": None,
                "owner_name": None,                     # pipeline resolves the owner
                "signal_date": None,                    # a state, not a dated event
                "confidence": ev["confidence"],
                "parcel_apn": ev["parcel_apn"],
                "raw_payload": {
                    "type": "lot_value_arbitrage",
                    "parcel_apn": ev["parcel_apn"],
                    "land_ratio": ev["land_ratio"],
                    "zip": ev["zip"],
                    "tier": ev["tier"],
                    "year_built": ev["year_built"],
                    "tenure_years": ev["tenure_years"],
                },
            })
        return signals

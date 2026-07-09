"""
estate_sale_ext.py — DiscoveryAdapter: text-mine estate-sale listings for MOTIVATION.

THE THESIS. An estate sale is a fact; the LISTING TEXT is the motive. "Assisted
living move", "memory care", "downsizing", "estate of the late...", "everything
must go" each name WHY the household is dissolving — and a care-facility move or a
death is a far stronger seller signal than a routine moving sale. We already ingest
estate-sale listings (source_type='estate_sale') with their full description/title
text. This adapter reads that text and emits motivation SUB-TAGS, turning a flat
"estate_sale" into a graded seller-intent signal. $0.

    signal: {type: estate_sale_ext, parcel_apn, motivation_tags:[...],
             motivation_classes:[...], confidence}

CENSUS FIRST. This adapter is a TEXT DIFF over listings already on the spine. It
ARMS today (unlike the disarmed sleepers): the census (lvdi 2026-07-08) found 91
estate_sale signals, ALL 91 carrying description/title/raw text, canon_parcel_apn
populated 91/91, and 12+ already hitting a motivation phrase. A listing with text
but NO motivation phrase is a non-match (logged, never fabricated into a tag).

MATCHING & LEAD CREATION belong to the pipeline — this adapter's only job is the
graded motivation signal keyed to the parcel (canon_parcel_apn).
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .base import DiscoveryAdapter, DiscoverySignal

# Motivation taxonomy: (sub_tag, class, weight, regex). ORDER is report-only; every
# matching tag is collected. `class` groups tags so multi-CLASS motivation (e.g. a
# care move AND a death) can lift confidence. Weights: a care-facility move or a
# death is the strongest seller intent; downsizing/relocating is medium; bare
# urgency ("must go") is weakest on its own.
_MOTIVATIONS = [
    ("memory_care",       "care",     0.88, r"memory[\s-]*care|alzheimer|dementia"),
    ("assisted_living",   "care",     0.85, r"assisted[\s-]*living|senior living|retirement (?:home|community)"),
    ("nursing_home",      "care",     0.85, r"nursing home|skilled nursing|long[\s-]*term care|convalescent"),
    ("deceased",          "estate",   0.82, r"deceased|passed away|passed on|the late |in memoriam"),
    ("estate_settlement", "estate",   0.80, r"estate of\b|settling (?:the|an) estate|estate liquidation|probate"),
    ("downsizing",        "downsize", 0.68, r"downsiz"),
    ("relocating",        "downsize", 0.62, r"relocat|moving (?:sale|out|to)|out[\s-]*of[\s-]*state"),
    ("must_go",           "urgency",  0.58, r"must go|everything must|must sell|priced to sell"),
    ("liquidation",       "urgency",  0.58, r"liquidat"),
]
_COMPILED = [(tag, cls, w, re.compile(rx, re.I)) for tag, cls, w, rx in _MOTIVATIONS]
_WEIGHT = {tag: w for tag, _c, w, _rx in _MOTIVATIONS}
_CLASS = {tag: c for tag, c, _w, _rx in _MOTIVATIONS}


def extract_motivations(text: Optional[str]) -> List[str]:
    """PURE extractor. Return the sorted motivation sub-tags present in the listing
    text (title + description + raw). Empty list when the text names no motivation —
    never a fabricated tag."""
    if not text:
        return []
    hits = {tag for tag, _cls, _w, rx in _COMPILED if rx.search(text)}
    return sorted(hits)


def _confidence(tags: List[str]) -> float:
    """Score seller intent by the strongest motivation present, with a small lift
    when motivations from MULTIPLE classes co-occur (e.g. a care move AND a death
    named in the same listing). Capped so a text match never reads certainty."""
    if not tags:
        return 0.0
    base = max(_WEIGHT[t] for t in tags)
    if len({_CLASS[t] for t in tags}) >= 2:
        base += 0.03
    return round(min(base, 0.92), 2)


def _listing_text(rec: Dict[str, Any]) -> str:
    return " ".join(str(rec.get(k) or "") for k in ("title", "description", "raw"))


# ---------------------------------------------------------------------------
# CENSUS — measured, never assumed ($0).
# ---------------------------------------------------------------------------
def census(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per-county census of motivation-mining readiness. Estate sales often carry no
    county_fips (they're geocoded downstream), so unmapped rows bucket under
    '(unmapped)'. COVERED requires evidence: >=1 listing with text AND a motivation
    tag. Reports listings scanned, with-text, with-apn, and motivated counts."""
    by_county: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_county[r.get("county_fips") or "(unmapped)"].append(r)

    out: Dict[str, Dict[str, Any]] = {}
    for county, recs in by_county.items():
        with_text = sum(1 for r in recs if _listing_text(r).strip())
        with_apn = sum(1 for r in recs if (r.get("parcel_apn") or "").strip())
        motivated = sum(1 for r in recs if extract_motivations(_listing_text(r)))
        entry = {"county_fips": county, "listings": len(recs), "with_text": with_text,
                 "with_apn": with_apn, "motivated": motivated, "covered": motivated >= 1}
        if not entry["covered"]:
            entry["gap"] = ("no listing text captured" if with_text == 0
                            else "listings carry text but none name a motivation phrase")
        out[county] = entry
    return out


def load_estate_sale_records(cur, tenant_id: str) -> List[Dict[str, Any]]:
    """Load estate_sale listings from the live spine. parcel_apn comes from
    raw_payload->>'canon_parcel_apn' (the top-level column is null on these rows);
    text from title/description/raw. NOTE: the discovery pool is tenant-agnostic —
    estate_sale/taxsale rows carry tenant_id NULL (tenantization happens at lead
    projection), so read the global pool plus any tenant-scoped rows."""
    cur.execute(
        """select county_fips,
                  coalesce(nullif(parcel_apn,''), raw_payload->>'canon_parcel_apn') as parcel_apn,
                  raw_payload->>'listing_id'  as listing_id,
                  raw_payload->>'title'       as title,
                  raw_payload->>'description' as description,
                  raw_payload->>'raw'         as raw,
                  raw_payload->>'postal_code' as zip
           from landos.discovery_signals
           where source_type='estate_sale' and (tenant_id=%s or tenant_id is null)""",
        (tenant_id,))
    cols = ("county_fips", "parcel_apn", "listing_id", "title", "description", "raw", "zip")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def census_live(cur, tenant_id: str) -> Dict[str, Dict[str, Any]]:
    """Per-county motivation-mining census against the live spine."""
    return census(load_estate_sale_records(cur, tenant_id))


class EstateSaleExtAdapter(DiscoveryAdapter):
    """Derived DiscoveryAdapter: grades estate_sale listings by motivation sub-tags.
    access_mode='derived' — reads listings ALREADY ingested (no external fetch).
    ARMS today: emits one estate_sale_ext signal per listing that names a motivation;
    logs listings with text but no motivation as non-matches."""

    source_type = "estate_sale_ext"
    county_fips = "*"
    access_mode = "derived"
    access_path = "landos.discovery_signals source_type='estate_sale' (listing text)"
    recipe_hint = "R4"          # death/downsizing -> heirs/estate outreach

    def __init__(self, tenant_id: str, records: Optional[List[Dict[str, Any]]] = None,
                 county_fips: str = "*"):
        super().__init__(tenant_id)
        self.records = records or []
        self.county_fips = county_fips
        self.last_census: Dict[str, Dict[str, Any]] = {}
        self.non_matches = 0
        self.skipped_no_ref = 0

    def fetch(self) -> List[DiscoverySignal]:
        recs = self.records
        if self.county_fips != "*":
            recs = [r for r in recs if r.get("county_fips") == self.county_fips]
        self.last_census = census(recs)

        signals: List[DiscoverySignal] = []
        self.non_matches = 0
        self.skipped_no_ref = 0
        for r in recs:
            tags = extract_motivations(_listing_text(r))
            if not tags:
                self.non_matches += 1                   # listing with text, no motivation
                continue
            apn = (r.get("parcel_apn") or "").strip() or None
            ref = r.get("listing_id") or apn
            if not ref:
                self.skipped_no_ref += 1                # no stable natural key -> honest skip, never a text-prefix collide
                continue
            signals.append({
                "source_ref": f"{ref}:estate_sale_ext",
                "source_url": None,
                "owner_name": None,
                "signal_date": None,
                "confidence": _confidence(tags),
                "parcel_apn": apn,
                "raw_payload": {
                    "type": "estate_sale_ext",
                    "parcel_apn": apn,
                    "motivation_tags": tags,
                    "motivation_classes": sorted({_CLASS[t] for t in tags}),
                    "zip": (r.get("zip") or None),
                },
            })
        return signals

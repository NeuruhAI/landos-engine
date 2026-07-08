"""
tangled_title.py — DiscoveryAdapter: parcels taxed to dead people (curative feedstock).

THE THESIS. When an owner dies and no probate is opened, the assessor keeps
mailing the tax bill to the decedent's name — the roll ossifies into "SMITH JOHN
ESTATE", "(LIFE ESTATE)", "ET AL". The title is tangled: no living grantor can
sign a deed, the heirs are un-probated, and the taxes quietly fall delinquent.
That is the highest-conviction motivated-seller state there is — a house nobody
can legally sell — and it is sitting in the owner-name string we already ingest.
$0. This adapter reads that name and emits the signal. (Fulmer's curative-title
model, encoded as a query.)

    signal: {type: heirs_of_tangled_title, parcel_apn, matched_pattern,
             has_probate_filing: bool}   (+ tier, tax_delinquent, confidence)

CENSUS FIRST (why the regex is TIGHT, not broad). A naive `owner_name ~ 'estate'`
is mostly FALSE POSITIVES on live MI data: it catches "PDS REAL ESTATE LLC"
(a company, not a decedent) and a broad `c/o` catches "OIL CO LLC". The live
census (lvdi, 2026-07-08) proved the genuine universe is small and specific:
    estate_of 8 · et_al 4 · life_estate 1  (= 13 on the CRM projection)
    ZERO 'HEIRS OF', ZERO 'DECEASED', ZERO true 'c/o' in this data.
    24 living TRUSTs are EXCLUDED — a revocable trust is estate PLANNING that
    AVOIDS probate on purpose; it is the opposite of a tangled title.
So the patterns below carry anti-patterns ("real estate" excluded, "c/o"
requires the slash). HEIRS/DECEASED/c_o are kept because other counties/states
DO use them — they simply score zero here, honestly.

THE PROBATE CROSS-CHECK (and its honest data-gate). A tangled parcel with NO
matching probate filing is the strongest: the estate was never opened, so no
personal representative exists to convey. `has_probate_filing` is a real join to
landos.discovery_signals (source_type ~ 'probate'). TODAY the probate universe on
the spine is ~1 APN, so the flag reads false for essentially every tangled parcel
— the discriminator is CORRECT but INERT until probate ingest fills (same gate as
the distress recipe's probate axis). census() surfaces this per county rather than
hiding it.

Unlike the exemption_drop sleeper (input absent -> disarmed), this adapter's input
(owner_name) is PRESENT on the spine, so it ARMS today: it emits the 13 live hits.

MATCHING & LEAD CREATION belong to the pipeline (scripts/ingest_discovery.py),
per the DiscoveryAdapter contract — this adapter's only job is the signal.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Dict, List, Optional

from .base import DiscoveryAdapter, DiscoverySignal

# Ordered tangled-title patterns. ORDER MATTERS: life_estate before estate_of so
# "(LIFE ESTATE)" classifies as the more specific remainderman signal, not the
# generic decedent's-estate one. Each: (name, tier, match regex, anti regex|None).
# Anti-patterns kill the census's proven false positives ("REAL ESTATE" company,
# "CO LLC" mistaken for "c/o"). A plain living trust matches NONE -> excluded.
_PATTERNS = [
    ("life_estate", "high", r"life\s+estate|\(l/?e\)|\bl/e\b", None),
    ("estate_of",   "high", r"\bestate\b",                     r"\breal\s+estate\b"),
    ("heirs_of",    "high", r"\bheirs?\b",                      None),
    ("deceased",    "high", r"\bdec(eased|'?d)\b",             None),
    ("et_al",       "med",  r"\bet\s+(al|ux)\b",               None),
    ("c_o",         "med",  r"\bc/o\b",                        None),
]
_COMPILED = [
    (name, tier, re.compile(pat, re.I), re.compile(anti, re.I) if anti else None)
    for name, tier, pat, anti in _PATTERNS
]
_HIGH_TIERS = {"life_estate", "estate_of", "heirs_of", "deceased"}


def classify_owner_name(owner_name: Optional[str]) -> Optional[Dict[str, str]]:
    """PURE classifier. Return {matched_pattern, tier} for the first tangled-title
    pattern the name matches (anti-pattern respected), else None. A living trust,
    a real-estate company, or an ordinary "SURNAME GIVEN" name returns None —
    never a fabricated hit (the whole census-first point)."""
    if not owner_name:
        return None
    name = owner_name.strip()
    if not name:
        return None
    for pattern, tier, rx, anti in _COMPILED:
        if rx.search(name) and not (anti and anti.search(name)):
            return {"matched_pattern": pattern, "tier": tier}
    return None


def _confidence(tier: str, tax_delinquent: bool, has_probate_filing: bool) -> float:
    """Score a tangled parcel. HIGH-tier names (estate/heirs/deceased/life-estate)
    name a decedent outright; MED-tier (et_al/c_o) imply fractional/absent owners.
    Stack-boost when the taxes are ALSO delinquent (motivated + can't-convey). The
    strongest state is NO probate opened (nobody can legally sell) — so a false
    has_probate_filing lifts conviction. Capped so a flag can never read certainty."""
    base = 0.80 if tier == "high" else 0.55
    if tax_delinquent:
        base += 0.10                    # delinquency stacked on tangled title
    if not has_probate_filing:
        base += 0.05                    # estate never opened = curative feedstock
    return round(min(base, 0.95), 2)


# ---------------------------------------------------------------------------
# CENSUS — measured, never assumed ($0). Reports the probate data-gate honestly.
# ---------------------------------------------------------------------------
def census(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per-county census of the tangled-title universe. A record is one parcel:
    {county_fips, parcel_apn, owner_name, tax_delinquent, has_probate_filing}.
    Reports how many owner names were scanned, how many are tangled (by tier), the
    tax-delinquent and no-probate subsets, and the highest-conviction intersection.
    Where NO tangled parcel matched a probate filing, it flags the discriminator as
    inert (data-gated) rather than implying the no-probate subset is meaningful."""
    by_county: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_county[r.get("county_fips")].append(r)

    out: Dict[str, Dict[str, Any]] = {}
    for county, recs in by_county.items():
        scanned = len(recs)
        classed = [(r, classify_owner_name(r.get("owner_name"))) for r in recs]
        hits = [(r, c) for r, c in classed if c]
        high = sum(1 for _, c in hits if c["tier"] == "high")
        td = sum(1 for r, _ in hits if r.get("tax_delinquent"))
        no_probate = sum(1 for r, _ in hits if not r.get("has_probate_filing"))
        probate_matched = len(hits) - no_probate
        hi_conv = sum(1 for r, _ in hits
                      if r.get("tax_delinquent") and not r.get("has_probate_filing"))
        entry = {
            "county_fips": county, "scanned": scanned, "hits": len(hits),
            "high": high, "med": len(hits) - high, "tax_delinquent": td,
            "no_probate": no_probate, "probate_matched": probate_matched,
            "hi_conviction": hi_conv,
        }
        if len(hits) and probate_matched == 0:
            entry["probate_gate"] = (
                "no probate filing matched any tangled parcel — has_probate_filing "
                "is false for all; the no-probate discriminator is INERT until "
                "probate ingest fills (landos.discovery_signals source_type~probate)")
        out[county] = entry
    return out


def load_owner_records(cur, tenant_id: str) -> List[Dict[str, Any]]:
    """Load owner-name records from the live spine into the adapter's record shape.
    Reads the assessor vested-owner projection (public.acquisition_leads.owner_name),
    co-locating tax_delinquent (from emerging_signals) and computing
    has_probate_filing by a REAL parcel join to landos.discovery_signals probate
    rows — not an in-row text guess. Owner_leads carries a few more not-yet-projected
    names; the projection is the actionable surface with the cross-check fields."""
    cur.execute(
        """
        select a.county_fips, a.parcel_apn, a.owner_name,
               coalesce(a.emerging_signals::text ilike '%%taxsale%%', false)
                   as tax_delinquent,
               exists (
                   select 1 from landos.discovery_signals d
                   where d.source_type ilike '%%probate%%'
                     and d.county_fips = a.county_fips
                     and d.parcel_apn  = a.parcel_apn
               ) as has_probate_filing
        from public.acquisition_leads a
        where a.owner_name is not null and a.owner_name <> ''
          and a.tenant_id = %s
        """,
        (tenant_id,),
    )
    return [
        {"county_fips": row[0], "parcel_apn": row[1], "owner_name": row[2],
         "tax_delinquent": bool(row[3]), "has_probate_filing": bool(row[4])}
        for row in cur.fetchall()
    ]


def census_live(cur, tenant_id: str) -> Dict[str, Dict[str, Any]]:
    """Per-county tangled-title census against the live spine."""
    return census(load_owner_records(cur, tenant_id))


class TangledTitleAdapter(DiscoveryAdapter):
    """Derived DiscoveryAdapter: classifies owner names for tangled-title patterns.
    access_mode='derived' — reads owner names ALREADY ingested (no external fetch),
    injected as owner_records (the pipeline/runner loads them; tests inject synthetic
    ones). ARMS today (owner_name is present on the spine): emits one signal per
    tangled parcel, stack-boosted when tax-delinquent, with the probate cross-check."""

    source_type = "heirs_of_tangled_title"
    county_fips = "*"            # cross-county; the runner may pin a single county
    access_mode = "derived"     # derived from ingested owner names, not fetched
    access_path = "public.acquisition_leads.owner_name (assessor vested-owner projection)"
    recipe_hint = "R4"          # heirs/estate outreach — same family as probate

    def __init__(self, tenant_id: str, owner_records: Optional[List[Dict[str, Any]]] = None,
                 county_fips: str = "*"):
        super().__init__(tenant_id)
        self.owner_records = owner_records or []
        self.county_fips = county_fips
        self.last_census: Dict[str, Dict[str, Any]] = {}

    def fetch(self) -> List[DiscoverySignal]:
        """Classify every owner name; emit a heirs_of_tangled_title signal per hit.
        Non-tangled names (living owners, trusts, real-estate cos) yield nothing —
        never fabricated. Returns the live tangled parcels (13 on the spine today)."""
        recs = self.owner_records
        if self.county_fips != "*":
            recs = [r for r in recs if r.get("county_fips") == self.county_fips]
        self.last_census = census(recs)

        signals: List[DiscoverySignal] = []
        for r in recs:
            cls = classify_owner_name(r.get("owner_name"))
            if not cls:
                continue
            td = bool(r.get("tax_delinquent"))
            has_probate = bool(r.get("has_probate_filing"))
            apn = r.get("parcel_apn")
            county = r.get("county_fips")
            signals.append({
                "source_ref": f"{county}:{apn}:tangled_title",
                "source_url": None,
                "owner_name": r.get("owner_name"),
                "signal_date": None,        # a state, not a dated event
                "confidence": _confidence(cls["tier"], td, has_probate),
                "parcel_apn": apn,
                "raw_payload": {
                    "type": "heirs_of_tangled_title",
                    "parcel_apn": apn,
                    "matched_pattern": cls["matched_pattern"],
                    "has_probate_filing": has_probate,
                    "tier": cls["tier"],
                    "tax_delinquent": td,
                },
            })
        return signals

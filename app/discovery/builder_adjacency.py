"""
builder_adjacency.py — DiscoveryAdapter: match active builders to land/teardown lots.

THE THESIS. A builder pulling permits in a zip is a KNOWN, MOTIVATED cash buyer for
the teardown and vacant-land candidates in that same geography. If we know who is
actively building where, every land/teardown candidate in that footprint gets a
ready-made exit (and a warm dispo call). Permit clusters (Census BPS + county permit
portals) name the builders; geography joins them to our candidates. $0 once the
permits are ingested.

    signal: {type: builder_adjacency, parcel_apn, zip, top_builder,
             cluster_size, builders:[{name,permits}], confidence}

CENSUS FIRST. This is a JOIN between two ingested sets: permit records and candidate
parcels. It NEVER fabricates a builder: it censuses whether permits exist and DISARMS
where they don't.

    LIVE STATE 2026-07-08: DISARMED. There is NO permit source on the spine — the
    discovery_signals source_types are taxsale/estate_sale/graph_*/probate_in only;
    no permit table, no 'permit'/'builder' column anywhere. So there is nothing to
    match on — honestly.

    REAL FIRST TASK (ingest-side, landos-ingest — repo firewall): stage permit
    records (builder/applicant name, situs zip, permit_type, issued_date) from
    Census BPS (county-level new-construction counts) + county permit portals, as
    landos.discovery_signals source_type='permit' (or a permits table). THEN this
    adapter arms with no code change (the match logic below is proven vs synthetic
    permits). The engine never fetches raw sources.

MATCHING & LEAD CREATION belong to the pipeline — this adapter's only job is the signal.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from .base import DiscoveryAdapter, DiscoverySignal

DEFAULT_MIN_PERMITS = 2          # a "cluster" = an actively-building builder in a zip
NEW_BUILD_TYPES = {"new", "new_construction", "single_family", "sfr", "residential_new", "demo_rebuild"}


def _norm_builder(name: Optional[str]) -> Optional[str]:
    return " ".join(str(name).split()).upper() if name and str(name).strip() else None


def active_builders(permits: List[Dict[str, Any]], min_permits: int = DEFAULT_MIN_PERMITS,
                    since_year: Optional[int] = None) -> Dict[str, List[Dict[str, Any]]]:
    """PURE. Cluster permits into active (builder, zip) pairs. A pair is active when it
    has >= min_permits permits (optionally within/after since_year). Returns
    {zip: [{builder, permits}, ...]} sorted by volume. Non-new-build permit_types are
    ignored (a re-roof permit is not a builder-buyer signal)."""
    counts: Dict[tuple, int] = defaultdict(int)
    for p in permits:
        b = _norm_builder(p.get("builder"))
        z = (p.get("zip") or "").strip()
        if not b or not z:
            continue
        ptype = str(p.get("permit_type") or "").strip().lower()
        if ptype and ptype not in NEW_BUILD_TYPES:
            continue
        if since_year is not None:
            yr = p.get("issued_year")
            if yr is None or int(yr) < since_year:
                continue
        counts[(b, z)] += 1
    by_zip: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for (b, z), n in counts.items():
        if n >= min_permits:
            by_zip[z].append({"builder": b, "permits": n})
    for z in by_zip:
        by_zip[z].sort(key=lambda x: (-x["permits"], x["builder"]))
    return dict(by_zip)


def _confidence(cluster_size: int, top_permits: int) -> float:
    """More distinct active builders in the zip + a higher top-builder volume = a
    deeper, more reliable buyer pool. Capped."""
    base = 0.55 + 0.08 * min(cluster_size, 3) + 0.03 * min(top_permits, 5)
    return round(min(base, 0.90), 2)


def match(candidates: List[Dict[str, Any]], builders_by_zip: Dict[str, List[Dict[str, Any]]]
          ) -> List[Dict[str, Any]]:
    """PURE join. For each candidate parcel, attach the active builders in its zip.
    Returns one event per candidate that has >=1 active builder (others drop out)."""
    events = []
    for c in candidates:
        z = (c.get("zip") or "").strip()
        builders = builders_by_zip.get(z) or []
        if not builders:
            continue
        events.append({
            "parcel_apn": c.get("parcel_apn"), "county_fips": c.get("county_fips"), "zip": z,
            "builders": builders, "top_builder": builders[0]["builder"],
            "cluster_size": len(builders),
            "confidence": _confidence(len(builders), builders[0]["permits"]),
        })
    return events


# ---------------------------------------------------------------------------
# CENSUS
# ---------------------------------------------------------------------------
def census(permits: List[Dict[str, Any]], candidates: List[Dict[str, Any]],
           min_permits: int = DEFAULT_MIN_PERMITS) -> Dict[str, Any]:
    """Single-scope census (permits are the load-bearing input). COVERED requires
    >=1 active builder cluster AND >=1 candidate parcel to match. Reports the gap
    when permits are absent."""
    bz = active_builders(permits, min_permits)
    matches = match(candidates, bz)
    covered = bool(bz) and bool(candidates)
    entry = {"permit_records": len(permits), "candidate_parcels": len(candidates),
             "active_builder_zips": len(bz),
             "active_builders": sum(len(v) for v in bz.values()),
             "matches": len(matches), "covered": covered}
    if not covered:
        gaps = []
        if not permits:
            gaps.append("no permit records ingested")
        elif not bz:
            gaps.append("permits present but no builder reaches the cluster threshold")
        if not candidates:
            gaps.append("no candidate parcels")
        entry["gap"] = "; ".join(gaps)
        entry["first_task"] = ("landos-ingest: stage permit records (builder/zip/type/date) from "
                               "Census BPS + county permit portals as discovery_signals source_type='permit'")
    return entry


def load_permit_records(cur, tenant_id: str) -> List[Dict[str, Any]]:
    """Load permit records from the spine. Today source_type='permit' has 0 rows, so
    this returns [] and the census reports the gap — honest live wiring, not a stub."""
    cur.execute(
        """select raw_payload->>'builder'      as builder,
                  coalesce(raw_payload->>'zip', raw_payload->>'postal_code') as zip,
                  raw_payload->>'permit_type'   as permit_type,
                  raw_payload->>'issued_year'   as issued_year
           from landos.discovery_signals
           where source_type='permit' and (tenant_id=%s or tenant_id is null)""",
        (tenant_id,))
    return [{"builder": r[0], "zip": r[1], "permit_type": r[2], "issued_year": r[3]}
            for r in cur.fetchall()]


def load_candidate_parcels(cur, tenant_id: str) -> List[Dict[str, Any]]:
    """Land/teardown candidate parcels with a zip (owner_leads situs)."""
    cur.execute(
        """select county_fips, parcel_apn, raw_meta->>'situs_zip' as zip
           from landos.owner_leads
           where tenant_id=%s and parcel_apn is not null and raw_meta->>'situs_zip' is not null""",
        (tenant_id,))
    return [{"county_fips": r[0], "parcel_apn": r[1], "zip": r[2]} for r in cur.fetchall()]


def census_live(cur, tenant_id: str) -> Dict[str, Any]:
    return census(load_permit_records(cur, tenant_id), load_candidate_parcels(cur, tenant_id))


class BuilderAdjacencyAdapter(DiscoveryAdapter):
    """Derived DiscoveryAdapter: joins active-builder permit clusters to candidate
    parcels by zip. Census-gated; DISARMED today (no permit source ingested)."""

    source_type = "builder_adjacency"
    county_fips = "*"
    access_mode = "derived"
    access_path = "landos.discovery_signals source_type='permit' (absent) x owner_leads candidates"
    recipe_hint = "R7"          # dispo / builder-buyer outreach

    def __init__(self, tenant_id: str, permits: Optional[List[Dict[str, Any]]] = None,
                 candidates: Optional[List[Dict[str, Any]]] = None,
                 min_permits: int = DEFAULT_MIN_PERMITS):
        super().__init__(tenant_id)
        self.permits = permits or []
        self.candidates = candidates or []
        self.min_permits = min_permits
        self.last_census: Dict[str, Any] = {}
        self.non_matches = 0

    def fetch(self) -> List[DiscoverySignal]:
        self.last_census = census(self.permits, self.candidates, self.min_permits)
        self.non_matches = 0
        if not self.last_census.get("covered"):
            return []                                   # disarmed — never fabricate a builder
        bz = active_builders(self.permits, self.min_permits)
        matched_apns = set()
        signals: List[DiscoverySignal] = []
        for ev in match(self.candidates, bz):
            matched_apns.add(ev["parcel_apn"])
            signals.append({
                "source_ref": f"{ev['county_fips']}:{ev['parcel_apn']}:builder_adjacency",
                "source_url": None, "owner_name": None, "signal_date": None,
                "confidence": ev["confidence"], "parcel_apn": ev["parcel_apn"],
                "raw_payload": {"type": "builder_adjacency", "parcel_apn": ev["parcel_apn"],
                                "zip": ev["zip"], "top_builder": ev["top_builder"],
                                "cluster_size": ev["cluster_size"], "builders": ev["builders"]},
            })
        self.non_matches = sum(1 for c in self.candidates if c.get("parcel_apn") not in matched_apns)
        return signals

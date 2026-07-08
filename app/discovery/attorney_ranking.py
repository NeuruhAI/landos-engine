"""
attorney_ranking.py — DiscoveryAdapter: rank estate attorneys by case volume.

THE THESIS. An attorney who appears as attorney-of-record on many estate / probate /
legal-notice matters controls deal flow: each case is a household dissolving an
estate, and one warm relationship with a high-volume estate attorney is a channel to
dozens of future sellers. Ranking attorneys by case volume surfaces the trust-node
targets — the relationships worth building. $0 (aggregation over already-extracted
attorney-of-record).

    signal (ENTITY, not parcel): {type: attorney_ranking, attorney_name, case_count,
             case_types:[...], parcels:[...], rank, confidence}
    NOTE: this is a NODE signal — parcel_apn is null; the parcels a top attorney
    touches ride in the payload. This is a deliberate deviation from the usual
    parcel-keyed shape, because the deliverable feeds the trust-node channel.

CENSUS FIRST + THE GATE. Ranking needs attorney-of-record extracted at volume, which
is the job of t5's 1.4 (legal-notice attorney extraction). This adapter NEVER
fabricates a ranking: it censuses how many attorneys are extracted and DISARMS below
a rankable distribution.

    LIVE STATE 2026-07-08: DISARMED — the t5 1.4 gate is NOT met. attorney-of-record
    is populated on exactly 1 row / 1 distinct attorney on the spine ("Hesser, Randall
    Gary", a probate_in signal). You cannot rank 1 attorney. So the adapter reports
    the gate-unmet gap and emits nothing.

    REAL FIRST TASK (upstream): deliver t5 1.4 — extract attorney-of-record from
    probate/legal-notice text across the discovery pool so >=2 attorneys carry cases.
    THEN this adapter arms with no code change (ranking logic proven vs synthetic).

MATCHING & LEAD CREATION belong to the pipeline — this adapter's only job is the ranking.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from .base import DiscoveryAdapter, DiscoverySignal

DEFAULT_MIN_CASES = 1            # emit attorneys with >= this many cases
DEFAULT_MIN_DISTINCT = 2        # a ranking needs a distribution (can't rank 1)


def _norm_attorney(name: Optional[str]) -> Optional[str]:
    return " ".join(str(name).split()).title() if name and str(name).strip() else None


def rank_attorneys(records: List[Dict[str, Any]], min_cases: int = DEFAULT_MIN_CASES
                   ) -> List[Dict[str, Any]]:
    """PURE. Aggregate case records by attorney-of-record, count distinct cases,
    collect case types and touched parcels, and rank by volume (desc). Records with
    no attorney are ignored (never a fabricated node). Returns [] when nothing ranks."""
    by_atty: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"cases": set(), "types": set(), "parcels": set()})
    for r in records:
        atty = _norm_attorney(r.get("attorney"))
        if not atty:
            continue
        agg = by_atty[atty]
        agg["cases"].add(r.get("case_ref") or r.get("parcel_apn") or id(r))
        if r.get("case_type"):
            agg["types"].add(str(r["case_type"]))
        if r.get("parcel_apn"):
            agg["parcels"].add(r["parcel_apn"])
    ranked = [
        {"attorney_name": atty, "case_count": len(a["cases"]),
         "case_types": sorted(a["types"]), "parcels": sorted(a["parcels"])}
        for atty, a in by_atty.items() if len(a["cases"]) >= min_cases
    ]
    ranked.sort(key=lambda x: (-x["case_count"], x["attorney_name"]))
    for i, row in enumerate(ranked, 1):
        row["rank"] = i
    return ranked


def _confidence(case_count: int, rank: int) -> float:
    """A deeper case book = a more valuable trust-node; the #1 attorney gets a small
    lift. Capped."""
    base = 0.55 + 0.05 * min(case_count, 6)
    if rank == 1:
        base += 0.05
    return round(min(base, 0.90), 2)


# ---------------------------------------------------------------------------
# CENSUS — reports the t5 1.4 gate explicitly
# ---------------------------------------------------------------------------
def census(records: List[Dict[str, Any]], min_cases: int = DEFAULT_MIN_CASES,
           min_distinct: int = DEFAULT_MIN_DISTINCT) -> Dict[str, Any]:
    """COVERED requires a rankable distribution: >= min_distinct attorneys each with
    >= min_cases. Reports the gate status when below that."""
    ranked = rank_attorneys(records, min_cases)
    rows_with_attorney = sum(1 for r in records if _norm_attorney(r.get("attorney")))
    covered = len(ranked) >= min_distinct
    entry = {"records": len(records), "rows_with_attorney": rows_with_attorney,
             "distinct_attorneys": len(ranked), "top_case_count": ranked[0]["case_count"] if ranked else 0,
             "covered": covered}
    if not covered:
        entry["gate"] = "t5 1.4 (legal-notice attorney extraction) NOT delivered at volume"
        entry["gap"] = (f"only {len(ranked)} attorney(s) extracted — need >= {min_distinct} to rank")
        entry["first_task"] = ("upstream t5 1.4: extract attorney-of-record from probate/legal-notice "
                               "text across the discovery pool so >=2 attorneys carry cases")
    return entry


def load_attorney_records(cur, tenant_id: str) -> List[Dict[str, Any]]:
    """Load attorney-of-record cases from the discovery pool (tenant-agnostic)."""
    cur.execute(
        """select raw_payload->>'attorney'  as attorney,
                  source_type                as case_type,
                  coalesce(source_ref, parcel_apn) as case_ref,
                  parcel_apn                 as parcel_apn
           from landos.discovery_signals
           where coalesce(raw_payload->>'attorney','') <> ''
             and (tenant_id=%s or tenant_id is null)""",
        (tenant_id,))
    cols = ("attorney", "case_type", "case_ref", "parcel_apn")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def census_live(cur, tenant_id: str) -> Dict[str, Any]:
    return census(load_attorney_records(cur, tenant_id))


class AttorneyRankingAdapter(DiscoveryAdapter):
    """Derived DiscoveryAdapter: ranks attorneys-of-record by estate-case volume.
    Emits ENTITY (attorney-node) signals for the trust-node channel. Census-gated;
    DISARMED today (t5 1.4 gate unmet — 1 attorney extracted)."""

    source_type = "attorney_ranking"
    county_fips = "*"
    access_mode = "derived"
    access_path = "landos.discovery_signals raw_payload->>'attorney' (from t5 1.4)"
    recipe_hint = "R8"          # relationship / trust-node channel

    def __init__(self, tenant_id: str, records: Optional[List[Dict[str, Any]]] = None,
                 min_cases: int = DEFAULT_MIN_CASES, min_distinct: int = DEFAULT_MIN_DISTINCT):
        super().__init__(tenant_id)
        self.records = records or []
        self.min_cases = min_cases
        self.min_distinct = min_distinct
        self.last_census: Dict[str, Any] = {}
        self.non_matches = 0

    def fetch(self) -> List[DiscoverySignal]:
        self.last_census = census(self.records, self.min_cases, self.min_distinct)
        self.non_matches = sum(1 for r in self.records if not _norm_attorney(r.get("attorney")))
        if not self.last_census.get("covered"):
            return []                                   # gate unmet — never fabricate a ranking
        signals: List[DiscoverySignal] = []
        for row in rank_attorneys(self.records, self.min_cases):
            signals.append({
                "source_ref": f"{row['attorney_name']}:attorney_ranking",
                "source_url": None, "owner_name": row["attorney_name"], "signal_date": None,
                "confidence": _confidence(row["case_count"], row["rank"]),
                "parcel_apn": None,                     # entity signal, not parcel-keyed
                "raw_payload": {"type": "attorney_ranking", "attorney_name": row["attorney_name"],
                                "case_count": row["case_count"], "case_types": row["case_types"],
                                "parcels": row["parcels"], "rank": row["rank"]},
            })
        return signals

"""
exemption_drop.py — DiscoveryAdapter: the "sleeper" pre-probate death detector.

THE THESIS. A homestead / Principal-Residence (PRE) / senior / disabled-vet
exemption is a claim only the living, resident owner can hold. When the owner
DIES or MOVES OUT, the assessor strips the exemption from the roll at the next
cycle — months to years before probate opens, before an obituary is cross-cut,
before any listing. A parcel that LOST an occupancy exemption with NO sale
recorded is a motivated-seller candidate hiding in plain sight, in data we
already touch (the assessor roll). $0. This adapter emits that signal.

    signal: {type: exemption_drop, confidence, parcel_apn,
             prior_vintage, current_vintage, dropped:[...], sale_recorded:false}

CENSUS FIRST (the load-bearing discipline). This adapter is a DIFF over roll
VINTAGES — it needs (a) an exemption flag captured on the roll, and (b) two
vintages of the same parcel to diff. Neither may exist for a given county. The
adapter NEVER fabricates the signal: it censuses each county's captured roll and
DISARMS (emits nothing, reports the gap) where the exemption column is absent —
exactly the hunt_core "$0 census discipline": covered only by evidence.

    LIVE STATE 2026-07-08 (this build): the vintage-capture that exists
    (scripts/enrich_owner_chain.py — reused here) stows vested_owner + roll
    mailing, NOT exemptions; and 0 parcels have >=2 vintages. So EVERY county is
    disarmed today. The real first task is ingest-side: extend vintage-capture
    to stow `exemption_flags` from the roll (county_rest_attributes already
    fetched, currently discarded) at >=2 time points. THEN this adapter lights
    up with no code change. The diff logic below is proven by tests against
    synthetic vintages so the machinery is ready the moment the column lands.

THE EXPECTED ROLL-VINTAGE SHAPE (the ingest contract this adapter reads).
A roll record is one parcel with its vintages, newest last:
    {"county_fips": "26049", "parcel_apn": "...",
     "vintages": [
        {"captured_at": "2024-04-01", "exemption_flags": ["pre","senior"],
         "sale_recorded": false},
        {"captured_at": "2026-04-01", "exemption_flags": [],
         "sale_recorded": false}]}          # <- PRE+senior dropped, no sale => emit
A vintage WITHOUT `exemption_flags` (the only shape captured today) is
NOT-CAPTURED — it cannot evidence presence OR absence, so it never yields a
drop (silence, never a false positive).

MATCHING & LEAD CREATION belong to the pipeline (scripts/ingest_discovery.py),
per the DiscoveryAdapter contract — this adapter's only job is the signal.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from .base import DiscoveryAdapter, DiscoverySignal

# Occupancy / person-bound exemptions whose disappearance implies the resident
# owner died or moved out. Normalized aliases -> canonical token.
_EXEMPTION_ALIASES = {
    "pre": "pre", "principal_residence": "pre", "principal_residence_exemption": "pre",
    "homestead": "pre", "homestead_exemption": "pre", "he": "pre",   # MI PRE == homestead
    "senior": "senior", "senior_citizen": "senior", "old_age": "senior",
    "disabled_vet": "disabled_vet", "disabled_veteran": "disabled_vet", "dav": "disabled_vet",
    "veteran": "disabled_vet",
}
# only these count as a "sleeper" drop (person-bound). A non-occupancy exemption
# (e.g. agricultural, industrial-facilities) leaving is NOT a death/move-out signal.
_OCCUPANCY = {"pre", "senior", "disabled_vet"}


def normalize_exemptions(flags: Optional[List[str]]) -> set:
    """Fold raw roll exemption labels to canonical occupancy tokens; drop unknowns
    (they're recorded upstream but cannot move this signal)."""
    out = set()
    for f in flags or []:
        key = str(f).strip().lower().replace(" ", "_").replace("-", "_")
        canon = _EXEMPTION_ALIASES.get(key)
        if canon:
            out.add(canon)
    return out


def _has_exemption_capture(vintage: Dict[str, Any]) -> bool:
    """A vintage EVIDENCES exemptions only if it carries the key at all. Absent key
    = not-captured (silence), NOT 'no exemptions' (which would fake a drop)."""
    return isinstance(vintage, dict) and "exemption_flags" in vintage


def _confidence(dropped: set) -> float:
    """Score a drop by which person-bound exemptions vanished. PRE/homestead is the
    most reliable occupancy proxy (it REQUIRES the owner live there); a senior or
    disabled-vet drop pins the exemptee specifically. Occupancy + a person-bound
    exemption dropping together (elderly resident homeowner gone) is the strongest."""
    occ = "pre" in dropped
    person = bool(dropped & {"senior", "disabled_vet"})
    if occ and person:
        return 0.85                      # PRE + senior/vet gone together
    if "senior" in dropped:
        return 0.72                      # the senior exemptee specifically
    if occ:
        return 0.70                      # primary residence exemption gone
    if "disabled_vet" in dropped:
        return 0.68
    return 0.0


def diff_exemptions(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """PURE diff for one parcel's roll vintages. Walk consecutive vintages
    (oldest->newest); emit a drop event when a person-bound exemption present in
    the prior vintage is ABSENT in the current one AND no sale was recorded
    between them. Returns [] (never raises) when there's nothing to diff or the
    exemption column isn't captured — the census, not an exception, gates arming.

    Each event: {parcel_apn, county_fips, dropped:set, prior, current,
                 sale_recorded, confidence, signal_date, reason}."""
    vints = record.get("vintages") or []
    apn = record.get("parcel_apn")
    county = record.get("county_fips")
    events: List[Dict[str, Any]] = []
    for prev, cur in zip(vints, vints[1:]):
        if not (_has_exemption_capture(prev) and _has_exemption_capture(cur)):
            continue                                   # not-captured window -> skip
        prior_ex = normalize_exemptions(prev.get("exemption_flags"))
        curr_ex = normalize_exemptions(cur.get("exemption_flags"))
        dropped = (prior_ex - curr_ex) & _OCCUPANCY
        if not dropped:
            continue                                   # no occupancy exemption lost
        sale = bool(cur.get("sale_recorded"))
        if sale:
            continue                                   # a sale explains it — not a sleeper
        events.append({
            "parcel_apn": apn, "county_fips": county, "dropped": dropped,
            "prior": prev.get("captured_at"), "current": cur.get("captured_at"),
            "sale_recorded": False, "confidence": _confidence(dropped),
            "signal_date": cur.get("captured_at"),
            "reason": f"lost {'+'.join(sorted(dropped))}, no sale recorded",
        })
    return events


# ---------------------------------------------------------------------------
# CENSUS — per-county evidence gate ($0, never guessed)
# ---------------------------------------------------------------------------
def census(records: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per-county census of exemption-diff readiness. COVERED requires evidence:
    at least one parcel with >=2 vintages that CARRY `exemption_flags`. A county
    with rolls but no exemption capture is reported as a GAP (the real first
    task), never counted as covered."""
    by_county: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_county[r.get("county_fips")].append(r)

    out: Dict[str, Dict[str, Any]] = {}
    for county, recs in by_county.items():
        parcels = len(recs)
        with_col = sum(1 for r in recs
                       if any(_has_exemption_capture(v) for v in (r.get("vintages") or [])))
        diffable = sum(1 for r in recs
                       if sum(1 for v in (r.get("vintages") or []) if _has_exemption_capture(v)) >= 2)
        covered = diffable >= 1
        entry = {"county_fips": county, "parcels": parcels,
                 "vintages_with_exemption_col": with_col, "diffable_parcels": diffable,
                 "covered": covered}
        if not covered:
            entry["gap"] = (
                "exemption column NOT captured on the roll vintage"
                if with_col == 0 else
                "exemption column present but <2 vintages/parcel — need a second capture to diff")
            entry["first_task"] = (
                "extend vintage-capture (scripts/enrich_owner_chain.py) to stow "
                "`exemption_flags` from county_rest_attributes, at >=2 time points")
        out[county] = entry
    return out


def load_roll_records(cur, tenant_id: str) -> List[Dict[str, Any]]:
    """Load roll vintages from the spine into the adapter's record shape, REUSING
    the existing vintage-capture (owner_leads.raw_meta->'vintage', written by
    scripts/enrich_owner_chain.py). One record per parcel, vintages oldest-first.
    Today those vintages carry no `exemption_flags` key, so the census reports a
    gap — this is the honest live wiring, not a stub."""
    cur.execute(
        """select county_fips, parcel_apn, raw_meta->'vintage' as vintage
           from landos.owner_leads
           where tenant_id = %s and parcel_apn is not null and raw_meta ? 'vintage'""",
        (tenant_id,))
    by_parcel: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for row in cur.fetchall():
        by_parcel[(row["county_fips"], row["parcel_apn"])].append(row["vintage"] or {})
    return [{"county_fips": c, "parcel_apn": a,
             "vintages": sorted(vs, key=lambda x: (x or {}).get("captured_at") or "")}
            for (c, a), vs in by_parcel.items()]


def census_live(cur, tenant_id: str) -> Dict[str, Dict[str, Any]]:
    """Per-county exemption-diff census against the live spine."""
    return census(load_roll_records(cur, tenant_id))


class ExemptionDropAdapter(DiscoveryAdapter):
    """Derived DiscoveryAdapter: diffs roll vintages for lost occupancy exemptions.
    access_mode='derived' — reads roll data ALREADY ingested (no external fetch),
    injected as roll_records (the pipeline/runner loads them; tests inject synthetic
    ones). Census-gated: emits only for counties with the exemption column captured
    and >=2 diffable vintages; disarms + reports the gap everywhere else."""

    source_type = "exemption_drop"
    county_fips = "*"            # cross-county; the runner may pin a single county
    access_mode = "derived"     # derived from ingested roll vintages, not fetched
    access_path = "landos.owner_leads roll vintages (reuses vintage-capture)"
    recipe_hint = "R4"          # death/move-out -> heirs/estate outreach, like probate

    def __init__(self, tenant_id: str, roll_records: Optional[List[Dict[str, Any]]] = None,
                 county_fips: str = "*"):
        super().__init__(tenant_id)
        self.roll_records = roll_records or []
        self.county_fips = county_fips
        self.last_census: Dict[str, Dict[str, Any]] = {}
        self.non_matches = 0

    def fetch(self) -> List[DiscoverySignal]:
        """Census -> arm covered counties, disarm+log gaps elsewhere. Returns the
        exemption_drop signals (empty when every county is disarmed — today's state)."""
        recs = self.roll_records
        if self.county_fips != "*":
            recs = [r for r in recs if r.get("county_fips") == self.county_fips]
        self.last_census = census(recs)

        signals: List[DiscoverySignal] = []
        self.non_matches = 0
        for r in recs:
            county = r.get("county_fips")
            if not self.last_census.get(county, {}).get("covered"):
                continue                                # disarmed county — never fabricate
            events = diff_exemptions(r)
            if not events:
                self.non_matches += 1                   # diffable parcel, no drop — logged
                continue
            for ev in events:
                signals.append({
                    "source_ref": f"{ev['parcel_apn']}:{ev['current']}:exemption_drop",
                    "source_url": None,
                    "owner_name": None,                 # roll owner is vested/estate; pipeline resolves heirs
                    "signal_date": ev["signal_date"],
                    "confidence": ev["confidence"],
                    "parcel_apn": ev["parcel_apn"],
                    "raw_payload": {
                        "type": "exemption_drop",
                        "dropped": sorted(ev["dropped"]),
                        "prior_vintage": ev["prior"],
                        "current_vintage": ev["current"],
                        "sale_recorded": False,
                        "reason": ev["reason"],
                    },
                })
        return signals

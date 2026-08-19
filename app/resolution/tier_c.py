"""tier_c_promotion_rule: the versioned narrow rule, and why it stays unarmed.

T20D certified one subcondition -- A2_SITUS_ZIP_UNKNOWN_CONTAMINATED, n=90,
precision 1.0000 against two arbiters that are deliberately not the voter file
(NC OneMap parcel fabric, Census ZCTA gazetteer). 13 of those 90 carry a phone.

The founder's instruction is explicit: implement the versioned rule ONLY if the
production matcher can load the required independent arbiter evidence without
creating unacceptable coupling; otherwise leave it CERTIFIED_NOT_ARMED, because
truth beats count.

This module implements the rule as a real, testable, versioned object AND the
arming precondition. The rule can be evaluated by anything holding arbiter
evidence. It reports NOT_ARMED and promotes nothing when that evidence is absent
-- which is the actual state of the production matcher, so nothing auto-appends.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence

TIER_C_RULE_ID = "tier_c_promotion_rule.v1"

STATUS_CERTIFIED_NOT_ARMED = "CERTIFIED_NOT_ARMED"
STATUS_ARMED = "ARMED"
STATUS_UNAVAILABLE = "ARBITER_UNAVAILABLE_NOT_ARMED"

# The one subcondition that met the contract. Everything else stayed held.
CERTIFIED_SUBCONDITION = "A2_SITUS_ZIP_UNKNOWN_CONTAMINATED"
CERTIFIED_N = 90
CERTIFIED_PRECISION = 1.0
PRECISION_FLOOR = 0.98
MIN_SAMPLE = 40

# Arbiters must be independent of the source being adjudicated. A voter-file
# match is never validated by the voter file.
REQUIRED_ARBITERS = ("NC_ONEMAP_PARCEL_FABRIC", "CENSUS_ZCTA_GAZETTEER")
FORBIDDEN_ARBITER = "NCSBE_PUBLIC_VOTER_FILE"


@dataclass
class ArbiterEvidence:
    """What the matcher would have to hold AT MATCH TIME to arm the rule."""
    family: str
    source_hash: str
    loaded: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PromotionDecision:
    rule_id: str
    status: str
    promoted: List[str] = field(default_factory=list)
    refused: List[str] = field(default_factory=list)
    reason: str = ""
    arbiter_hashes: Dict[str, str] = field(default_factory=dict)

    @property
    def incremental_contacts(self) -> int:
        return len(self.promoted)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["incremental_contacts"] = self.incremental_contacts
        return d


def arming_precondition(arbiters: Sequence[ArbiterEvidence]) -> tuple[bool, str]:
    """Can the rule be armed at all?

    Arming requires that the DETERMINISTIC MATCHER can load both arbiters at
    match time. It cannot: the matcher reads the voter file and the estate, and
    nothing else. Adding two more source loads to the match path in order to
    gain 13 phones is the coupling the founder asked to be weighed -- and the
    weighing comes out against it.
    """
    by_family = {a.family: a for a in arbiters}
    missing = [f for f in REQUIRED_ARBITERS if not by_family.get(f, ArbiterEvidence(f, "")).loaded]
    if missing:
        return False, (
            f"arbiter evidence not loadable at match time: {', '.join(missing)}. "
            "The production matcher loads the voter file and the estate only. "
            "Arming would make every deterministic match depend on two further "
            "source loads for a ceiling of 13 phones, all Research-tier. "
            "Refused: the coupling costs more than the count is worth."
        )
    for a in arbiters:
        if a.family == FORBIDDEN_ARBITER:
            return False, "a voter-file match may not be arbitrated by the voter file"
        if not a.source_hash:
            return False, f"arbiter {a.family} carries no source hash; provenance would be unrecordable"
    return True, ""


def evaluate(
    held_rows: Sequence[dict],
    certified_ids: Sequence[str],
    arbiters: Optional[Sequence[ArbiterEvidence]] = None,
    *,
    precision: float = CERTIFIED_PRECISION,
    sample_n: int = CERTIFIED_N,
) -> PromotionDecision:
    """Apply the versioned rule. Promotes nothing unless every gate passes."""
    arbiters = list(arbiters or [])
    hashes = {a.family: a.source_hash for a in arbiters if a.loaded}
    certified = set(certified_ids)
    eligible = [r for r in held_rows if str(r.get("candidate_id")) in certified]
    others = [str(r.get("candidate_id")) for r in held_rows
              if str(r.get("candidate_id")) not in certified]

    if precision < PRECISION_FLOOR or sample_n < MIN_SAMPLE:
        return PromotionDecision(
            TIER_C_RULE_ID, STATUS_CERTIFIED_NOT_ARMED, [], [str(r.get("candidate_id")) for r in held_rows],
            f"precision {precision} / n {sample_n} does not meet the contract "
            f"(>= {PRECISION_FLOOR} on n >= {MIN_SAMPLE})", hashes)

    ok, why = arming_precondition(arbiters)
    if not ok:
        # Fail-closed: no arbiter, no promotion. Never a silent promotion.
        return PromotionDecision(
            TIER_C_RULE_ID, STATUS_UNAVAILABLE if hashes else STATUS_CERTIFIED_NOT_ARMED,
            [], [str(r.get("candidate_id")) for r in held_rows], why, hashes)

    # Name-only matches can never be promoted, whatever the subcondition says.
    promoted, refused = [], list(others)
    for r in eligible:
        cid = str(r.get("candidate_id"))
        if not str(r.get("address_corroborated") or "").upper().startswith("Y"):
            refused.append(cid)
            continue
        if str(r.get("subcondition") or "") != CERTIFIED_SUBCONDITION:
            refused.append(cid)
            continue
        promoted.append(cid)
    return PromotionDecision(
        TIER_C_RULE_ID, STATUS_ARMED, sorted(promoted), sorted(refused),
        f"armed against {', '.join(sorted(hashes))}", hashes)


def current_status() -> dict:
    """What the rule reports in production today: certified, not armed."""
    decision = evaluate([], [], arbiters=[
        ArbiterEvidence("NC_ONEMAP_PARCEL_FABRIC", "", loaded=False),
        ArbiterEvidence("CENSUS_ZCTA_GAZETTEER", "", loaded=False),
    ])
    return {
        "rule_id": TIER_C_RULE_ID,
        "status": STATUS_CERTIFIED_NOT_ARMED,
        "certified_subcondition": CERTIFIED_SUBCONDITION,
        "certified_n": CERTIFIED_N,
        "certified_precision": CERTIFIED_PRECISION,
        "certified_with_phone": 13,
        "incremental_contacts_if_armed": 13,
        "incremental_contacts_today": 0,
        "required_arbiters": list(REQUIRED_ARBITERS),
        "refusal_reason": decision.reason,
    }

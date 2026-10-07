"""Transparent contact confidence. Not a model — a scored checklist.

HIGH auto-enters the T12 append candidate set.
MEDIUM is written to a manual-review file and is not auto-appended.
REVIEW is not appended.
TIER_C never auto-appends.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True, slots=True)
class Confidence:
    category: str  # HIGH | MEDIUM | REVIEW
    score: int
    components: dict


def _age_days(observed_at: str, now: Optional[datetime] = None) -> Optional[int]:
    text = (observed_at or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    now = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, int((now - dt).total_seconds() // 86400))


def score_contact(
    *,
    tier: str,
    strong_name: bool,
    common_name: bool,
    source: str,
    observed_at: str,
    source_count: int,
    fanout_candidates: int,
    fanout_distinct_owners: int,
    numbering_plan_valid: bool,
    deceased_target: bool,
    confidential: bool = False,
    ambiguous: bool = False,
) -> Confidence:
    parts = {}
    if deceased_target or confidential or ambiguous or not numbering_plan_valid:
        reason = "deceased" if deceased_target else "confidential" if confidential else "ambiguous" if ambiguous else "invalid_phone"
        return Confidence("REVIEW", 0, {"block": reason})

    if tier == "TIER_C":
        return Confidence("REVIEW", 15, {"block": "tier_c_held", "tier": 10})

    parts["identity"] = 40 if tier == "TIER_A" else 32 if tier == "TIER_B" else 8
    parts["address"] = 25 if tier == "TIER_A" else 18 if tier == "TIER_B" else 6
    if not strong_name:
        parts["weak_name"] = -12
    if common_name and tier != "TIER_A":
        parts["common_name"] = -12

    reliability = {
        "NCSBE_PUBLIC_VOTER_FILE": 20,
        "IRS_FORM_990_PUBLIC": 20,
        "IRS_EO_ICO_NCSBE": 12,
        "PUBLIC_BUSINESS_WEB": 8,
        "LOCAL_OWNED_IDENTITY": 15,
    }
    parts["source_reliability"] = reliability.get(source, 5)

    age = _age_days(observed_at)
    parts["source_age_days"] = age if age is not None else "UNKNOWN"
    if age is None:
        parts["recency"] = 3
    elif age <= 365:
        parts["recency"] = 10
    elif age <= 1095:
        parts["recency"] = 5
    else:
        parts["recency"] = 0

    extra = max(0, source_count - 1)
    parts["independent_sources"] = min(20, extra * 10)

    if fanout_distinct_owners >= 3:
        parts["fanout"] = -30
    elif fanout_distinct_owners == 2:
        parts["fanout"] = -10
    elif fanout_candidates >= 5:
        parts["fanout"] = -15
    else:
        parts["fanout"] = 0

    score = sum(v for k, v in parts.items() if k != "source_age_days" and isinstance(v, int))
    if parts.get("fanout", 0) <= -30:
        category = "REVIEW"
    elif score >= 80:
        category = "HIGH"
    elif score >= 55:
        category = "MEDIUM"
    else:
        category = "REVIEW"
    return Confidence(category, score, parts)

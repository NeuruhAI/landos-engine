
"""
NC DPS P0 contract.

Pure constants and deterministic score helpers only:
- no database
- no network
- no CRM
- no current-time reads
- no execution authority
"""
from __future__ import annotations

from typing import Iterable, Mapping

CONTRACT_VERSION = "nc_dps_scoring.v1"

TRIGGER_CLASSES = (
    "tax_distress",
    "court_action",
    "death_or_probate",
    "title_chain_problem",
    "absentee_or_out_of_state",
    "vacancy_or_code",
    "entity_failure",
    "recorded_lien_or_judgment",
    "ownership_fragmentation",
    "market_or_redevelopment_signal",
)

OPPORTUNITY_LANE_CAPS = {
    "equity_quality": 35.0,
    "tax_and_court_distress": 25.0,
    "title_curative_opportunity": 25.0,
    "owner_detachment": 10.0,
    "market_liquidity": 5.0,
}

PRIORITY_WEIGHTS = {
    "opportunity_score": 0.70,
    "urgency_score": 0.20,
    "evidence_confidence": 0.10,
}

RAW_DPS_POINTS = {
    "tax_lawsuit_over_365_days": 25,
    "confirmed_deceased_owner": 20,
    "no_probate_found": 15,
    "unknown_heirs_named": 15,
    "failed_service": 10,
    "ad_litem_appointed": 10,
    "service_by_publication": 10,
    "taxes_delinquent_2_4_years": 15,
    "taxes_delinquent_5_plus_years": 10,
    "free_and_clear": 15,
    "equity_above_150k": 15,
    "equity_75k_to_150k": 8,
    "absentee_owner": 8,
    "out_of_state_owner": 5,
    "ownership_over_20_years": 8,
    "vacant_property": 10,
    "code_case_over_180_days": 8,
    "defunct_entity": 12,
    "probate_opened_without_conveyance": 12,
    "lis_pendens": 7,
    "partition_case": 12,
    "returned_mail_or_bad_address": 8,
    "tax_suit_inactive_over_six_months": 8,
    "owner_or_heir_occupied": -20,
    "equity_below_50k": -30,
    "mortgage_above_70_percent_of_value": -20,
    "more_than_5_signatures_equity_below_250k": -20,
    "more_than_10_signatures": -25,
    "contested_probate": -10,
    "active_bankruptcy": -15,
    "condemned_or_demolition_pending": -10,
    "weak_resale_market": -10,
    "taxes_and_liens_above_40_percent_of_value": -20,
}

MANUAL_ESCALATIONS = {
    "possible_fraud",
    "competing_deed",
    "material_name_conflict",
    "post_death_conveyance",
    "omitted_family_branch",
}

ROUTING_THRESHOLDS = (
    (85.0, "immediate_senior_review"),
    (70.0, "full_ai_dps_analysis"),
    (55.0, "automated_enrichment_then_analyst"),
    (35.0, "monitor_or_nurture"),
    (0.0, "archive_until_new_signal"),
)


def _bounded_score(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a numeric score")
    value = float(value)
    if not 0.0 <= value <= 100.0:
        raise ValueError(f"{field} must be between 0 and 100")
    return value


def raw_dps_score(active_signals: Iterable[str]) -> int:
    names = {str(name) for name in active_signals}
    unknown = sorted(names - set(RAW_DPS_POINTS))
    if unknown:
        raise ValueError(f"unknown raw DPS signal(s): {unknown}")
    return sum(RAW_DPS_POINTS[name] for name in names)


def normalized_dps_score(active_signals: Iterable[str]) -> float:
    return round(max(0.0, min(100.0, float(raw_dps_score(active_signals)))), 2)


def composite_priority(
    opportunity_score: float,
    urgency_score: float,
    evidence_confidence: float,
) -> float:
    values = {
        "opportunity_score": _bounded_score(
            opportunity_score,
            "opportunity_score",
        ),
        "urgency_score": _bounded_score(
            urgency_score,
            "urgency_score",
        ),
        "evidence_confidence": _bounded_score(
            evidence_confidence,
            "evidence_confidence",
        ),
    }

    score = sum(
        values[name] * PRIORITY_WEIGHTS[name]
        for name in PRIORITY_WEIGHTS
    )
    return round(score, 2)


def validate_opportunity_lanes(lanes: Mapping[str, float]) -> float:
    unknown = sorted(set(lanes) - set(OPPORTUNITY_LANE_CAPS))
    missing = sorted(set(OPPORTUNITY_LANE_CAPS) - set(lanes))

    if unknown or missing:
        raise ValueError(
            f"invalid opportunity lanes; unknown={unknown}, missing={missing}"
        )

    total = 0.0
    for name, cap in OPPORTUNITY_LANE_CAPS.items():
        value = lanes[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be numeric")
        value = float(value)
        if not 0.0 <= value <= cap:
            raise ValueError(f"{name} must be between 0 and {cap}")
        total += value

    return round(total, 2)


def route_priority(priority_score: float) -> str:
    score = _bounded_score(priority_score, "priority_score")

    for floor, route in ROUTING_THRESHOLDS:
        if score >= floor:
            return route

    raise AssertionError("routing thresholds are incomplete")

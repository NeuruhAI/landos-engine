"""Pure UnderwritingResultV1 producer.

The producer intentionally accepts only a validated fixture/document input. It has no
database connection, network client, model call, or write adapter. Valuation calculations
delegate to app.valuation; deterministic objections delegate to app.insight.compose.
"""

from __future__ import annotations

import statistics
from typing import Any

from pydantic import ValidationError

from app import valuation
from app.insight import compose

from .canonical import sha256_hex
from .models import (
    ConfidenceV1,
    MoneyRangeV1,
    NullableMoneyV1,
    OfferMaoLogicV1,
    ReceiptV1,
    StrategyScenarioV1,
    UnderwritingCaseV1,
    UnderwritingResultV1,
)


class UnderwritingProductionError(ValueError):
    """Fail-closed validation or receipt error."""


def _known_range(value: Any) -> MoneyRangeV1:
    if value is None:
        return MoneyRangeV1(unknown_reason="valuation engine returned no grounded range")
    return MoneyRangeV1(low=value[0], high=value[1])


def _repair_basis(case: UnderwritingCaseV1) -> float | None:
    if case.repair_range.low is None:
        return None
    if case.cash_finance_assumptions.repair_basis == "low":
        return case.repair_range.low
    return case.repair_range.high


def _valuation(case: UnderwritingCaseV1) -> tuple[dict[str, Any], float | None]:
    per_acre = sorted(
        comp.sale_price / comp.acreage for comp in case.comparable_sales
    )
    base_ppa = statistics.median(per_acre) if len(per_acre) >= 3 else None
    p25 = valuation._percentile(per_acre, 0.25) if base_ppa is not None else None
    p75 = valuation._percentile(per_acre, 0.75) if base_ppa is not None else None
    result = valuation.land_value(
        base_ppa,
        case.property_identity.acreage,
        buildable_fraction=case.buildable_fraction,
        buildable_input=(
            "UnderwritingCaseV1.buildable_fraction"
            if case.buildable_fraction is not None
            else None
        ),
        in_sfha=case.in_sfha,
        dist_to_water_m=case.distance_to_water_m,
        road_frontage_ft=case.road_frontage_ft,
        ppa_p25=p25,
        ppa_p75=p75,
    )
    return result, base_ppa


def _offer_logic(
    case: UnderwritingCaseV1, land_result: dict[str, Any]
) -> OfferMaoLogicV1:
    basis_name = "ARV" if case.arv.value is not None else "land_value"
    basis_value = (
        case.arv.value if case.arv.value is not None else land_result.get("value")
    )
    repairs = _repair_basis(case)
    cfg = {
        "pct": case.cash_finance_assumptions.offer_pct,
        "repairs": repairs or 0.0,
        "fee_pct": case.cash_finance_assumptions.fee_pct,
        "fee_flat": case.cash_finance_assumptions.fee_flat,
        "source": "UnderwritingCaseV1.cash_finance_assumptions",
    }
    mao = valuation.compute_mao(basis_value, cfg)
    basis = (
        NullableMoneyV1(value=basis_value)
        if basis_value is not None
        else NullableMoneyV1(
            unknown_reason="neither grounded ARV nor grounded land value is available"
        )
    )
    repair_value = (
        NullableMoneyV1(value=repairs)
        if repairs is not None
        else NullableMoneyV1(unknown_reason="repair range is unknown")
    )
    fee = (
        NullableMoneyV1(value=mao.get("fee"))
        if mao.get("fee") is not None
        else NullableMoneyV1(unknown_reason="fee cannot be calculated without a basis")
    )
    mao_range = (
        _known_range(mao.get("range"))
        if repairs is not None
        else MoneyRangeV1(
            unknown_reason="MAO withheld until repair range is evidenced"
        )
    )
    return OfferMaoLogicV1(
        formula="MAO = (basis × offer_pct) − repairs − fee",
        basis_name=basis_name,
        basis=basis,
        offer_pct=case.cash_finance_assumptions.offer_pct,
        repairs=repair_value,
        fee=fee,
        mao=mao_range,
        engine_method="app.valuation.compute_mao",
    )


def _insight(case: UnderwritingCaseV1) -> dict[str, Any]:
    facts: dict[str, dict[str, Any]] = {}

    def add(name: str, value: Any, field: str) -> None:
        if value is not None and value != "":
            facts[name] = {"value": value, "field": field}

    add("parcel_apn", case.property_identity.parcel_apn, "property_identity.parcel_apn")
    add("county_fips", case.property_identity.county_fips, "property_identity.county_fips")
    add("situs_address", case.property_identity.address, "property_identity.address")
    add("acreage", case.property_identity.acreage, "property_identity.acreage")
    for evidence in case.source_evidence:
        if evidence.evidence_type in {
            "owner_name",
            "amount_owed",
            "contamination_kind",
            "requires_bea",
            "dnc",
        }:
            add(evidence.evidence_type, evidence.value, evidence.provenance.field)
    return compose.compose({"facts": facts})


def _confidence(
    case: UnderwritingCaseV1, land_result: dict[str, Any]
) -> ConfidenceV1:
    engine_level = land_result.get("confidence", "none")
    cap = {"none": 0.0, "low": 0.35, "medium": 0.65, "high": 0.85}[engine_level]
    penalty = min(0.45, 0.05 * len(case.missing_evidence))
    score = round(max(0.0, cap - penalty), 2)
    if score == 0:
        level = "none"
    elif score < 0.5:
        level = "low"
    elif score < 0.8:
        level = "medium"
    else:
        level = "high"
    return ConfidenceV1(
        level=level,
        score=score,
        rationale=(
            f"app.valuation confidence={engine_level}; "
            f"{len(case.missing_evidence)} named evidence gap(s)"
        ),
    )


def _scenarios(
    case: UnderwritingCaseV1, valuation_range: MoneyRangeV1
) -> list[StrategyScenarioV1]:
    scenarios: list[StrategyScenarioV1] = []
    for strategy in case.strategy_names:
        if valuation_range.low is None:
            status = "insufficient_evidence"
            required = list(case.missing_evidence) or [
                "three grounded comparable land sales",
                "subject acreage",
            ]
        elif case.recommended_decision == "reject":
            status = "blocked"
            required = list(case.missing_evidence)
        else:
            status = "candidate"
            required = list(case.missing_evidence)
        scenarios.append(
            StrategyScenarioV1(
                strategy=strategy,
                status=status,
                projected_value=valuation_range,
                rationale=(
                    "Projected value delegates to app.valuation.land_value; "
                    "scenario execution remains subject to human approval."
                ),
                required_evidence=required,
            )
        )
    return scenarios


def _red_team_objections(
    case: UnderwritingCaseV1,
    land_result: dict[str, Any],
    insight: dict[str, Any],
) -> list[str]:
    objections = list(insight.get("do_not_say") or [])
    objections.extend(
        f"Missing evidence: {item}" for item in case.missing_evidence
    )
    objections.extend(
        adjustment["rationale"]
        for adjustment in land_result.get("adjustments", [])
        if not adjustment.get("verified", False)
    )
    if not objections:
        objections.append(
            "No automated objection cleared the deal; independent human underwriting "
            "and source-document review remain mandatory."
        )
    return list(dict.fromkeys(objections))


def _payload(case: UnderwritingCaseV1) -> dict[str, Any]:
    land_result, _ = _valuation(case)
    valuation_range = _known_range(land_result.get("range"))
    offer_logic = _offer_logic(case, land_result)
    insight = _insight(case)
    insight_summary = insight.get("brief") or (
        "No narrative insight was emitted from the grounded fixture facts."
    )
    return {
        "schema_version": "underwriting-result-v1",
        "run_id": case.run_id,
        "fixture_mode": case.fixture_mode,
        "production_write_performed": False,
        "lead_id": case.lead_id,
        "synthetic_case_id": case.synthetic_case_id,
        "property_identity": case.property_identity.model_dump(mode="json"),
        "source_evidence": [
            item.model_dump(mode="json") for item in case.source_evidence
        ],
        "condition_assumptions": [
            item.model_dump(mode="json") for item in case.condition_assumptions
        ],
        "market_comp_evidence": [
            item.model_dump(mode="json") for item in case.comparable_sales
        ],
        "valuation_range": valuation_range.model_dump(mode="json"),
        "arv": case.arv.model_dump(mode="json"),
        "rent_range": case.rent_range.model_dump(mode="json"),
        "repair_range": case.repair_range.model_dump(mode="json"),
        "strategy_scenarios": [
            item.model_dump(mode="json")
            for item in _scenarios(case, valuation_range)
        ],
        "cash_finance_assumptions": (
            case.cash_finance_assumptions.model_dump(mode="json")
        ),
        "offer_mao_logic": offer_logic.model_dump(mode="json"),
        "risk_register": [
            item.model_dump(mode="json") for item in case.risk_register
        ],
        "zoning_land_constraints": [
            item.model_dump(mode="json")
            for item in case.zoning_land_constraints
        ],
        "confidence": _confidence(case, land_result).model_dump(mode="json"),
        "missing_evidence": case.missing_evidence,
        "red_team_objections": _red_team_objections(
            case, land_result, insight
        ),
        "recommended_decision": case.recommended_decision,
        "decision_rationale": case.decision_rationale,
        "human_approval_required": True,
        "insight_summary": insight_summary,
        "source_sha": case.source_sha,
        "source_worktree_dirty": case.source_worktree_dirty,
        "generated_at": case.generated_at.isoformat(),
    }


def produce_underwriting_result(
    raw_case: UnderwritingCaseV1 | dict[str, Any],
) -> UnderwritingResultV1:
    """Validate, produce, validate again, and seal an underwriting artifact."""

    try:
        case = (
            raw_case
            if isinstance(raw_case, UnderwritingCaseV1)
            else UnderwritingCaseV1.model_validate(raw_case)
        )
        payload = _payload(case)
        draft = UnderwritingResultV1.model_validate(
            {
                **payload,
                "receipt": {
                    "algorithm": "sha256",
                    "scope": "underwriting_result_without_receipt",
                    "payload_sha256": "0" * 64,
                },
            }
        )
        normalized_payload = draft.model_dump(mode="json")
        normalized_payload.pop("receipt")
        receipt = ReceiptV1(
            algorithm="sha256",
            scope="underwriting_result_without_receipt",
            payload_sha256=sha256_hex(normalized_payload),
        )
        result = UnderwritingResultV1.model_validate(
            {**normalized_payload, "receipt": receipt.model_dump(mode="json")}
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise UnderwritingProductionError(str(exc)) from exc
    if not verify_receipt(result):
        raise UnderwritingProductionError("internal receipt verification failed")
    return result


def verify_receipt(result: UnderwritingResultV1 | dict[str, Any]) -> bool:
    try:
        model = (
            result
            if isinstance(result, UnderwritingResultV1)
            else UnderwritingResultV1.model_validate(result)
        )
    except ValidationError:
        return False
    payload = model.model_dump(mode="json")
    receipt = payload.pop("receipt")
    return receipt["payload_sha256"] == sha256_hex(payload)

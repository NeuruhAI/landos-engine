"""Pure, deterministic producer for the normative BLACK UnderwritingResultV1."""

from __future__ import annotations

import statistics
from typing import Any

from pydantic import ValidationError

from app import valuation
from app.insight import compose

from .canonical import content_sha256, verify_content_hash
from .models import (
    CompsV1,
    ScenarioV1,
    UnderwritingCaseV1,
    UnderwritingResultV1,
    UnderwritingRunContext,
    V2ProjectionV1,
)


class UnderwritingProductionError(ValueError):
    """Fail-closed input, production, or seal error."""


def _validated_case(raw_case: UnderwritingCaseV1 | dict[str, Any]) -> UnderwritingCaseV1:
    return (
        raw_case
        if isinstance(raw_case, UnderwritingCaseV1)
        else UnderwritingCaseV1.model_validate(raw_case)
    )


def _land_result(case: UnderwritingCaseV1) -> dict[str, Any]:
    per_acre = sorted(
        float(comp.sale_price) / float(comp.acreage)
        for comp in case.comparable_sales
    )
    grounded = len(per_acre) >= 3
    base_ppa = statistics.median(per_acre) if grounded else None
    p25 = valuation._percentile(per_acre, 0.25) if grounded else None
    p75 = valuation._percentile(per_acre, 0.75) if grounded else None
    acreage = next(
        (
            float(item.value)
            for item in case.evidence_items
            if item.key == "gross_acres"
            and isinstance(item.value, (int, float))
            and not isinstance(item.value, bool)
        ),
        None,
    )
    return valuation.land_value(
        base_ppa,
        acreage,
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


def _evidence_ids(case: UnderwritingCaseV1) -> list[str]:
    return [str(item.evidence_id) for item in case.evidence_items]


def _comps(case: UnderwritingCaseV1, land: dict[str, Any]) -> CompsV1:
    rng = land.get("range")
    items = [
        {
            "comp_id": comp.comp_id,
            "sale_price": float(comp.sale_price),
            "acreage": float(comp.acreage),
            "price_per_acre": round(
                float(comp.sale_price) / float(comp.acreage), 2
            ),
            "sale_date": (
                comp.sale_date.isoformat().replace("+00:00", "Z")
                if comp.sale_date
                else None
            ),
            "source": comp.source,
            "evidence_id": str(comp.evidence_id),
            "method": "comp_median",
        }
        for comp in case.comparable_sales
    ]
    return CompsV1(
        fmv_low=rng[0] if rng else None,
        fmv_estimate=land.get("value"),
        fmv_high=rng[1] if rng else None,
        comps_n=len(items),
        confidence=land.get("confidence"),
        items=items,
    )


def _insight(case: UnderwritingCaseV1) -> dict[str, Any]:
    facts: dict[str, dict[str, Any]] = {}
    for item in case.evidence_items:
        key = item.key
        if key in {
            "gross_acres",
            "owner_name",
            "amount_owed",
            "contamination_kind",
            "requires_bea",
            "dnc",
        } and item.value not in (None, "", [], {}):
            insight_key = "acreage" if key == "gross_acres" else key
            facts[insight_key] = {
                "value": item.value,
                "field": f"EvidenceItemV1:{item.evidence_id}",
            }
    return compose.compose({"facts": facts})


def _assumptions(case: UnderwritingCaseV1, evidence_ids: list[str]) -> list[str]:
    evidence = ",".join(evidence_ids)
    fixed = [
        (
            "acreage curve is heuristic; method=app.valuation.land_value;"
            f" REF={valuation.CURVE_REF_AC},K={valuation.CURVE_K},"
            f"FLOOR={valuation.CURVE_FLOOR}; evidence_ids={evidence}"
        ),
        (
            "unbuildable residual is 25% of buildable $/acre;"
            f" method=app.valuation.land_value; evidence_ids={evidence}"
        ),
        (
            "unverified overlays apply factor=1.0 and remain verified=false;"
            f" method=app.valuation.land_value; evidence_ids={evidence}"
        ),
        (
            f"MAO pct={float(case.mao_pct)} repairs={float(case.mao_repairs)} "
            f"fee_pct={float(case.mao_fee_pct)} fee_flat={float(case.mao_fee_flat)};"
            " method=app.valuation.compute_mao; config_source=file-only fixture;"
            f" evidence_ids={evidence}"
        ),
        (
            f"case_class={case.case_class}; fixture_mode=true; "
            "production calibration eligibility=false"
        ),
    ]
    return fixed + [item.render() for item in case.assumptions]


def _mao(case: UnderwritingCaseV1, point_value: float | None) -> dict[str, Any]:
    return valuation.compute_mao(
        point_value,
        {
            "pct": float(case.mao_pct),
            "repairs": float(case.mao_repairs),
            "fee_pct": float(case.mao_fee_pct),
            "fee_flat": float(case.mao_fee_flat),
            "source": "file-only UnderwritingCaseV1; not tenant_settings",
        },
    )


def _scenarios(
    case: UnderwritingCaseV1,
    land: dict[str, Any],
    mao: dict[str, Any],
    evidence_ids: list[str],
) -> list[ScenarioV1]:
    rng = land.get("range")
    note = (
        "method=app.valuation.land_value+compute_mao; "
        f"evidence_ids={','.join(evidence_ids)}; human offer approval required"
    )
    if not rng:
        return [
            ScenarioV1(
                name=name,
                notes=[note, "valuation withheld because grounded comps are insufficient"],
            )
            for name in ("base", "upside", "downside")
        ]
    pct = float(case.mao_pct)
    return [
        ScenarioV1(
            name="base",
            offer=mao.get("value"),
            exit=land.get("value"),
            notes=[note],
        ),
        ScenarioV1(
            name="upside",
            offer=round(float(rng[1]) * pct, 2),
            exit=rng[1],
            notes=[note],
        ),
        ScenarioV1(
            name="downside",
            offer=round(float(rng[0]) * pct, 2),
            exit=rng[0],
            notes=[note],
        ),
    ]


def _payload(case: UnderwritingCaseV1) -> dict[str, Any]:
    land = _land_result(case)
    evidence_ids = _evidence_ids(case)
    comps = _comps(case, land)
    mao = _mao(case, land.get("value"))
    insight = _insight(case)

    adjustments = []
    for adjustment in land.get("adjustments", []):
        adjustments.append(
            {
                **adjustment,
                "method": "app.valuation.land_value",
                "evidence_ids": evidence_ids,
            }
        )

    risk = dict(case.risk)
    risk.update(
        {
            "case_class": case.case_class,
            "fixture_mode": True,
            "calibration_eligible": False,
            "production_write_performed": False,
            "insight_summary": insight.get("brief") or None,
            "insight_guardrails": insight.get("do_not_say") or [],
        }
    )

    falsifiers = list(case.counter_analysis.falsifiers)
    falsifiers.extend(
        adjustment["rationale"]
        for adjustment in adjustments
        if not adjustment.get("verified", False)
    )
    falsifiers.extend(insight.get("do_not_say") or [])
    counter_analysis = case.counter_analysis.model_copy(
        update={"falsifiers": list(dict.fromkeys(falsifiers))}
    )

    valuation_payload = {
        "mode": "land",
        "point_value": land.get("value"),
        "range": land.get("range"),
        "base_ppa": land.get("base_ppa"),
        "net_factor": land.get("net_factor"),
        "comp_dispersion": land.get("comp_dispersion"),
        "adjustments": adjustments,
        "mao": mao,
        "reason_if_null": land.get("reason"),
    }
    v2 = V2ProjectionV1(
        schema="underwriting_result.v2",
        generated_at=case.generated_at,
        zoning=case.zoning,
        constraints=case.constraints,
        comps=comps,
    )
    return {
        "schema": "neuruh.underwriting_result.v1",
        "underwriting_id": str(case.underwriting_id),
        "generated_at": case.generated_at,
        "method_version": case.method_version,
        "entity": case.entity.model_dump(mode="json", exclude_none=True),
        "evidence_ids": evidence_ids,
        "zoning": case.zoning.model_dump(mode="json"),
        "constraints": case.constraints.model_dump(mode="json"),
        "comps": comps.model_dump(mode="json"),
        "valuation": valuation_payload,
        "assumptions": _assumptions(case, evidence_ids),
        "ranges": {
            "land_value": land.get("range"),
            "method": "app.valuation.land_value",
            "evidence_ids": evidence_ids,
        },
        "scenarios": [
            item.model_dump(mode="json")
            for item in _scenarios(case, land, mao, evidence_ids)
        ],
        "risk": risk,
        "confidence": land.get("confidence", "none"),
        "counter_analysis": counter_analysis.model_dump(mode="json"),
        "human_approval": case.human_approval.model_dump(mode="json"),
        "v2_projection": (
            v2.model_dump(mode="json", by_alias=True)
            if case.include_v2_projection
            else None
        ),
    }


def produce_underwriting_result(
    raw_case: UnderwritingCaseV1 | dict[str, Any],
) -> UnderwritingResultV1:
    """Validate input, run existing valuation/insight brains, validate, and seal."""

    try:
        case = _validated_case(raw_case)
        payload = _payload(case)
        draft = UnderwritingResultV1.model_validate(
            {**payload, "content_sha256": "0" * 64}
        )
        normalized = draft.model_dump(mode="json", by_alias=True)
        normalized["content_sha256"] = content_sha256(normalized)
        result = UnderwritingResultV1.model_validate(normalized)
    except (ValidationError, ValueError, TypeError) as exc:
        raise UnderwritingProductionError(str(exc)) from exc
    if not verify_receipt(result):
        raise UnderwritingProductionError("internal content_sha256 verification failed")
    return result


def verify_receipt(result: UnderwritingResultV1 | dict[str, Any]) -> bool:
    """Compatibility name: the BLACK receipt is ``content_sha256``."""

    try:
        payload = (
            result.model_dump(mode="json", by_alias=True)
            if isinstance(result, UnderwritingResultV1)
            else UnderwritingResultV1.model_validate(result).model_dump(
                mode="json", by_alias=True
            )
        )
    except ValidationError:
        return False
    return verify_content_hash(payload)


def underwriting_run_context(
    raw_case: UnderwritingCaseV1 | dict[str, Any],
    result: UnderwritingResultV1 | None = None,
) -> UnderwritingRunContext:
    """Expose the run/case link without adding non-normative fields to court V1."""

    try:
        case = _validated_case(raw_case)
        artifact = result or produce_underwriting_result(case)
        if artifact.underwriting_id != case.underwriting_id:
            raise ValueError("result underwriting_id does not match input")
        if not verify_receipt(artifact):
            raise ValueError("result content_sha256 is invalid")
        return UnderwritingRunContext(
            run_id=case.run_id,
            case_id=case.case_id,
            case_class=case.case_class,
            fixture_mode=True,
            calibration_eligible=False,
            source_sha=case.source_sha,
            underwriting_id=artifact.underwriting_id,
            underwriting_sha256=artifact.content_sha256,
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise UnderwritingProductionError(str(exc)) from exc

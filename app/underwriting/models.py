"""Strict input and output contracts for the UnderwritingResultV1 producer."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    StringConstraints,
    model_validator,
)
from typing_extensions import Annotated

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitSha = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40}$")]
Currency = Literal["USD"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProvenanceV1(StrictModel):
    system: NonEmpty
    field: NonEmpty
    record_id: Optional[NonEmpty] = None
    observed_at: Optional[AwareDatetime] = None
    method: Optional[NonEmpty] = None


class SourceEvidenceV1(StrictModel):
    evidence_id: NonEmpty
    evidence_type: NonEmpty
    value: Any
    provenance: ProvenanceV1
    verified: bool
    limitation: Optional[NonEmpty] = None

    @model_validator(mode="after")
    def unverified_has_limitation(self) -> "SourceEvidenceV1":
        if not self.verified and not self.limitation:
            raise ValueError("unverified evidence requires a limitation")
        return self


class PropertyIdentityV1(StrictModel):
    parcel_apn: Optional[NonEmpty] = None
    address: Optional[NonEmpty] = None
    county_fips: Optional[Annotated[str, StringConstraints(pattern=r"^\d{5}$")]] = None
    latitude: Optional[FiniteFloat] = None
    longitude: Optional[FiniteFloat] = None
    acreage: Optional[Annotated[FiniteFloat, Field(gt=0)]] = None
    property_type: Optional[NonEmpty] = None
    identity_unknown_reason: Optional[NonEmpty] = None

    @model_validator(mode="after")
    def identity_or_unknown(self) -> "PropertyIdentityV1":
        if not (self.parcel_apn or self.address) and not self.identity_unknown_reason:
            raise ValueError(
                "property identity requires parcel_apn/address or identity_unknown_reason"
            )
        return self


class AssumptionV1(StrictModel):
    name: NonEmpty
    value: Any
    basis: NonEmpty
    confirmed: bool = False


class ComparableSaleV1(StrictModel):
    comp_id: NonEmpty
    sale_price: Annotated[FiniteFloat, Field(gt=0)]
    acreage: Annotated[FiniteFloat, Field(gt=0)]
    sale_date: Optional[datetime] = None
    source_evidence_id: NonEmpty
    property_type: Optional[NonEmpty] = None


class NullableMoneyV1(StrictModel):
    value: Optional[FiniteFloat] = None
    currency: Currency = "USD"
    unknown_reason: Optional[NonEmpty] = None

    @model_validator(mode="after")
    def value_xor_unknown(self) -> "NullableMoneyV1":
        if (self.value is None) == (self.unknown_reason is None):
            raise ValueError("set exactly one of value or unknown_reason")
        return self


class MoneyRangeV1(StrictModel):
    low: Optional[FiniteFloat] = None
    high: Optional[FiniteFloat] = None
    currency: Currency = "USD"
    unknown_reason: Optional[NonEmpty] = None

    @model_validator(mode="after")
    def range_or_unknown(self) -> "MoneyRangeV1":
        has_range = self.low is not None or self.high is not None
        if has_range:
            if self.low is None or self.high is None:
                raise ValueError("known range requires both low and high")
            if self.low > self.high:
                raise ValueError("range low cannot exceed high")
            if self.unknown_reason is not None:
                raise ValueError("known range cannot include unknown_reason")
        elif self.unknown_reason is None:
            raise ValueError("unknown range requires unknown_reason")
        return self


class CashFinanceAssumptionsV1(StrictModel):
    offer_pct: Annotated[FiniteFloat, Field(gt=0, le=1)]
    fee_pct: Annotated[FiniteFloat, Field(ge=0, le=1)] = 0.0
    fee_flat: Annotated[FiniteFloat, Field(ge=0)] = 0.0
    repair_basis: Literal["low", "high"] = "high"
    financing: Literal["cash", "seller_finance", "conventional", "unknown"]
    down_payment_pct: Optional[Annotated[FiniteFloat, Field(ge=0, le=1)]] = None
    interest_rate_pct: Optional[Annotated[FiniteFloat, Field(ge=0, le=1)]] = None
    term_months: Optional[Annotated[int, Field(gt=0)]] = None
    unknowns: list[NonEmpty] = Field(default_factory=list)


class ConstraintV1(StrictModel):
    constraint_type: NonEmpty
    status: Literal["confirmed", "not_found", "unknown"]
    detail: NonEmpty
    source_evidence_id: Optional[NonEmpty] = None

    @model_validator(mode="after")
    def confirmed_has_source(self) -> "ConstraintV1":
        if self.status == "confirmed" and not self.source_evidence_id:
            raise ValueError("confirmed constraint requires source_evidence_id")
        return self


class RiskV1(StrictModel):
    risk_id: NonEmpty
    category: NonEmpty
    severity: Literal["low", "medium", "high", "critical"]
    statement: NonEmpty
    mitigation: NonEmpty
    evidence_ids: list[NonEmpty] = Field(default_factory=list)


class StrategyScenarioV1(StrictModel):
    strategy: NonEmpty
    status: Literal["candidate", "blocked", "insufficient_evidence"]
    projected_value: MoneyRangeV1
    rationale: NonEmpty
    required_evidence: list[NonEmpty] = Field(default_factory=list)


class OfferMaoLogicV1(StrictModel):
    formula: Literal["MAO = (basis × offer_pct) − repairs − fee"]
    basis_name: Literal["ARV", "land_value"]
    basis: NullableMoneyV1
    offer_pct: Annotated[FiniteFloat, Field(gt=0, le=1)]
    repairs: NullableMoneyV1
    fee: NullableMoneyV1
    mao: MoneyRangeV1
    engine_method: Literal["app.valuation.compute_mao"]


class ConfidenceV1(StrictModel):
    level: Literal["none", "low", "medium", "high"]
    score: Annotated[FiniteFloat, Field(ge=0, le=1)]
    rationale: NonEmpty


class ReceiptV1(StrictModel):
    algorithm: Literal["sha256"]
    scope: Literal["underwriting_result_without_receipt"]
    payload_sha256: Sha256


class UnderwritingCaseV1(StrictModel):
    """File-safe input contract. It has no database or external-write settings."""

    schema_version: Literal["underwriting-case-v1"]
    run_id: NonEmpty
    fixture_mode: Literal[True]
    lead_id: Optional[NonEmpty] = None
    synthetic_case_id: Optional[NonEmpty] = None
    property_identity: PropertyIdentityV1
    source_evidence: list[SourceEvidenceV1] = Field(min_length=1)
    condition_assumptions: list[AssumptionV1] = Field(default_factory=list)
    comparable_sales: list[ComparableSaleV1] = Field(default_factory=list)
    buildable_fraction: Optional[Annotated[FiniteFloat, Field(ge=0, le=1)]] = None
    in_sfha: Optional[bool] = None
    distance_to_water_m: Optional[Annotated[FiniteFloat, Field(ge=0)]] = None
    road_frontage_ft: Optional[Annotated[FiniteFloat, Field(ge=0)]] = None
    arv: NullableMoneyV1
    rent_range: MoneyRangeV1
    repair_range: MoneyRangeV1
    cash_finance_assumptions: CashFinanceAssumptionsV1
    zoning_land_constraints: list[ConstraintV1] = Field(default_factory=list)
    risk_register: list[RiskV1] = Field(default_factory=list)
    strategy_names: list[NonEmpty] = Field(min_length=1)
    missing_evidence: list[NonEmpty] = Field(default_factory=list)
    recommended_decision: Literal[
        "proceed_to_human_review", "hold_for_evidence", "reject"
    ]
    decision_rationale: NonEmpty
    source_sha: GitSha
    source_worktree_dirty: bool
    generated_at: AwareDatetime

    @model_validator(mode="after")
    def exactly_one_case_identifier_and_valid_refs(self) -> "UnderwritingCaseV1":
        if (self.lead_id is None) == (self.synthetic_case_id is None):
            raise ValueError("set exactly one of lead_id or synthetic_case_id")
        evidence_ids = [item.evidence_id for item in self.source_evidence]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("source evidence IDs must be unique")
        known = set(evidence_ids)
        for comp in self.comparable_sales:
            if comp.source_evidence_id not in known:
                raise ValueError(
                    f"comparable {comp.comp_id} references unknown evidence "
                    f"{comp.source_evidence_id}"
                )
        for constraint in self.zoning_land_constraints:
            if constraint.source_evidence_id not in known:
                if constraint.source_evidence_id is not None:
                    raise ValueError(
                        f"constraint references unknown evidence "
                        f"{constraint.source_evidence_id}"
                    )
        for risk in self.risk_register:
            missing = set(risk.evidence_ids) - known
            if missing:
                raise ValueError(
                    f"risk {risk.risk_id} references unknown evidence {sorted(missing)}"
                )
        if self.repair_range.low is None and not any(
            "repair" in item.lower() for item in self.missing_evidence
        ):
            raise ValueError("unknown repair range must be named in missing_evidence")
        if self.recommended_decision == "proceed_to_human_review":
            if not self.comparable_sales:
                raise ValueError("proceed decision requires comparable sales")
        return self


class UnderwritingResultV1(StrictModel):
    schema_version: Literal["underwriting-result-v1"]
    run_id: NonEmpty
    fixture_mode: bool
    production_write_performed: Literal[False]
    lead_id: Optional[NonEmpty] = None
    synthetic_case_id: Optional[NonEmpty] = None
    property_identity: PropertyIdentityV1
    source_evidence: list[SourceEvidenceV1] = Field(min_length=1)
    condition_assumptions: list[AssumptionV1]
    market_comp_evidence: list[ComparableSaleV1]
    valuation_range: MoneyRangeV1
    arv: NullableMoneyV1
    rent_range: MoneyRangeV1
    repair_range: MoneyRangeV1
    strategy_scenarios: list[StrategyScenarioV1] = Field(min_length=1)
    cash_finance_assumptions: CashFinanceAssumptionsV1
    offer_mao_logic: OfferMaoLogicV1
    risk_register: list[RiskV1]
    zoning_land_constraints: list[ConstraintV1]
    confidence: ConfidenceV1
    missing_evidence: list[NonEmpty]
    red_team_objections: list[NonEmpty] = Field(min_length=1)
    recommended_decision: Literal[
        "proceed_to_human_review", "hold_for_evidence", "reject"
    ]
    decision_rationale: NonEmpty
    human_approval_required: Literal[True]
    insight_summary: NonEmpty
    source_sha: GitSha
    source_worktree_dirty: bool
    generated_at: AwareDatetime
    receipt: ReceiptV1

    @model_validator(mode="after")
    def exactly_one_case_identifier(self) -> "UnderwritingResultV1":
        if (self.lead_id is None) == (self.synthetic_case_id is None):
            raise ValueError("set exactly one of lead_id or synthetic_case_id")
        return self

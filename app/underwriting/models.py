"""BLACK-court underwriting contracts and the file-only producer input."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

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
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
GitSha = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{40}$")]
Number = FiniteFloat


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntityV1(StrictModel):
    tenant_id: UUID
    parcel_apn: str | None = None
    county_fips: str | None = None
    source_lead_id: UUID | None = None
    acquisition_lead_id: UUID | None = None
    situs_fingerprint_sha256: Sha256 | None = None


class EvidenceSourceV1(StrictModel):
    system: Literal["landos-engine", "crm", "ingest", "human", "aegis", "external"]
    name: NonEmpty
    ref: str | None = None
    ref_sha256: Sha256 | None = None


class LicensingV1(StrictModel):
    rights_note: NonEmpty
    redistributable: bool
    retention_class: Literal["operational", "ephemeral", "vault_only"]


class EvidenceItemV1(StrictModel):
    """The subset is the complete BLACK EvidenceItemV1 JSON contract."""

    schema_: Literal["neuruh.evidence_item.v1"] = Field(alias="schema")
    evidence_id: UUID
    source: EvidenceSourceV1
    acquired_at: AwareDatetime
    effective_at: AwareDatetime
    entity: EntityV1
    key: str | None = None
    value: Number | str | bool | None | dict[str, Any] | list[Any]
    unit: str | None
    method: Literal[
        "direct_read", "comp_median", "overlay_join", "human_assert", "model_infer"
    ]
    confidence: Literal["none", "low", "medium", "high"]
    conflict_status: Literal["none", "suspected", "confirmed", "resolved"]
    range: tuple[Number | None, Number | None] | None = None
    assumptions: list[str] = Field(default_factory=list)
    conflict_with: list[UUID] = Field(default_factory=list)
    supersedes: UUID | None = None
    verified: bool | None = None
    actor_role: Literal["system", "operator", "desk", "admin"] | None = None
    created_at: AwareDatetime | None = None
    content_sha256: Sha256
    licensing: LicensingV1

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, serialize_by_alias=True
    )


class ComparableSaleInputV1(StrictModel):
    comp_id: NonEmpty
    sale_price: Annotated[Number, Field(gt=0)]
    acreage: Annotated[Number, Field(gt=0)]
    sale_date: datetime | None = None
    evidence_id: UUID
    source: NonEmpty = "synthetic fixture"


class NumericAssumptionV1(StrictModel):
    name: NonEmpty
    value: Any
    method: Literal[
        "direct_read", "comp_median", "overlay_join", "human_assert", "model_infer"
    ]
    detail: NonEmpty
    evidence_ids: list[UUID] = Field(default_factory=list)

    def render(self) -> str:
        ids = ",".join(str(item) for item in self.evidence_ids) or "none"
        return (
            f"{self.name}={self.value!r}; method={self.method}; "
            f"evidence_ids={ids}; {self.detail}"
        )


class ZoningV1(StrictModel):
    code: str | None
    description: str | None
    confirmed: bool
    source: str | None


class ConstraintsV1(StrictModel):
    landlocked: bool | None
    road_frontage_ft: Annotated[Number, Field(ge=0)] | None
    road_type: str | None
    wetlands_pct: Annotated[Number, Field(ge=0, le=100)] | None
    fema_flood_pct: Annotated[Number, Field(ge=0, le=100)] | None
    fema_zone: str | None
    slope_avg_pct: Annotated[Number, Field(ge=0, le=100)] | None
    buildable_pct: Annotated[Number, Field(ge=0, le=100)] | None
    easements: list[str]
    notes: list[str]


class CompsV1(StrictModel):
    fmv_low: Annotated[Number, Field(ge=0)] | None
    fmv_estimate: Annotated[Number, Field(ge=0)] | None
    fmv_high: Annotated[Number, Field(ge=0)] | None
    comps_n: Annotated[int, Field(ge=0)] | None
    confidence: str | None
    items: list[dict[str, Any]]


class ValuationV1(StrictModel):
    mode: Literal["land", "house"]
    point_value: Number | None
    range: tuple[Number | None, Number | None] | None
    base_ppa: Number | None = None
    net_factor: Number | None = None
    comp_dispersion: Number | None = None
    adjustments: list[dict[str, Any]]
    mao: dict[str, Any] | None = None
    reason_if_null: str | None = None

    @model_validator(mode="after")
    def honest_null(self) -> "ValuationV1":
        if self.point_value is None and not self.reason_if_null:
            raise ValueError("null point_value requires reason_if_null")
        if self.point_value is not None and self.reason_if_null is not None:
            raise ValueError("valued result cannot carry reason_if_null")
        return self


class ScenarioV1(StrictModel):
    name: Literal["base", "upside", "downside"]
    offer: Number | None = None
    exit: Number | None = None
    assignment_fee: Number | None = None
    probability: Number | None = None
    notes: list[str] = Field(default_factory=list)


class CounterAnalysisV1(StrictModel):
    falsifiers: list[str]
    worst_case_loss: Number | None = None
    kill_criteria: list[str]


class HumanApprovalV1(StrictModel):
    required: Literal[True] = True
    status: Literal["pending", "approved", "rejected"] = "pending"
    actor_role: str | None = None
    actor_id_sha256: Sha256 | None = None
    approved_at: AwareDatetime | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def approval_is_complete(self) -> "HumanApprovalV1":
        if self.status == "approved":
            if not (
                self.actor_role
                and self.actor_id_sha256
                and self.approved_at
                and self.notes
            ):
                raise ValueError("approved human gate requires actor, time, and notes")
        elif self.approved_at is not None:
            raise ValueError("non-approved human gate cannot carry approved_at")
        return self


class V2ProjectionV1(StrictModel):
    """Exact preparation-only shape accepted by the current CRM v2 column."""

    schema_: Literal["underwriting_result.v2"] = Field(alias="schema")
    generated_at: AwareDatetime
    zoning: ZoningV1
    constraints: ConstraintsV1
    comps: CompsV1

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, serialize_by_alias=True
    )


class UnderwritingCaseV1(StrictModel):
    """Deterministic, file-only producer input; ``run_id`` stays out of court V1."""

    schema_: Literal["neuruh.underwriting_case_input.v1"] = Field(alias="schema")
    run_id: NonEmpty
    case_id: NonEmpty
    case_class: Literal["synthetic", "test"]
    fixture_mode: Literal[True]
    calibration_eligible: Literal[False]
    source_sha: GitSha
    underwriting_id: UUID
    generated_at: AwareDatetime
    method_version: NonEmpty
    entity: EntityV1
    evidence_items: list[EvidenceItemV1] = Field(min_length=1)
    comparable_sales: list[ComparableSaleInputV1] = Field(default_factory=list)
    buildable_fraction: Annotated[Number, Field(ge=0, le=1)] | None = None
    in_sfha: bool | None = None
    distance_to_water_m: Annotated[Number, Field(ge=0)] | None = None
    road_frontage_ft: Annotated[Number, Field(ge=0)] | None = None
    zoning: ZoningV1
    constraints: ConstraintsV1
    assumptions: list[NumericAssumptionV1] = Field(default_factory=list)
    risk: dict[str, Any]
    counter_analysis: CounterAnalysisV1
    human_approval: HumanApprovalV1 = Field(default_factory=HumanApprovalV1)
    mao_pct: Annotated[Number, Field(gt=0, le=1)] = 0.70
    mao_repairs: Annotated[Number, Field(ge=0)] = 0.0
    mao_fee_pct: Annotated[Number, Field(ge=0, le=1)] = 0.0
    mao_fee_flat: Annotated[Number, Field(ge=0)] = 0.0
    include_v2_projection: bool = True

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, serialize_by_alias=True
    )

    @model_validator(mode="after")
    def validate_evidence_and_gates(self) -> "UnderwritingCaseV1":
        from .canonical import verify_content_hash

        ids = [item.evidence_id for item in self.evidence_items]
        if len(ids) != len(set(ids)):
            raise ValueError("evidence_id values must be unique")
        for item in self.evidence_items:
            if item.entity != self.entity:
                raise ValueError(f"evidence {item.evidence_id} entity mismatch")
            if not verify_content_hash(item.model_dump(mode="json", by_alias=True)):
                raise ValueError(f"evidence {item.evidence_id} content_sha256 mismatch")
        known = set(ids)
        for comp in self.comparable_sales:
            if comp.evidence_id not in known:
                raise ValueError(f"comp {comp.comp_id} references unknown evidence_id")
        for assumption in self.assumptions:
            if set(assumption.evidence_ids) - known:
                raise ValueError(f"assumption {assumption.name} has unknown evidence_id")
        if self.human_approval.status == "approved":
            if self.case_class != "synthetic":
                raise ValueError("only an explicitly synthetic case may carry fixture approval")
            if "synthetic" not in (self.human_approval.notes or "").lower():
                raise ValueError("synthetic approval notes must identify synthetic scope")
        return self


class UnderwritingResultV1(StrictModel):
    """Normative BLACK ``schemas/underwriting-result.v1.json`` envelope."""

    schema_: Literal["neuruh.underwriting_result.v1"] = Field(alias="schema")
    underwriting_id: UUID
    generated_at: AwareDatetime
    method_version: NonEmpty
    entity: dict[str, Any]
    evidence_ids: list[UUID]
    zoning: ZoningV1
    constraints: ConstraintsV1
    comps: CompsV1
    valuation: ValuationV1
    assumptions: list[str]
    ranges: dict[str, Any]
    scenarios: list[ScenarioV1]
    risk: dict[str, Any]
    confidence: Literal["none", "low", "medium", "high"]
    counter_analysis: CounterAnalysisV1
    human_approval: HumanApprovalV1 | None
    v2_projection: V2ProjectionV1 | None = None
    content_sha256: Sha256

    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, serialize_by_alias=True
    )

    @model_validator(mode="after")
    def enforce_court_predicates(self) -> "UnderwritingResultV1":
        if not self.evidence_ids:
            raise ValueError("underwriting requires at least one evidence_id")
        if len(self.evidence_ids) != len(set(self.evidence_ids)):
            raise ValueError("evidence_ids must be unique")
        # BLACK's normative SYN-BLACK-001 carries a supported aggregate comp band
        # (comps_n=3) while intentionally omitting redistributable item rows.
        no_comps = not self.comps.comps_n
        if no_comps and self.valuation.point_value is not None:
            raise ValueError("no comps requires null valuation")
        if self.v2_projection is not None:
            if self.v2_projection.generated_at != self.generated_at:
                raise ValueError("v2_projection generated_at mismatch")
            if self.v2_projection.zoning != self.zoning:
                raise ValueError("v2_projection zoning mismatch")
            if self.v2_projection.constraints != self.constraints:
                raise ValueError("v2_projection constraints mismatch")
            if self.v2_projection.comps != self.comps:
                raise ValueError("v2_projection comps mismatch")
        if self.human_approval is None or not self.human_approval.required:
            raise ValueError("human approval gate is mandatory")
        return self


class UnderwritingRunContext(StrictModel):
    """Run linkage for the proof-chain lane; not part of the court V1 artifact."""

    run_id: NonEmpty
    case_id: NonEmpty
    case_class: Literal["synthetic", "test"]
    fixture_mode: Literal[True]
    calibration_eligible: Literal[False]
    source_sha: GitSha
    underwriting_id: UUID
    underwriting_sha256: Sha256

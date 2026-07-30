"""Semantic validators for the normative BLACK closed-loop V1 contracts.

The court JSON Schemas intentionally leave several nested objects open.  These
models add the acceptance predicates that JSON Schema cannot express, including
self-excluding hashes, human gates, calibration eligibility, and chain integrity.
"""

from __future__ import annotations

import copy
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.receipt import section_hash

SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
PII_KEYS = {
    "phone",
    "phone_number",
    "email",
    "email_address",
    "ssn",
    "social_security_number",
    "owner_name",
    "seller_name",
    "full_name",
}
IRREVERSIBLE_ACTIONS = {
    "dial_owner",
    "sms_owner",
    "email_owner",
    "mail_offer",
    "create_contract",
}
OFFER_CONTRACT_ACTIONS = {"mail_offer", "set_offer_price", "create_contract"}
SECTION_ORDER = (
    "SIGNAL",
    "CANDIDATE_SCORE",
    "CRM_LEAD",
    "EVIDENCE",
    "UNDERWRITING",
    "ACTION_PROPOSAL",
    "ACTION_RECEIPT",
    "OUTCOME",
    "CALIBRATION_LINK",
)


class ContractViolation(ValueError):
    """A BLACK contract or semantic acceptance predicate was violated."""


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, serialize_by_alias=True
    )


class OpenModel(BaseModel):
    model_config = ConfigDict(extra="allow")


def _plain(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return copy.deepcopy(value)


def _without_content_hash(value: BaseModel | dict[str, Any]) -> dict[str, Any]:
    body = _plain(value)
    body.pop("content_sha256", None)
    return body


def seal_content(value: dict[str, Any]) -> dict[str, Any]:
    """Return a new artifact sealed over canonical JSON excluding the seal itself."""

    body = copy.deepcopy(value)
    body.pop("content_sha256", None)
    body["content_sha256"] = section_hash(body)
    return body


def verify_content_hash(value: BaseModel | dict[str, Any]) -> bool:
    body = _plain(value)
    claimed = body.pop("content_sha256", None)
    return isinstance(claimed, str) and claimed == section_hash(body)


def assert_no_raw_pii_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in PII_KEYS:
                raise ContractViolation(f"raw PII key forbidden at {path}.{key}")
            assert_no_raw_pii_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_raw_pii_keys(item, f"{path}[{index}]")


class EvidenceSourceV1(StrictModel):
    system: Literal["landos-engine", "crm", "ingest", "human", "aegis", "external"]
    name: str = Field(min_length=1)
    ref: str | None = None
    ref_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class EvidenceEntityV1(StrictModel):
    tenant_id: uuid.UUID
    parcel_apn: str | None = None
    county_fips: str | None = None
    source_lead_id: uuid.UUID | None = None
    acquisition_lead_id: uuid.UUID | None = None
    situs_fingerprint_sha256: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{64}$"
    )


class LicensingV1(StrictModel):
    rights_note: str = Field(min_length=1)
    redistributable: bool
    retention_class: Literal["operational", "ephemeral", "vault_only"]


class EvidenceItemV1(StrictModel):
    schema_: Literal["neuruh.evidence_item.v1"] = Field(alias="schema")
    evidence_id: uuid.UUID
    source: EvidenceSourceV1
    acquired_at: AwareDatetime
    effective_at: AwareDatetime
    entity: EvidenceEntityV1
    value: Any
    unit: str | None
    method: Literal[
        "direct_read", "comp_median", "overlay_join", "human_assert", "model_infer"
    ]
    confidence: Literal["none", "low", "medium", "high"]
    conflict_status: Literal["none", "suspected", "confirmed", "resolved"]
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    licensing: LicensingV1
    key: str | None = None
    range: list[float | None] | None = Field(default=None, min_length=2, max_length=2)
    assumptions: list[str] = Field(default_factory=list)
    conflict_with: list[uuid.UUID] = Field(default_factory=list)
    supersedes: uuid.UUID | None = None
    verified: bool | None = None
    actor_role: Literal["system", "operator", "desk", "admin"] | None = None
    created_at: AwareDatetime | None = None

    @model_validator(mode="before")
    @classmethod
    def sealed_raw(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not verify_content_hash(value):
            raise ValueError("content_sha256 mismatch")
        return value

    @model_validator(mode="after")
    def court_semantics(self) -> "EvidenceItemV1":
        if self.effective_at > self.acquired_at + timedelta(days=1):
            raise ValueError("effective_at exceeds acquired_at by more than one day")
        if self.conflict_status != "none" and not self.conflict_with:
            raise ValueError("conflicting evidence requires conflict_with")
        if self.method == "model_infer" and not any(
            "evidence" in assumption.lower() for assumption in self.assumptions
        ):
            raise ValueError("model_infer requires an upstream evidence citation")
        assert_no_raw_pii_keys(self.entity.model_dump(mode="json"))
        return self


class ActionAuthorityV1(OpenModel):
    propose_role: str
    approve_role_min: str
    execute_role_min: str
    tenant_id: uuid.UUID
    capability_flags: list[str] = Field(default_factory=list)


class ActionCostV1(OpenModel):
    usd: float = Field(ge=0)
    minutes: float = Field(ge=0)
    compliance_risk: Literal["none", "low", "medium", "high"]
    burn_rate_note: str | None = None


class ActionApprovalV1(OpenModel):
    status: Literal["pending", "approved", "rejected", "expired"]
    actor_role: str | None = None
    actor_id_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    approved_at: AwareDatetime | None = None
    rejection_reason: str | None = None


class ActionProposalV1(StrictModel):
    schema_: Literal["neuruh.action_proposal.v1"] = Field(alias="schema")
    proposal_id: uuid.UUID
    created_at: AwareDatetime
    entity: dict[str, Any]
    proposed_action: Literal[
        "enrich_skip_trace",
        "dial_owner",
        "sms_owner",
        "email_owner",
        "mail_offer",
        "set_offer_price",
        "create_contract",
        "assign_buyer",
        "promote_stage",
        "suppress_dnc",
        "score_rescore",
        "underwrite_refresh",
        "no_action",
    ]
    authority: ActionAuthorityV1
    cost: ActionCostV1
    reversibility: Literal["reversible", "partially_reversible", "irreversible"]
    expected_outcome: dict[str, Any]
    downside: dict[str, Any]
    prerequisites: list[str]
    approval: ActionApprovalV1
    expiry: AwareDatetime
    receipt_links: dict[str, Any]
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="before")
    @classmethod
    def sealed_raw(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not verify_content_hash(value):
            raise ValueError("content_sha256 mismatch")
        return value

    @model_validator(mode="after")
    def court_semantics(self) -> "ActionProposalV1":
        if self.expiry <= self.created_at:
            raise ValueError("expiry must be after created_at")
        human_approval = (
            self.approval.status == "approved"
            and bool(self.approval.actor_role)
            and bool(self.approval.actor_id_sha256)
            and self.approval.approved_at is not None
        )
        if (
            self.reversibility == "irreversible"
            or self.proposed_action in IRREVERSIBLE_ACTIONS
        ) and not human_approval:
            raise ValueError("irreversible external action requires human approval")
        if self.proposed_action in OFFER_CONTRACT_ACTIONS:
            if not human_approval:
                raise ValueError("offer/contract action requires human approval")
            if "underwriting_human_approved" not in self.prerequisites:
                raise ValueError("offer/contract action requires underwriting approval gate")
            links = self.receipt_links
            if not links.get("underwriting_id") or not links.get("underwriting_sha256"):
                raise ValueError("offer/contract action requires sealed underwriting link")
        assert_no_raw_pii_keys(self.entity)
        return self


class HumanGradingV1(OpenModel):
    grade: Literal["success", "partial", "failure", "invalid", "unknown"]
    quality: Literal["high", "medium", "low"] | None = None
    notes: str | None = None
    grader_role: str | None = None
    graded_at: AwareDatetime | None = None
    calibration_eligible: bool


class OutcomeRecordV1(StrictModel):
    schema_: Literal["neuruh.outcome_record.v1"] = Field(alias="schema")
    outcome_id: uuid.UUID
    proposal_id: uuid.UUID | None = None
    entity: dict[str, Any]
    action_taken: str
    timestamps: dict[str, Any]
    cost: dict[str, Any]
    contact_result: dict[str, Any] | None = None
    contract_result: dict[str, Any] | None = None
    financial_result: dict[str, Any] | None = None
    cycle_time: dict[str, Any]
    deviations: list[str]
    external_factors: list[str]
    evidence_ids: list[uuid.UUID]
    human_grading: HumanGradingV1
    is_test: bool
    is_synthetic: bool
    forecast_version: str | None = None
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="before")
    @classmethod
    def sealed_raw(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not verify_content_hash(value):
            raise ValueError("content_sha256 mismatch")
        return value

    @model_validator(mode="after")
    def court_semantics(self) -> "OutcomeRecordV1":
        if (self.is_test or self.is_synthetic) and self.human_grading.calibration_eligible:
            raise ValueError("test/synthetic outcome cannot be calibration eligible")
        if self.human_grading.calibration_eligible:
            if self.human_grading.grade in {"invalid", "unknown"}:
                raise ValueError("invalid/unknown grade cannot be calibration eligible")
            if not self.forecast_version:
                raise ValueError("eligible outcome requires forecast_version")
            if not self.human_grading.grader_role or not self.human_grading.graded_at:
                raise ValueError("eligible outcome requires a human grader")
        assert_no_raw_pii_keys(self.entity)
        return self


class CalibrationRecordV1(StrictModel):
    schema_: Literal["neuruh.calibration_record.v1"] = Field(alias="schema")
    calibration_id: uuid.UUID
    forecast_version: str = Field(min_length=1)
    process_version: str = Field(min_length=1)
    window: dict[str, Any]
    n_samples: int = Field(ge=0)
    outcome_ids: list[uuid.UUID] = Field(default_factory=list)
    predicted: dict[str, Any]
    actual: dict[str, Any]
    error_metrics: dict[str, Any]
    confidence_calibration: dict[str, Any]
    causal_caveats: list[str]
    model_or_process: str
    recommended_adjustment: dict[str, Any]
    forbidden_overfitting_rule: dict[str, Any]
    next_sample_threshold: int = Field(ge=1)
    human_acceptance: dict[str, Any]
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="before")
    @classmethod
    def sealed_raw(cls, value: Any) -> Any:
        if not isinstance(value, dict) or not verify_content_hash(value):
            raise ValueError("content_sha256 mismatch")
        return value

    @model_validator(mode="after")
    def court_semantics(self) -> "CalibrationRecordV1":
        rule = self.forbidden_overfitting_rule
        if (
            rule.get("min_samples_for_claim") != 30
            or rule.get("min_samples_for_weight_change") != 100
            or rule.get("holdout_required") is not True
            or rule.get("auto_apply_forbidden") is not True
        ):
            raise ValueError("forbidden overfitting rule does not match BLACK law")
        if self.n_samples != len(self.outcome_ids):
            raise ValueError("n_samples must match outcome_ids")
        if self.error_metrics.get("n") != self.n_samples:
            raise ValueError("error_metrics.n must match n_samples")
        adjustment = self.recommended_adjustment
        if self.n_samples < 30 and adjustment.get("type") not in {"none", "monitor"}:
            raise ValueError("fewer than 30 samples cannot support an adjustment claim")
        if self.n_samples < 100 and adjustment.get("status") == "accepted":
            raise ValueError("weight/config change cannot be accepted below 100 samples")
        return self


class ArtifactRefV1(StrictModel):
    type: str
    id: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ManifestSectionV1(OpenModel):
    present: bool
    artifact_refs: list[ArtifactRefV1]
    authority: Literal["landos-engine", "crm", "aegis", "vault", "human"]
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def present_matches_refs(self) -> "ManifestSectionV1":
        if self.present and not self.artifact_refs:
            raise ValueError("present section requires artifact_refs")
        if not self.present and self.artifact_refs:
            raise ValueError("absent section cannot claim artifact_refs")
        return self


class OneCaseProofManifestV1(StrictModel):
    schema_: Literal["neuruh.one_case_proof_manifest.v1"] = Field(alias="schema")
    manifest_id: uuid.UUID
    case_id: str = Field(min_length=1)
    case_class: Literal["synthetic", "operator_live"]
    created_at: AwareDatetime
    entity: dict[str, Any]
    sections: dict[str, ManifestSectionV1]
    section_hashes: dict[str, str]
    chain_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    authority: dict[str, Any]
    gate_results: list[dict[str, Any]]
    verdict: Literal["PASS", "FAIL", "INCOMPLETE"]

    @field_validator("sections")
    @classmethod
    def fixed_sections(
        cls, value: dict[str, ManifestSectionV1]
    ) -> dict[str, ManifestSectionV1]:
        if set(value) != set(SECTION_ORDER) or len(value) != len(SECTION_ORDER):
            raise ValueError("manifest sections do not match the BLACK fixed set")
        return value

    @model_validator(mode="after")
    def court_semantics(self) -> "OneCaseProofManifestV1":
        if set(self.section_hashes) != set(SECTION_ORDER):
            raise ValueError("section_hashes do not match the BLACK fixed set")
        if any(not SHA256_RE.fullmatch(value) for value in self.section_hashes.values()):
            raise ValueError("invalid section hash")
        if any(gate.get("status") == "fail" for gate in self.gate_results):
            if self.verdict == "PASS":
                raise ValueError("PASS cannot ignore a failed gate")
        assert_no_raw_pii_keys(self.entity)
        validate_manifest_chain(self.model_dump(mode="json", by_alias=True))
        return self


def manifest_chain_hash(section_hashes: dict[str, str]) -> str:
    if set(section_hashes) != set(SECTION_ORDER):
        raise ContractViolation("section hashes do not match fixed BLACK sections")
    ordered_hashes = {name: section_hashes[name] for name in SECTION_ORDER}
    return section_hash(
        {
            "algo": "sha256",
            "order": list(SECTION_ORDER),
            "section_hashes": ordered_hashes,
        }
    )


def validate_manifest_chain(manifest: dict[str, Any]) -> None:
    sections = manifest.get("sections")
    hashes = manifest.get("section_hashes")
    if not isinstance(sections, dict) or set(sections) != set(SECTION_ORDER):
        raise ContractViolation("manifest section set mismatch")
    if not isinstance(hashes, dict) or set(hashes) != set(SECTION_ORDER):
        raise ContractViolation("manifest section-hash set mismatch")
    recomputed = {name: section_hash(sections[name]) for name in SECTION_ORDER}
    if any(hashes.get(name) != recomputed[name] for name in SECTION_ORDER):
        raise ContractViolation("manifest section hash mismatch")
    if manifest.get("chain_sha256") != manifest_chain_hash(recomputed):
        raise ContractViolation("manifest chain_sha256 mismatch")


def validate_underwriting_result(value: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "underwriting_id",
        "generated_at",
        "method_version",
        "entity",
        "evidence_ids",
        "zoning",
        "constraints",
        "comps",
        "valuation",
        "assumptions",
        "ranges",
        "scenarios",
        "risk",
        "confidence",
        "counter_analysis",
        "human_approval",
        "content_sha256",
    }
    missing = sorted(required - set(value))
    extra = sorted(set(value) - (required | {"v2_projection"}))
    if missing or extra:
        raise ContractViolation(f"underwriting fields missing={missing} extra={extra}")
    if value.get("schema") != "neuruh.underwriting_result.v1":
        raise ContractViolation("wrong underwriting schema")
    try:
        uuid.UUID(str(value["underwriting_id"]))
        datetime.fromisoformat(str(value["generated_at"]).replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise ContractViolation(f"invalid underwriting identity/time: {exc}") from exc
    if value["confidence"] not in {"none", "low", "medium", "high"}:
        raise ContractViolation("invalid underwriting confidence")
    valuation = value["valuation"]
    if valuation.get("point_value") is None and not valuation.get("reason_if_null"):
        raise ContractViolation("null valuation requires reason_if_null")
    approval = value["human_approval"]
    if not isinstance(approval, dict) or approval.get("required") is not True:
        raise ContractViolation("human underwriting approval gate must remain mandatory")
    if not verify_content_hash(value):
        raise ContractViolation("underwriting content_sha256 mismatch")
    assert_no_raw_pii_keys(value["entity"])
    return copy.deepcopy(value)


def validate_artifact(value: dict[str, Any]) -> BaseModel | dict[str, Any]:
    """Validate and hash-check one normative BLACK artifact."""

    schema = value.get("schema")
    try:
        if schema == "neuruh.evidence_item.v1":
            return EvidenceItemV1.model_validate(value)
        if schema == "neuruh.underwriting_result.v1":
            return validate_underwriting_result(value)
        if schema == "neuruh.action_proposal.v1":
            return ActionProposalV1.model_validate(value)
        if schema == "neuruh.outcome_record.v1":
            return OutcomeRecordV1.model_validate(value)
        if schema == "neuruh.calibration_record.v1":
            return CalibrationRecordV1.model_validate(value)
        if schema == "neuruh.one_case_proof_manifest.v1":
            return OneCaseProofManifestV1.model_validate(value)
    except (ValidationError, ValueError, TypeError) as exc:
        raise ContractViolation(str(exc)) from exc
    raise ContractViolation(f"unknown BLACK artifact schema: {schema!r}")

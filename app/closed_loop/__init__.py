"""BLACK court closed-loop proof contracts and deterministic bundle generator."""

from .contracts import (
    ActionProposalV1,
    CalibrationRecordV1,
    ContractViolation,
    EvidenceItemV1,
    OneCaseProofManifestV1,
    OutcomeRecordV1,
    seal_content,
    validate_artifact,
    validate_manifest_chain,
    verify_content_hash,
)
from .proof import (
    CASE_ID,
    SECTION_ORDER,
    build_synthetic_one_case_proof,
    verify_bundle,
    write_synthetic_one_case_proof,
)

__all__ = [
    "ActionProposalV1",
    "CalibrationRecordV1",
    "CASE_ID",
    "ContractViolation",
    "EvidenceItemV1",
    "OneCaseProofManifestV1",
    "OutcomeRecordV1",
    "SECTION_ORDER",
    "build_synthetic_one_case_proof",
    "seal_content",
    "validate_artifact",
    "validate_manifest_chain",
    "verify_bundle",
    "verify_content_hash",
    "write_synthetic_one_case_proof",
]

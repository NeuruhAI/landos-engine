"""Normative BLACK UnderwritingResultV1, produced without external writes."""

from .adapters import (
    AcquisitionLeadUnderwritingResultAdapter,
    UnderwritingResultAdapter,
)
from .canonical import content_sha256, verify_content_hash
from .models import (
    EvidenceItemV1,
    UnderwritingCaseV1,
    UnderwritingResultV1,
    UnderwritingRunContext,
)
from .producer import (
    UnderwritingProductionError,
    produce_underwriting_result,
    underwriting_run_context,
    verify_receipt,
)

__all__ = [
    "AcquisitionLeadUnderwritingResultAdapter",
    "EvidenceItemV1",
    "UnderwritingCaseV1",
    "UnderwritingProductionError",
    "UnderwritingResultAdapter",
    "UnderwritingResultV1",
    "UnderwritingRunContext",
    "content_sha256",
    "produce_underwriting_result",
    "underwriting_run_context",
    "verify_content_hash",
    "verify_receipt",
]

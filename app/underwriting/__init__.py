"""UnderwritingResultV1: deterministic, non-production underwriting artifacts."""

from .adapters import (
    AcquisitionLeadUnderwritingResultAdapter,
    UnderwritingResultAdapter,
)
from .models import UnderwritingCaseV1, UnderwritingResultV1
from .producer import (
    UnderwritingProductionError,
    produce_underwriting_result,
    verify_receipt,
)

__all__ = [
    "AcquisitionLeadUnderwritingResultAdapter",
    "UnderwritingCaseV1",
    "UnderwritingProductionError",
    "UnderwritingResultAdapter",
    "UnderwritingResultV1",
    "produce_underwriting_result",
    "verify_receipt",
]

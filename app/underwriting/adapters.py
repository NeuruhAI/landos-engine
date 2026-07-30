"""Read-only adapter contract for a future CRM-side persistence implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from .models import UnderwritingResultV1


@runtime_checkable
class UnderwritingResultAdapter(Protocol):
    """Transforms a result for a target; deliberately does not define write/execute."""

    target_table: str
    target_field: str

    def prepare(self, result: UnderwritingResultV1) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class AcquisitionLeadUnderwritingResultAdapter:
    """Future payload mapping only. This class cannot perform a database write."""

    target_table: str = "public.acquisition_leads"
    target_field: str = "underwriting_result"

    def prepare(self, result: UnderwritingResultV1) -> Mapping[str, Any]:
        if result.production_write_performed:
            raise ValueError("refusing result that claims a production write")
        if not result.lead_id:
            raise ValueError(
                "synthetic fixture has no acquisition_leads target identifier"
            )
        return {
            "lead_id": result.lead_id,
            self.target_field: result.model_dump(mode="json"),
            "expected_target": f"{self.target_table}.{self.target_field}",
            "write_performed": False,
        }

"""Preparation-only adapter for the existing CRM underwriting_result.v2 column."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from .models import UnderwritingResultV1
from .producer import verify_receipt


@runtime_checkable
class UnderwritingResultAdapter(Protocol):
    """Projection only. Deliberately no write, execute, connection, or credential API."""

    target_table: str
    target_field: str

    def prepare(self, result: UnderwritingResultV1) -> Mapping[str, Any]:
        ...


@dataclass(frozen=True)
class AcquisitionLeadUnderwritingResultAdapter:
    """Prepare the exact v2 subset without touching CRM or any database."""

    target_table: str = "public.acquisition_leads"
    target_field: str = "underwriting_result"

    def prepare(self, result: UnderwritingResultV1) -> Mapping[str, Any]:
        if not verify_receipt(result):
            raise ValueError("refusing artifact with invalid content_sha256")
        acquisition_lead_id = result.entity.get("acquisition_lead_id")
        if not acquisition_lead_id:
            raise ValueError("artifact has no acquisition_lead_id target")
        if result.v2_projection is None:
            raise ValueError("artifact has no v2_projection")
        return {
            "lead_id": acquisition_lead_id,
            self.target_field: result.v2_projection.model_dump(
                mode="json", by_alias=True
            ),
            "expected_target": f"{self.target_table}.{self.target_field}",
            "preparation_only": True,
            "write_performed": False,
        }

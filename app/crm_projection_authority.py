"""Candidate receipt shape/integrity validation for the sole CRM writer.

This module does not grant mutation authority. Ordinary SHA-256 is not an
authentic Governance signature; ``sync_to_crm`` therefore remains fail-closed
after this validation until the canonical receipt spine verifier is integrated.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "landos.crm_projection_authority.v1"
ACTION = "landos.crm.sync"


class AuthorityError(RuntimeError):
    """Raised before any database connection when projection is not authorized."""


def canonical_payload(receipt: Mapping[str, Any]) -> bytes:
    payload = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def validate_authority_receipt(
    receipt: Mapping[str, Any], *, tenant_id: str, now: datetime | None = None
) -> dict[str, Any]:
    required = {
        "schema", "action", "tenant_id", "governance_decision",
        "human_approved", "approved_by", "issued_at", "expires_at",
        "nonce", "receipt_sha256",
    }
    missing = sorted(required - set(receipt))
    if missing:
        raise AuthorityError(f"authority receipt missing fields: {', '.join(missing)}")
    if receipt["schema"] != SCHEMA or receipt["action"] != ACTION:
        raise AuthorityError("authority receipt schema/action mismatch")
    if receipt["tenant_id"] != tenant_id:
        raise AuthorityError("authority receipt tenant mismatch")
    if receipt["governance_decision"] != "ESCALATE" or receipt["human_approved"] is not True:
        raise AuthorityError("mutation requires Governance ESCALATE plus typed human approval")
    if not isinstance(receipt["approved_by"], str) or not receipt["approved_by"].strip():
        raise AuthorityError("approved_by must identify the approving human")
    if not isinstance(receipt["nonce"], str) or len(receipt["nonce"].strip()) < 16:
        raise AuthorityError("authority nonce must be at least 16 characters")
    digest = hashlib.sha256(canonical_payload(receipt)).hexdigest()
    if receipt["receipt_sha256"] != digest:
        raise AuthorityError("authority receipt hash mismatch")
    current = now or datetime.now(timezone.utc)
    try:
        issued = datetime.fromisoformat(str(receipt["issued_at"]).replace("Z", "+00:00"))
        expires = datetime.fromisoformat(str(receipt["expires_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorityError("authority receipt timestamps must be ISO-8601") from exc
    if issued.tzinfo is None or expires.tzinfo is None:
        raise AuthorityError("authority receipt timestamps must be timezone-aware")
    if issued > current or expires <= current or expires <= issued:
        raise AuthorityError("authority receipt is not currently valid")
    return dict(receipt)


def load_authority_receipt(path: str | Path, *, tenant_id: str) -> dict[str, Any]:
    candidate = Path(path)
    if candidate.is_symlink() or not candidate.is_file():
        raise AuthorityError("authority receipt must be a regular non-symlink file")
    try:
        receipt = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuthorityError("authority receipt is not readable JSON") from exc
    if not isinstance(receipt, dict):
        raise AuthorityError("authority receipt must be a JSON object")
    return validate_authority_receipt(receipt, tenant_id=tenant_id)

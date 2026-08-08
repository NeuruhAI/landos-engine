from __future__ import annotations

import ast
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.crm_projection_authority import AuthorityError, canonical_payload, validate_authority_receipt

ROOT = Path(__file__).resolve().parents[1]
SOLE_WRITER = ROOT / "scripts" / "sync_to_crm.py"
LEGACY_ADAPTERS = (
    ROOT / "app" / "lead_visibility.py",
    ROOT / "scripts" / "retire_restricted_taxsale.py",
    ROOT / "scripts" / "scrub_conversion.py",
)
DML = re.compile(r"\b(?:insert\s+into|update|delete\s+from)\s+public\.acquisition_leads\b", re.I)


def _string_literals(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return "\n".join(
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def test_sync_to_crm_is_the_only_runtime_acquisition_leads_dml_owner():
    offenders = []
    for folder in (ROOT / "app", ROOT / "scripts"):
        for path in folder.rglob("*.py"):
            if DML.search(_string_literals(path)):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == ["scripts/sync_to_crm.py"]


@pytest.mark.parametrize("path", LEGACY_ADAPTERS)
def test_legacy_crm_maintenance_modules_are_request_only(path: Path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "psycopg" not in imported
    assert not DML.search(_string_literals(path))
    assert ".connect(" not in source
    assert ".commit(" not in source


def _receipt(**overrides):
    now = datetime.now(timezone.utc)
    receipt = {
        "schema": "landos.crm_projection_authority.v1",
        "action": "landos.crm.sync",
        "tenant_id": "1840ac90-54bd-4b3d-8baf-227c8d84ab5a",
        "governance_decision": "ESCALATE",
        "human_approved": True,
        "approved_by": "operator@example.invalid",
        "issued_at": (now - timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "nonce": "case-zero-nonce-0001",
    }
    receipt.update(overrides)
    receipt["receipt_sha256"] = hashlib.sha256(canonical_payload(receipt)).hexdigest()
    return receipt


def test_typed_authority_receipt_shape_accepts_exact_escalate_plus_human_approval():
    receipt = _receipt()
    assert validate_authority_receipt(receipt, tenant_id=receipt["tenant_id"])["nonce"]


def test_shape_and_self_hash_never_become_execution_authority(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nonprod.invalid/never-connect")
    monkeypatch.setenv("LANDOS_SECRET", "nonprod-import-only")
    from scripts.sync_to_crm import _require_authority

    with pytest.raises(RuntimeError, match="canonical Governance signature"):
        _require_authority(_receipt())


@pytest.mark.parametrize(
    "patch",
    [
        {"human_approved": False},
        {"governance_decision": "ALLOW"},
        {"tenant_id": "other-tenant"},
        {"action": "seller_contact"},
    ],
)
def test_typed_authority_receipt_fails_closed(patch):
    receipt = _receipt(**patch)
    with pytest.raises(AuthorityError):
        validate_authority_receipt(
            receipt, tenant_id="1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
        )


def test_sync_cli_defaults_to_dry_run_and_requires_apply_receipt():
    source = SOLE_WRITER.read_text(encoding="utf-8")
    assert "dry_run = not args.apply" in source
    assert "def main(dry_run: bool = True" in source
    assert source.index("_require_authority(authority_receipt)") < source.index("psycopg.connect(")
    assert "canonical Governance signature + receipt-spine replay verification" in source
    assert source.index("canonical Governance signature + receipt-spine replay verification") < source.index("psycopg.connect(")

"""BLACK contract semantics, proof-chain, deterministic generation, and tamper tests."""

from __future__ import annotations

import copy
import json

import pytest
from pydantic import ValidationError

from app.closed_loop import (
    ActionProposalV1,
    ContractViolation,
    EvidenceItemV1,
    OutcomeRecordV1,
    SECTION_ORDER,
    build_synthetic_one_case_proof,
    seal_content,
    verify_bundle,
    write_synthetic_one_case_proof,
)
from app.receipt import canonical_json
from app.receipt import section_hash
from app.closed_loop.contracts import manifest_chain_hash
from scripts import generate_one_case_proof as cli


def test_syn_black_001_is_deterministic_valid_and_fully_linked():
    first = build_synthetic_one_case_proof()
    second = build_synthetic_one_case_proof()
    assert canonical_json(first) == canonical_json(second)
    verify_bundle(first)
    manifest = first["one-case-proof-manifest-v1.json"]
    assert manifest["case_id"] == "SYN-BLACK-001"
    assert tuple(manifest["sections"]) == SECTION_ORDER
    assert tuple(manifest["section_hashes"]) == SECTION_ORDER
    assert manifest["verdict"] == "PASS"
    assert first["bundle-index.json"]["run_id"] == "SYN-BLACK-001"
    assert first["bundle-index.json"]["production_writes_performed"] is False


def test_three_evidence_items_are_sealed_and_pii_free():
    bundle = build_synthetic_one_case_proof()
    paths = sorted(path for path in bundle if path.startswith("evidence-"))
    assert len(paths) == 3
    for path in paths:
        EvidenceItemV1.model_validate(bundle[path])
        serialized = canonical_json(bundle[path]).lower()
        assert '"phone"' not in serialized
        assert '"email"' not in serialized
        assert '"owner_name"' not in serialized


def test_synthetic_outcome_is_explicitly_excluded_from_calibration():
    bundle = build_synthetic_one_case_proof()
    outcome = bundle["outcome-record-v1.json"]
    calibration = bundle["calibration-record-v1.json"]
    assert outcome["is_synthetic"] is True
    assert outcome["is_test"] is False
    assert outcome["human_grading"]["calibration_eligible"] is False
    assert calibration["n_samples"] == 0
    assert calibration["outcome_ids"] == []


def test_negative_black_acceptance_case_synthetic_calibration_fails():
    outcome = copy.deepcopy(build_synthetic_one_case_proof()["outcome-record-v1.json"])
    outcome["human_grading"]["calibration_eligible"] = True
    outcome = seal_content(outcome)
    with pytest.raises(ValidationError, match="cannot be calibration eligible"):
        OutcomeRecordV1.model_validate(outcome)


def test_irreversible_offer_and_contract_human_gates_are_mandatory():
    action = copy.deepcopy(build_synthetic_one_case_proof()["action-proposal-v1.json"])
    action["proposed_action"] = "create_contract"
    action["reversibility"] = "irreversible"
    action["approval"] = {
        "status": "pending",
        "actor_role": None,
        "actor_id_sha256": None,
        "approved_at": None,
        "rejection_reason": None,
    }
    action = seal_content(action)
    with pytest.raises(ValidationError, match="requires human approval"):
        ActionProposalV1.model_validate(action)

    action["approval"]["status"] = "approved"
    action["approval"]["actor_role"] = "operator"
    action["approval"]["actor_id_sha256"] = "e" * 64
    action["approval"]["approved_at"] = "2026-07-30T21:07:30Z"
    action = seal_content(action)
    with pytest.raises(ValidationError, match="underwriting approval gate"):
        ActionProposalV1.model_validate(action)


def test_manifest_section_and_artifact_tampering_fails_closed():
    bundle = build_synthetic_one_case_proof()
    tampered_section = copy.deepcopy(bundle)
    tampered_section["one-case-proof-manifest-v1.json"]["sections"]["OUTCOME"][
        "notes"
    ].append("tampered")
    with pytest.raises(ContractViolation, match="section hash mismatch"):
        verify_bundle(tampered_section)

    tampered_artifact = copy.deepcopy(bundle)
    tampered_artifact["outcome-record-v1.json"]["action_taken"] = "dial_owner"
    with pytest.raises(ContractViolation, match="content_sha256 mismatch"):
        verify_bundle(tampered_artifact)

    forged_links = copy.deepcopy(bundle)
    manifest = forged_links["one-case-proof-manifest-v1.json"]
    manifest["sections"]["OUTCOME"]["artifact_refs"][0]["sha256"] = "a" * 64
    manifest["section_hashes"] = {
        name: section_hash(manifest["sections"][name]) for name in SECTION_ORDER
    }
    manifest["chain_sha256"] = manifest_chain_hash(manifest["section_hashes"])
    with pytest.raises(ContractViolation, match="artifact_refs do not match"):
        verify_bundle(forged_links)


def test_bundle_index_tampering_fails_closed():
    bundle = build_synthetic_one_case_proof()
    bundle["bundle-index.json"]["artifacts"][0]["content_sha256"] = "0" * 64
    bundle["bundle-index.json"] = seal_content(bundle["bundle-index.json"])
    with pytest.raises(ContractViolation, match="index hash mismatch"):
        verify_bundle(bundle)


def test_generator_writes_atomically_and_refuses_implicit_overwrite(tmp_path, capsys):
    output = tmp_path / "SYN-BLACK-001"
    written = write_synthetic_one_case_proof(output)
    assert len(written) == 12
    assert cli.main(["--output", str(output), "--overwrite"]) == 0
    assert "ONE_CASE_PROOF_OK" in capsys.readouterr().err
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_synthetic_one_case_proof(output)
    loaded = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in output.glob("*.json")
    }
    verify_bundle(loaded)

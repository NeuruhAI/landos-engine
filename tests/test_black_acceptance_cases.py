"""Offline acceptance of the byte-pinned BLACK court contracts and fixtures."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.underwriting import (
    UnderwritingResultV1,
    produce_underwriting_result,
    underwriting_run_context,
    verify_receipt,
)
from app.closed_loop import (
    ActionProposalV1,
    CalibrationRecordV1,
    EvidenceItemV1,
    OneCaseProofManifestV1,
    OutcomeRecordV1,
    seal_content as seal_closed_loop_content,
)


ROOT = Path(__file__).resolve().parents[1]
BLACK_DIR = ROOT / "contracts" / "black-v1"


def _load_harness() -> ModuleType:
    path = BLACK_DIR / "acceptance.py"
    spec = importlib.util.spec_from_file_location("test_black_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


black = _load_harness()


def test_normative_inputs_match_pinned_court_hashes() -> None:
    manifest = json.loads((BLACK_DIR / "SOURCE_MANIFEST.json").read_text())
    assert manifest["source_package"] == "BLACK-CLOSED-LOOP-COURT-20260730T212633Z"
    assert manifest["court_manifest_sha256"] == (
        "d2bf52e276dee91ffff1e54529fa6497425487b55d1d074eb157144d84c3f594"
    )
    assert len(manifest["files"]) == 7
    for relative_path, expected in manifest["files"].items():
        actual = hashlib.sha256((BLACK_DIR / relative_path).read_bytes()).hexdigest()
        assert actual == expected, relative_path


def test_every_offline_black_predicate_passes() -> None:
    results = black.run_acceptance_cases()
    black.assert_all_pass(results)
    assert [result.acceptance_id for result in results] == [
        "AT-EV-01",
        "AT-EV-02",
        "AT-EV-03",
        "AT-UW-01",
        "AT-UW-02",
        "AT-UW-03",
        "AT-UW-04",
        "AT-AP-01",
        "AT-AP-02",
        "AT-AP-03",
        "AT-OR-01",
        "AT-OR-02",
        "AT-OR-03",
        "AT-CR-01",
        "AT-CR-02",
        "AT-CR-03",
        "AT-MF-01",
        "AT-MF-02",
        "AT-MF-03",
        "AT-SS-01",
        "AT-SS-02",
    ]


def test_schema_validator_rejects_missing_required_and_extra_fields() -> None:
    evidence = black.load_fixture_package()["cases"][0]["evidence_items"][0]
    missing = copy.deepcopy(evidence)
    missing.pop("source")
    with pytest.raises(black.ContractViolation):
        black.validate_contract("evidence_item", missing)
    extra = copy.deepcopy(evidence)
    extra["surprise"] = True
    with pytest.raises(black.ContractViolation):
        black.validate_contract("evidence_item", extra)


def test_normative_underwriting_fixture_parses_current_pydantic_contract() -> None:
    payload = black.load_fixture_package()["cases"][0]["underwriting_result"]
    result = UnderwritingResultV1.model_validate(payload)
    assert result.model_dump(mode="json", by_alias=True) == payload


def test_orange_producer_emits_validated_black_envelope_linked_to_run() -> None:
    raw_case = json.loads(
        (ROOT / "tests" / "fixtures" / "underwriting" / "synthetic_case_v1.json").read_text()
    )
    result = produce_underwriting_result(raw_case)
    payload = result.model_dump(mode="json", by_alias=True)
    black.validate_underwriting_semantics(payload)
    assert verify_receipt(result)
    assert payload["schema"] == "neuruh.underwriting_result.v1"
    assert payload["human_approval"]["required"] is True
    assert payload["risk"]["fixture_mode"] is True
    assert payload["risk"]["calibration_eligible"] is False
    assert payload["risk"]["production_write_performed"] is False
    context = underwriting_run_context(raw_case, result)
    assert context.run_id == raw_case["run_id"]
    assert context.underwriting_sha256 == result.content_sha256


def test_sealed_court_fixtures_parse_current_closed_loop_pydantic_contracts() -> None:
    case = black.load_fixture_package()["cases"][0]
    for evidence in case["evidence_items"]:
        EvidenceItemV1.model_validate(seal_closed_loop_content(evidence))
    ActionProposalV1.model_validate(
        seal_closed_loop_content(case["action_proposal"])
    )
    OutcomeRecordV1.model_validate(seal_closed_loop_content(case["outcome_record"]))
    CalibrationRecordV1.model_validate(
        seal_closed_loop_content(case["calibration_record"])
    )
    OneCaseProofManifestV1.model_validate(black.seal_manifest(case["manifest"]))


def test_human_mint_offer_and_contract_gates_fail_closed() -> None:
    case = black.load_fixture_package()["cases"][0]
    proposal = copy.deepcopy(case["action_proposal"])
    proposal["proposed_action"] = "mint_candidate"
    with pytest.raises(black.ContractViolation):
        black.validate_action_semantics(proposal)

    for action in ("set_offer_price", "create_contract"):
        proposal = copy.deepcopy(case["action_proposal"])
        proposal["proposed_action"] = action
        proposal["reversibility"] = "irreversible"
        proposal["approval"]["status"] = "pending"
        proposal["prerequisites"] = ["underwriting_human_approved"]
        with pytest.raises(black.ContractViolation):
            black.validate_action_semantics(
                proposal, underwriting=case["underwriting_result"]
            )


def test_manifest_chain_detects_body_hash_and_order_tamper() -> None:
    manifest = black.load_fixture_package()["cases"][0]["manifest"]
    sealed = black.seal_manifest(manifest)
    assert black.verify_manifest_chain(sealed)

    body_tamper = copy.deepcopy(sealed)
    body_tamper["sections"]["EVIDENCE"]["notes"].append("changed")
    assert not black.verify_manifest_chain(body_tamper)

    hash_tamper = copy.deepcopy(sealed)
    hash_tamper["section_hashes"]["EVIDENCE"] = "0" * 64
    assert not black.verify_manifest_chain(hash_tamper)

    order_tamper = copy.deepcopy(sealed)
    order_tamper["sections"] = dict(reversed(order_tamper["sections"].items()))
    assert not black.verify_manifest_chain(order_tamper)


def test_cli_emits_exact_ids_and_returns_zero() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_black_acceptance.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    emitted = [
        line.split()[1]
        for line in completed.stdout.splitlines()
        if line.startswith(("PASS ", "FAIL "))
    ]
    expected = [
        result.acceptance_id for result in black.run_acceptance_cases()
    ]
    assert emitted == expected
    assert "BLACK_ACCEPTANCE PASS" in completed.stdout


def test_cli_json_is_machine_readable() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "run_black_acceptance.py"),
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    rows = json.loads(completed.stdout)
    assert rows
    assert all(row["status"] == "PASS" for row in rows)

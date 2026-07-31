"""Deterministic, file-only SYN-BLACK-001 one-case proof generation."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.receipt import canonical_json, section_hash

from .contracts import (
    SECTION_ORDER,
    ContractViolation,
    manifest_chain_hash,
    seal_content,
    validate_artifact,
    validate_manifest_chain,
    verify_content_hash,
)

CASE_ID = "SYN-BLACK-001"
TENANT_ID = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
ENTITY = {
    "tenant_id": TENANT_ID,
    "parcel_apn": "SYN-00-0000-000",
    "county_fips": "18000",
}
OPERATOR_SHA = section_hash("synthetic machinery approval; no human identity")
CRM_SOURCE_SHA = "a271107c1a9832c57ea05e96a4929964818d74f9"
AEGIS_SOURCE_SHA = "e8850ab"
FULL_GIT_SHA = re.compile(r"^[a-f0-9]{40}$")


def _evidence(source_sha: str | None = None) -> list[dict[str, Any]]:
    # The emitted evidence is byte-identical to the producer input. This prevents
    # a same-ID/different-body split between the proof chain and underwriting.
    from app.underwriting import UnderwritingCaseV1

    case = UnderwritingCaseV1.model_validate(_underwriting_input(source_sha))
    return [
        item.model_dump(mode="json", by_alias=True)
        for item in case.evidence_items
    ]


def _underwriting_input(source_sha: str | None = None) -> dict[str, Any]:
    """Load the canonical file-only BLACK fixture and pin its run ID."""

    root = Path(__file__).resolve().parents[2]
    fixture = root / "tests" / "fixtures" / "underwriting" / "synthetic_case_v1.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    raw["run_id"] = CASE_ID
    if source_sha is not None:
        raw["source_sha"] = source_sha
    return raw


def _produce_underwriting(source_sha: str | None = None) -> dict[str, Any]:
    # Late import prevents this file-only proof module from initializing app runtime.
    from app.underwriting import (
        produce_underwriting_result,
        underwriting_run_context,
        verify_receipt,
    )

    input_payload = _underwriting_input(source_sha)
    produced = produce_underwriting_result(input_payload)
    if not verify_receipt(produced):
        raise ContractViolation("underwriting producer emitted an invalid receipt")
    context = underwriting_run_context(input_payload, produced)
    if context.run_id != CASE_ID or context.case_id != CASE_ID:
        raise ContractViolation("underwriting run context does not match SYN-BLACK-001")
    return produced.model_dump(mode="json", by_alias=True)


def _action(underwriting: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return seal_content(
        {
            "schema": "neuruh.action_proposal.v1",
            "proposal_id": "33333333-3333-4333-8333-333333333301",
            "created_at": "2026-07-30T21:07:00Z",
            "entity": {**ENTITY, "run_id": CASE_ID},
            "proposed_action": "no_action",
            "authority": {
                "propose_role": "system",
                "approve_role_min": "operator",
                "execute_role_min": "operator",
                "tenant_id": TENANT_ID,
                "capability_flags": [],
            },
            "cost": {
                "usd": 0,
                "minutes": 5,
                "compliance_risk": "none",
                "burn_rate_note": "synthetic desk time only",
            },
            "reversibility": "reversible",
            "expected_outcome": {
                "primary": "data_gain",
                "predicted_values": {"machinery_proof": 1},
                "forecast_version": "synthetic-forecast-v0",
                "confidence": "high",
                "horizon_days": 0,
            },
            "downside": {
                "summary": "No external action; no mouth, money, or production write.",
                "worst_case_usd": 0,
                "legal_risk": "none",
            },
            "prerequisites": ["human_approval_synthetic_machinery_only"],
            "approval": {
                "status": "approved",
                "actor_role": "operator",
                "actor_id_sha256": OPERATOR_SHA,
                "approved_at": "2026-07-30T21:07:30Z",
                "rejection_reason": None,
            },
            "expiry": "2026-08-06T21:07:00Z",
            "receipt_links": {
                "underwriting_id": underwriting["underwriting_id"],
                "underwriting_sha256": underwriting["content_sha256"],
                "evidence_ids": [item["evidence_id"] for item in evidence],
                "lead_receipt_chain_sha256": None,
                "aegis_decision_id": None,
            },
        }
    )


def _outcome(action: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    return seal_content(
        {
            "schema": "neuruh.outcome_record.v1",
            "outcome_id": "44444444-4444-4444-8444-444444444401",
            "proposal_id": action["proposal_id"],
            "entity": {**ENTITY, "run_id": CASE_ID},
            "action_taken": "no_action",
            "timestamps": {
                "proposed_at": action["created_at"],
                "approved_at": action["approval"]["approved_at"],
                "started_at": action["approval"]["approved_at"],
                "completed_at": "2026-07-30T21:08:00Z",
                "observed_at": "2026-07-30T21:08:00Z",
            },
            "cost": {"usd": 0, "minutes": 5},
            "contact_result": None,
            "contract_result": None,
            "financial_result": None,
            "cycle_time": {"hours_to_complete": 0.0083, "days_to_close": None},
            "deviations": [],
            "external_factors": ["synthetic case; no external execution"],
            "evidence_ids": [evidence[0]["evidence_id"]],
            "human_grading": {
                "grade": "success",
                "quality": "high",
                "notes": "Synthetic machinery proof only; not a market outcome.",
                "grader_role": "operator",
                "graded_at": "2026-07-30T21:08:00Z",
                "calibration_eligible": False,
            },
            "is_test": False,
            "is_synthetic": True,
            "forecast_version": "synthetic-forecast-v0",
        }
    )


def _calibration() -> dict[str, Any]:
    return seal_content(
        {
            "schema": "neuruh.calibration_record.v1",
            "calibration_id": "55555555-5555-4555-8555-555555555501",
            "forecast_version": "synthetic-forecast-v0",
            "process_version": "black-court-20260730",
            "window": {
                "from": "2026-07-30T00:00:00Z",
                "to": "2026-07-30T23:59:59Z",
            },
            "n_samples": 0,
            "outcome_ids": [],
            "predicted": {},
            "actual": {},
            "error_metrics": {
                "n": 0,
                "value_mae": None,
                "value_mape": None,
                "range_coverage": None,
                "disposition_accuracy": None,
                "close_rate_error": None,
            },
            "confidence_calibration": {
                "bins": [],
                "notes": "Synthetic outcome excluded; no production calibration throughput.",
            },
            "causal_caveats": ["synthetic fixture only", "no production throughput"],
            "model_or_process": "none-production",
            "recommended_adjustment": {
                "type": "none",
                "detail": "Do not adjust models on synthetic outcomes.",
                "applies_to": None,
                "status": "draft",
            },
            "forbidden_overfitting_rule": {
                "rule_id": "NO_FIT_LT_30",
                "text": (
                    "Do not change scoring weights, valuation knobs, or automated "
                    "policies on fewer than 30 non-test, non-synthetic, human-graded "
                    "eligible outcomes in the same process_version. Holdout required "
                    "before production weight change. One-case luck is not signal."
                ),
                "min_samples_for_claim": 30,
                "min_samples_for_weight_change": 100,
                "holdout_required": True,
                "auto_apply_forbidden": True,
            },
            "next_sample_threshold": 30,
            "human_acceptance": {
                "status": "pending",
                "actor_role": None,
                "accepted_at": None,
                "notes": "No production calibration claim to accept.",
            },
        }
    )


def _sealed_auxiliary(schema: str, body: dict[str, Any]) -> dict[str, Any]:
    return seal_content({"schema": schema, "run_id": CASE_ID, **body})


def _resolved_code_pins(
    code_pins: Mapping[str, str] | None,
) -> dict[str, str]:
    default_landos = str(_underwriting_input()["source_sha"])
    resolved = {
        "crm": CRM_SOURCE_SHA,
        "landos": default_landos,
        "aegis": AEGIS_SOURCE_SHA,
    }
    if code_pins is not None:
        if set(code_pins) != set(resolved):
            raise ContractViolation(
                "code_pins must contain exactly crm, landos, and aegis"
            )
        resolved.update({key: str(value) for key, value in code_pins.items()})
    for owner in ("crm", "landos"):
        if not FULL_GIT_SHA.fullmatch(resolved[owner]):
            raise ContractViolation(f"{owner} code pin must be a full Git SHA")
    return resolved


def build_synthetic_one_case_proof(
    *, code_pins: Mapping[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    """Build and validate a deterministic, PII-free, no-write proof in memory."""

    resolved_pins = _resolved_code_pins(code_pins)
    evidence = _evidence(resolved_pins["landos"])
    underwriting = _produce_underwriting(resolved_pins["landos"])
    action = _action(underwriting, evidence)
    action_receipt = _sealed_auxiliary(
        "neuruh.action_receipt.synthetic.v1",
        {
            "action": "no_action",
            "proposal_id": action["proposal_id"],
            "executed_at": "2026-07-30T21:08:00Z",
            "external_action_performed": False,
            "production_write_performed": False,
            "human_gate_scope": "synthetic machinery approval only",
        },
    )
    outcome = _outcome(action, evidence)
    calibration = _calibration()
    signal = _sealed_auxiliary(
        "neuruh.synthetic_signal.v1",
        {"signal_id": CASE_ID, "is_synthetic": True, "contains_pii": False},
    )
    score = _sealed_auxiliary(
        "neuruh.synthetic_candidate_score.v1",
        {
            "score_id": f"{CASE_ID}-score",
            "score": 0,
            "is_synthetic": True,
            "production_claim": False,
        },
    )

    refs = {
        "SIGNAL": [
            {"type": "synthetic_signal", "id": signal["signal_id"], "sha256": signal["content_sha256"]}
        ],
        "CANDIDATE_SCORE": [
            {
                "type": "synthetic_candidate_score",
                "id": score["score_id"],
                "sha256": score["content_sha256"],
            }
        ],
        "CRM_LEAD": [],
        "EVIDENCE": [
            {
                "type": "evidence_item",
                "id": item["evidence_id"],
                "sha256": item["content_sha256"],
            }
            for item in evidence
        ],
        "UNDERWRITING": [
            {
                "type": "underwriting_result_v1",
                "id": underwriting["underwriting_id"],
                "sha256": underwriting["content_sha256"],
            }
        ],
        "ACTION_PROPOSAL": [
            {
                "type": "action_proposal_v1",
                "id": action["proposal_id"],
                "sha256": action["content_sha256"],
            }
        ],
        "ACTION_RECEIPT": [
            {
                "type": "synthetic_action_receipt",
                "id": action["proposal_id"],
                "sha256": action_receipt["content_sha256"],
            }
        ],
        "OUTCOME": [
            {
                "type": "outcome_record_v1",
                "id": outcome["outcome_id"],
                "sha256": outcome["content_sha256"],
            }
        ],
        "CALIBRATION_LINK": [
            {
                "type": "calibration_record_v1",
                "id": calibration["calibration_id"],
                "sha256": calibration["content_sha256"],
            }
        ],
    }
    authorities = {
        "SIGNAL": "landos-engine",
        "CANDIDATE_SCORE": "landos-engine",
        "CRM_LEAD": "crm",
        "EVIDENCE": "landos-engine",
        "UNDERWRITING": "landos-engine",
        "ACTION_PROPOSAL": "human",
        "ACTION_RECEIPT": "human",
        "OUTCOME": "human",
        "CALIBRATION_LINK": "vault",
    }
    notes = {
        "SIGNAL": ["synthetic"],
        "CANDIDATE_SCORE": ["synthetic; no mint requested or performed"],
        "CRM_LEAD": ["synthetic; no CRM lead and no CRM write"],
        "EVIDENCE": ["three synthetic EvidenceItemV1 artifacts"],
        "UNDERWRITING": ["human approval is synthetic machinery approval only"],
        "ACTION_PROPOSAL": ["no_action; no offer or contract"],
        "ACTION_RECEIPT": ["no external action"],
        "OUTCOME": ["is_synthetic=true; calibration_eligible=false"],
        "CALIBRATION_LINK": ["n_samples=0; synthetic outcome excluded"],
    }
    sections = {
        name: {
            "present": bool(refs[name]),
            "artifact_refs": refs[name],
            "authority": authorities[name],
            "notes": notes[name],
        }
        for name in SECTION_ORDER
    }
    hashes = {name: section_hash(sections[name]) for name in SECTION_ORDER}
    manifest = {
        "schema": "neuruh.one_case_proof_manifest.v1",
        "manifest_id": "66666666-6666-4666-8666-666666666601",
        "case_id": CASE_ID,
        "case_class": "synthetic",
        "created_at": "2026-07-30T21:10:00Z",
        "entity": {**ENTITY, "run_id": CASE_ID},
        "sections": sections,
        "section_hashes": hashes,
        "chain_sha256": manifest_chain_hash(hashes),
        "authority": {
            "operator_role": "operator",
            "operator_id_sha256": OPERATOR_SHA,
            "code_pins": resolved_pins,
        },
        "gate_results": [
            {"gate": "pii_safe", "status": "pass", "evidence_sha256": None},
            {"gate": "no_auto_mint", "status": "pass", "evidence_sha256": None},
            {
                "gate": "synthetic_not_calibration_eligible",
                "status": "pass",
                "evidence_sha256": outcome["content_sha256"],
            },
            {
                "gate": "human_underwriting_approval_synthetic_only",
                "status": "pass",
                "evidence_sha256": underwriting["content_sha256"],
            },
            {
                "gate": "human_proposal_approval_synthetic_only",
                "status": "pass",
                "evidence_sha256": action["content_sha256"],
            },
            {
                "gate": "offer_and_contract_not_authorized",
                "status": "pass",
                "evidence_sha256": action_receipt["content_sha256"],
            },
        ],
        "verdict": "PASS",
    }
    bundle = {
        "synthetic-signal.json": signal,
        "synthetic-candidate-score.json": score,
        "evidence-01-gross-acres.json": evidence[0],
        "evidence-02-comps.json": evidence[1],
        "evidence-03-road-frontage-unknown.json": evidence[2],
        "underwriting-result-v1.json": underwriting,
        "action-proposal-v1.json": action,
        "action-receipt.json": action_receipt,
        "outcome-record-v1.json": outcome,
        "calibration-record-v1.json": calibration,
        "one-case-proof-manifest-v1.json": manifest,
    }
    artifact_index = [
        {
            "path": path,
            "schema": value["schema"],
            "content_sha256": value.get("content_sha256"),
        }
        for path, value in bundle.items()
    ]
    index = _sealed_auxiliary(
        "neuruh.one_case_proof_bundle_index.v1",
        {
            "artifacts": artifact_index,
            "manifest_chain_sha256": manifest["chain_sha256"],
            "production_writes_performed": False,
            "external_actions_performed": False,
        },
    )
    bundle["bundle-index.json"] = index
    verify_bundle(bundle)
    return bundle


def verify_bundle(bundle: dict[str, dict[str, Any]]) -> None:
    required = {
        "underwriting-result-v1.json",
        "action-proposal-v1.json",
        "outcome-record-v1.json",
        "calibration-record-v1.json",
        "one-case-proof-manifest-v1.json",
        "bundle-index.json",
    }
    if missing := required - set(bundle):
        raise ContractViolation(f"bundle missing files: {sorted(missing)}")
    for path, artifact in bundle.items():
        if path == "one-case-proof-manifest-v1.json":
            validate_artifact(artifact)
        elif artifact["schema"] in {
            "neuruh.evidence_item.v1",
            "neuruh.underwriting_result.v1",
            "neuruh.action_proposal.v1",
            "neuruh.outcome_record.v1",
            "neuruh.calibration_record.v1",
        }:
            validate_artifact(artifact)
        elif not verify_content_hash(artifact):
            raise ContractViolation(f"{path}: content_sha256 mismatch")
    manifest = bundle["one-case-proof-manifest-v1.json"]
    validate_manifest_chain(manifest)
    if manifest["case_id"] != CASE_ID:
        raise ContractViolation("manifest case_id does not match run ID")
    index = bundle["bundle-index.json"]
    if index.get("run_id") != CASE_ID:
        raise ContractViolation("bundle index run_id mismatch")
    for entry in index["artifacts"]:
        artifact = bundle.get(entry["path"])
        if artifact is None:
            raise ContractViolation(f"index references missing {entry['path']}")
        if entry["content_sha256"] != artifact.get("content_sha256"):
            raise ContractViolation(f"index hash mismatch for {entry['path']}")

    expected_refs = {
        (
            "synthetic_signal",
            bundle["synthetic-signal.json"]["signal_id"],
            bundle["synthetic-signal.json"]["content_sha256"],
        ),
        (
            "synthetic_candidate_score",
            bundle["synthetic-candidate-score.json"]["score_id"],
            bundle["synthetic-candidate-score.json"]["content_sha256"],
        ),
        *{
            ("evidence_item", item["evidence_id"], item["content_sha256"])
            for path, item in bundle.items()
            if path.startswith("evidence-")
        },
        (
            "underwriting_result_v1",
            bundle["underwriting-result-v1.json"]["underwriting_id"],
            bundle["underwriting-result-v1.json"]["content_sha256"],
        ),
        (
            "action_proposal_v1",
            bundle["action-proposal-v1.json"]["proposal_id"],
            bundle["action-proposal-v1.json"]["content_sha256"],
        ),
        (
            "synthetic_action_receipt",
            bundle["action-receipt.json"]["proposal_id"],
            bundle["action-receipt.json"]["content_sha256"],
        ),
        (
            "outcome_record_v1",
            bundle["outcome-record-v1.json"]["outcome_id"],
            bundle["outcome-record-v1.json"]["content_sha256"],
        ),
        (
            "calibration_record_v1",
            bundle["calibration-record-v1.json"]["calibration_id"],
            bundle["calibration-record-v1.json"]["content_sha256"],
        ),
    }
    manifest_refs = {
        (ref["type"], ref["id"], ref["sha256"])
        for name in SECTION_ORDER
        for ref in manifest["sections"][name]["artifact_refs"]
    }
    if manifest_refs != expected_refs:
        raise ContractViolation("manifest artifact_refs do not match sealed bundle artifacts")

    if bundle["outcome-record-v1.json"]["human_grading"]["calibration_eligible"]:
        raise ContractViolation("synthetic outcome entered calibration")
    if bundle["calibration-record-v1.json"]["n_samples"] != 0:
        raise ContractViolation("synthetic calibration must have n_samples=0")
    if bundle["action-proposal-v1.json"]["proposed_action"] != "no_action":
        raise ContractViolation("synthetic proof may not authorize an action")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(value))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def write_synthetic_one_case_proof(
    output_dir: str | Path,
    *,
    overwrite: bool = False,
    code_pins: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    output = Path(output_dir)
    bundle = build_synthetic_one_case_proof(code_pins=code_pins)
    if output.exists() and any(output.iterdir()) and not overwrite:
        raise FileExistsError(f"refusing to overwrite non-empty directory: {output}")
    output.mkdir(parents=True, exist_ok=True)
    for path, artifact in bundle.items():
        _atomic_json(output / path, artifact)
    loaded = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(output.glob("*.json"))
    }
    verify_bundle(loaded)
    return loaded

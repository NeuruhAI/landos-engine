"""Offline BLACK V1 contract and semantic acceptance helpers.

The normative JSON Schemas and fixtures live beside this module.  This module is
deliberately dependency-free: it implements only the Draft 2020-12 keywords used
by those six court schemas, then layers the court's cross-artifact invariants on
top.  It performs no network, database, or production writes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


BLACK_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = BLACK_DIR / "schemas"
FIXTURE_PATH = BLACK_DIR / "tests" / "acceptance-cases.json"
SOURCE_MANIFEST_PATH = BLACK_DIR / "SOURCE_MANIFEST.json"

SECTION_ORDER = (
    "SIGNAL",
    "CANDIDATE_SCORE",
    "CRM_LEAD",
    "EVIDENCE",
    "UNDERWRITING",
    "ACTION_PROPOSAL",
    "ACTION_RECEIPT",
    "OUTCOME",
    "CALIBRATION_LINK",
)

IRREVERSIBLE_ACTIONS = frozenset(
    {
        "dial_owner",
        "sms_owner",
        "email_owner",
        "mail_offer",
        "set_offer_price",
        "create_contract",
    }
)
UNDERWRITING_GATED_ACTIONS = frozenset({"mail_offer", "set_offer_price", "create_contract"})
HUMAN_ONLY_ACTIONS = frozenset(
    {"mint", "mint_candidate", "set_offer_price", "create_contract"}
)
PII_KEYS = frozenset(
    {
        "address",
        "email",
        "email_address",
        "full_name",
        "owner_name",
        "phone",
        "phone_1",
        "phone_2",
        "phone_number",
        "situs_address",
        "ssn",
        "social_security_number",
    }
)

SCHEMA_FILES = {
    "evidence_item": "evidence-item.v1.json",
    "underwriting_result": "underwriting-result.v1.json",
    "action_proposal": "action-proposal.v1.json",
    "outcome_record": "outcome-record.v1.json",
    "calibration_record": "calibration-record.v1.json",
    "manifest": "one-case-proof-manifest.v1.json",
}


class ContractViolation(ValueError):
    """A BLACK schema or semantic predicate failed closed."""


@dataclass(frozen=True)
class AcceptanceResult:
    acceptance_id: str
    status: str
    detail: str


def load_fixture_package() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def verify_source_manifest() -> None:
    manifest = json.loads(SOURCE_MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("algorithm") != "sha256":
        raise ContractViolation("BLACK source manifest requires sha256")
    files = manifest.get("files")
    if not isinstance(files, Mapping) or len(files) != 7:
        raise ContractViolation("BLACK source manifest must pin exactly 7 files")
    for relative_path, expected in files.items():
        candidate = (BLACK_DIR / relative_path).resolve()
        if BLACK_DIR.resolve() not in candidate.parents:
            raise ContractViolation(f"BLACK source path escapes bundle: {relative_path}")
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if actual != expected:
            raise ContractViolation(f"BLACK source hash mismatch: {relative_path}")


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMA_DIR / SCHEMA_FILES[name]).read_text(encoding="utf-8"))


def canonical_json_bytes(value: Any) -> bytes:
    """Match the established LandOS receipt canonicalization idiom."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def seal_content(document: Mapping[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(dict(document))
    sealed.pop("content_sha256", None)
    sealed["content_sha256"] = sha256_json(sealed)
    return sealed


def verify_content_sha256(document: Mapping[str, Any]) -> bool:
    expected = document.get("content_sha256")
    if not isinstance(expected, str):
        return False
    body = copy.deepcopy(dict(document))
    body.pop("content_sha256", None)
    return expected == sha256_json(body)


def _resolve_ref(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise ContractViolation(f"external schema reference is not allowed offline: {ref}")
    current: Any = root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        current = current[token]
    if not isinstance(current, Mapping):
        raise ContractViolation(f"schema reference does not resolve to an object: {ref}")
    return current


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    raise ContractViolation(f"unsupported schema type: {expected}")


def _validate_format(value: str, format_name: str, path: str) -> None:
    if format_name == "uuid":
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError) as exc:
            raise ContractViolation(f"{path}: invalid UUID") from exc
    elif format_name == "date-time":
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ContractViolation(f"{path}: invalid date-time") from exc
        if parsed.tzinfo is None:
            raise ContractViolation(f"{path}: date-time must include a timezone")


def validate_schema_document(
    document: Any,
    schema: Mapping[str, Any],
    *,
    root_schema: Mapping[str, Any] | None = None,
    path: str = "$",
) -> None:
    """Validate keywords used by the six pinned BLACK schemas.

    This is intentionally not advertised as a general JSON Schema implementation;
    it covers every assertion keyword present in the byte-pinned court schemas.
    """

    root = root_schema or schema
    if "$ref" in schema:
        validate_schema_document(
            document,
            _resolve_ref(root, str(schema["$ref"])),
            root_schema=root,
            path=path,
        )
        return

    if "const" in schema and document != schema["const"]:
        raise ContractViolation(f"{path}: expected constant {schema['const']!r}")
    if "enum" in schema and document not in schema["enum"]:
        raise ContractViolation(f"{path}: value is outside the allowed enum")

    expected_types = schema.get("type")
    if expected_types is not None:
        choices = [expected_types] if isinstance(expected_types, str) else expected_types
        if not any(_type_matches(document, choice) for choice in choices):
            raise ContractViolation(f"{path}: expected type {choices!r}")

    if isinstance(document, Mapping):
        required = schema.get("required", [])
        missing = [name for name in required if name not in document]
        if missing:
            raise ContractViolation(f"{path}: missing required fields {missing!r}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = sorted(set(document) - set(properties))
            if extra:
                raise ContractViolation(f"{path}: additional fields forbidden {extra!r}")
        for name, value in document.items():
            child_schema = properties.get(name)
            if child_schema is not None:
                validate_schema_document(
                    value,
                    child_schema,
                    root_schema=root,
                    path=f"{path}.{name}",
                )
            elif isinstance(schema.get("additionalProperties"), Mapping):
                validate_schema_document(
                    value,
                    schema["additionalProperties"],
                    root_schema=root,
                    path=f"{path}.{name}",
                )

    if isinstance(document, list):
        if "minItems" in schema and len(document) < schema["minItems"]:
            raise ContractViolation(f"{path}: too few array items")
        if "maxItems" in schema and len(document) > schema["maxItems"]:
            raise ContractViolation(f"{path}: too many array items")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, value in enumerate(document):
                validate_schema_document(
                    value,
                    item_schema,
                    root_schema=root,
                    path=f"{path}[{index}]",
                )

    if isinstance(document, str):
        if "minLength" in schema and len(document) < schema["minLength"]:
            raise ContractViolation(f"{path}: string is too short")
        if "pattern" in schema and re.search(schema["pattern"], document) is None:
            raise ContractViolation(f"{path}: string does not match required pattern")
        if "format" in schema:
            _validate_format(document, schema["format"], path)

    if isinstance(document, (int, float)) and not isinstance(document, bool):
        if "minimum" in schema and document < schema["minimum"]:
            raise ContractViolation(f"{path}: value is below minimum")
        if "maximum" in schema and document > schema["maximum"]:
            raise ContractViolation(f"{path}: value exceeds maximum")


def validate_contract(name: str, document: Mapping[str, Any]) -> None:
    validate_schema_document(document, load_schema(name))


def find_forbidden_pii_keys(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            child_path = f"{path}.{key}"
            if normalized in PII_KEYS:
                findings.append(child_path)
            findings.extend(find_forbidden_pii_keys(child, child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(find_forbidden_pii_keys(child, f"{path}[{index}]"))
    return findings


def validate_evidence_semantics(evidence: Mapping[str, Any]) -> None:
    validate_contract("evidence_item", evidence)
    findings = find_forbidden_pii_keys(evidence.get("entity", {}), "$.entity")
    if findings:
        raise ContractViolation(f"raw PII keys are forbidden: {findings!r}")


def validate_v2_projection(projection: Mapping[str, Any]) -> None:
    """Validate the fail-closed CRM v2 projection envelope available to BLACK."""

    expected = {"schema", "generated_at", "zoning", "constraints", "comps"}
    missing = sorted(expected - set(projection))
    if missing:
        raise ContractViolation(f"v2_projection missing fields {missing!r}")
    if projection.get("schema") != "underwriting_result.v2":
        raise ContractViolation("v2_projection has the wrong schema tag")
    _validate_format(str(projection["generated_at"]), "date-time", "$.generated_at")
    for key in ("zoning", "constraints", "comps"):
        if not isinstance(projection[key], Mapping):
            raise ContractViolation(f"v2_projection.{key} must be an object")


def validate_underwriting_semantics(underwriting: Mapping[str, Any]) -> None:
    validate_contract("underwriting_result", underwriting)
    comps = underwriting["comps"]
    valuation = underwriting["valuation"]
    comps_n = comps.get("comps_n")
    has_comps = isinstance(comps_n, int) and comps_n > 0
    if not has_comps:
        if valuation.get("point_value") is not None:
            raise ContractViolation("no comps requires a null valuation point")
        value_range = valuation.get("range")
        if value_range not in (None, [None, None]):
            raise ContractViolation("no comps requires a null valuation range")
        if not valuation.get("reason_if_null"):
            raise ContractViolation("null valuation requires reason_if_null")
    projection = underwriting.get("v2_projection")
    if projection is not None:
        validate_v2_projection(projection)


def underwriting_approved(underwriting: Mapping[str, Any] | None) -> bool:
    if not underwriting:
        return False
    approval = underwriting.get("human_approval")
    return bool(
        isinstance(approval, Mapping)
        and approval.get("required") is True
        and approval.get("status") == "approved"
        and approval.get("actor_role")
        and approval.get("actor_id_sha256")
        and approval.get("approved_at")
    )


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ContractViolation("date-time must include a timezone")
    return parsed


def validate_action_semantics(
    proposal: Mapping[str, Any],
    *,
    underwriting: Mapping[str, Any] | None = None,
    execution_at: datetime | None = None,
) -> None:
    validate_contract("action_proposal", proposal)
    action = proposal["proposed_action"]
    approval = proposal["approval"]
    expiry = parse_datetime(proposal["expiry"])
    created_at = parse_datetime(proposal["created_at"])
    if expiry <= created_at:
        raise ContractViolation("proposal expiry must be later than created_at")
    if execution_at is not None:
        normalized_execution = execution_at
        if normalized_execution.tzinfo is None:
            normalized_execution = normalized_execution.replace(tzinfo=timezone.utc)
        if normalized_execution >= expiry:
            raise ContractViolation("expired proposal cannot execute")
    if action in IRREVERSIBLE_ACTIONS:
        if approval.get("status") != "approved":
            raise ContractViolation("irreversible action requires human approval")
        if not all(
            approval.get(field)
            for field in ("actor_role", "actor_id_sha256", "approved_at")
        ):
            raise ContractViolation("approved irreversible action requires human authority")
    if action in UNDERWRITING_GATED_ACTIONS:
        if not underwriting_approved(underwriting):
            raise ContractViolation("offer/contract action requires approved underwriting")
        links = proposal.get("receipt_links", {})
        if not links.get("underwriting_id") or not links.get("underwriting_sha256"):
            raise ContractViolation("offer/contract action requires underwriting receipt links")
        prerequisites = set(proposal.get("prerequisites", []))
        if "underwriting_human_approved" not in prerequisites:
            raise ContractViolation("offer/contract action must list underwriting approval gate")


def validate_outcome_semantics(
    outcome: Mapping[str, Any],
    *,
    proposed_action: str | None = None,
) -> None:
    validate_contract("outcome_record", outcome)
    grading = outcome["human_grading"]
    if (outcome["is_test"] or outcome["is_synthetic"]) and grading[
        "calibration_eligible"
    ]:
        raise ContractViolation("test/synthetic outcome is not calibration eligible")
    if grading["calibration_eligible"]:
        if grading["grade"] in {"invalid", "unknown"}:
            raise ContractViolation("invalid/unknown grade is not calibration eligible")
        if not outcome.get("forecast_version"):
            raise ContractViolation("eligible outcome requires forecast_version")
        if not grading.get("grader_role") or not grading.get("graded_at"):
            raise ContractViolation("eligible outcome requires a human grade receipt")
    if proposed_action is not None and outcome["action_taken"] != proposed_action:
        if not outcome["deviations"]:
            raise ContractViolation("action deviation must be explicitly listed")


def eligible_outcome_ids(outcomes: Iterable[Mapping[str, Any]]) -> list[str]:
    eligible: list[str] = []
    for outcome in outcomes:
        validate_outcome_semantics(outcome)
        if outcome["human_grading"]["calibration_eligible"]:
            eligible.append(str(outcome["outcome_id"]))
    return eligible


def validate_calibration_semantics(
    calibration: Mapping[str, Any],
    *,
    eligible_ids: Sequence[str] | None = None,
) -> None:
    validate_contract("calibration_record", calibration)
    rules = calibration["forbidden_overfitting_rule"]
    if (
        rules["min_samples_for_claim"] != 30
        or rules["min_samples_for_weight_change"] != 100
        or rules["holdout_required"] is not True
        or rules["auto_apply_forbidden"] is not True
    ):
        raise ContractViolation("BLACK overfitting thresholds are immutable")
    if calibration["error_metrics"]["n"] != calibration["n_samples"]:
        raise ContractViolation("error metric count must equal n_samples")
    listed = list(calibration.get("outcome_ids", []))
    if len(listed) != calibration["n_samples"]:
        raise ContractViolation("n_samples must equal listed outcome_ids")
    if eligible_ids is not None and listed != list(eligible_ids):
        raise ContractViolation("calibration samples must equal eligible outcome IDs")
    adjustment = calibration["recommended_adjustment"]
    n_samples = calibration["n_samples"]
    if n_samples < 30 and adjustment["type"] not in {"none", "monitor"}:
        raise ContractViolation("fewer than 30 samples permits no model claim")
    if n_samples < 100 and (
        adjustment["type"] in {"human_review_weight", "config_knob"}
        and adjustment["status"] == "accepted"
    ):
        raise ContractViolation("fewer than 100 samples cannot accept a weight/config change")
    if n_samples < 30 and calibration["human_acceptance"]["status"] == "accepted":
        raise ContractViolation("anecdotal tier cannot receive accepted model claims")


def manifest_section_hashes(sections: Mapping[str, Any]) -> dict[str, str]:
    if tuple(sections) != SECTION_ORDER:
        raise ContractViolation("manifest sections are not in fixed BLACK order")
    return {name: sha256_json(sections[name]) for name in SECTION_ORDER}


def manifest_chain_sha256(section_hashes: Mapping[str, str]) -> str:
    if tuple(section_hashes) != SECTION_ORDER:
        raise ContractViolation("manifest section hashes are not in fixed BLACK order")
    return sha256_json(
        {
            "algo": "sha256",
            "order": list(SECTION_ORDER),
            "section_hashes": dict(section_hashes),
        }
    )


def seal_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    sealed = copy.deepcopy(dict(manifest))
    sealed["section_hashes"] = manifest_section_hashes(sealed["sections"])
    sealed["chain_sha256"] = manifest_chain_sha256(sealed["section_hashes"])
    return sealed


def verify_manifest_chain(manifest: Mapping[str, Any]) -> bool:
    try:
        validate_contract("manifest", manifest)
        expected_sections = manifest_section_hashes(manifest["sections"])
        if dict(manifest["section_hashes"]) != expected_sections:
            return False
        return manifest["chain_sha256"] == manifest_chain_sha256(expected_sections)
    except (ContractViolation, KeyError, TypeError):
        return False


def _pass(acceptance_id: str, detail: str) -> AcceptanceResult:
    return AcceptanceResult(acceptance_id, "PASS", detail)


def _fail(acceptance_id: str, exc: BaseException) -> AcceptanceResult:
    return AcceptanceResult(acceptance_id, "FAIL", str(exc))


def _assert_raises(callable_: Any, detail: str) -> None:
    try:
        callable_()
    except ContractViolation:
        return
    raise ContractViolation(detail)


def run_acceptance_cases() -> list[AcceptanceResult]:
    """Run every BLACK offline predicate with local fixtures and mutations."""

    verify_source_manifest()
    package = load_fixture_package()
    cases = {case["id"]: case for case in package["cases"]}
    synthetic = cases["SYN-BLACK-001"]
    negative = cases["NEGATIVE-MIX-SYNTHETIC-INTO-CALIBRATION"]
    results: list[AcceptanceResult] = []

    checks: list[tuple[str, Any, str]] = []

    checks.append(
        (
            "AT-EV-01",
            lambda: [
                validate_contract("evidence_item", item)
                for item in synthetic["evidence_items"]
            ],
            "3 EvidenceItemV1 fixtures parsed",
        )
    )

    def check_evidence_hash() -> None:
        sealed = seal_content(synthetic["evidence_items"][0])
        if not verify_content_sha256(sealed):
            raise ContractViolation("recomputed evidence content hash did not verify")
        sealed["value"] = 9
        if verify_content_sha256(sealed):
            raise ContractViolation("evidence tamper was not detected")

    checks.append(("AT-EV-02", check_evidence_hash, "seal recompute and tamper detection"))

    def check_no_pii() -> None:
        for item in synthetic["evidence_items"]:
            validate_evidence_semantics(item)
        poisoned = copy.deepcopy(synthetic["evidence_items"][0])
        poisoned["entity"]["phone"] = "555-0100"
        _assert_raises(
            lambda: validate_evidence_semantics(poisoned),
            "raw phone field was accepted",
        )

    checks.append(("AT-EV-03", check_no_pii, "entity PII-key rejection"))
    checks.append(
        (
            "AT-UW-01",
            lambda: validate_underwriting_semantics(synthetic["underwriting_result"]),
            "UnderwritingResultV1 parsed",
        )
    )

    def check_no_comps_honesty() -> None:
        no_comps = copy.deepcopy(synthetic["underwriting_result"])
        no_comps["comps"].update(
            {
                "fmv_low": None,
                "fmv_estimate": None,
                "fmv_high": None,
                "comps_n": 0,
                "confidence": "none",
                "items": [],
            }
        )
        no_comps["valuation"].update(
            {
                "point_value": None,
                "range": None,
                "base_ppa": None,
                "reason_if_null": "no comparable sales",
            }
        )
        validate_underwriting_semantics(no_comps)
        no_comps["valuation"]["point_value"] = 1
        _assert_raises(
            lambda: validate_underwriting_semantics(no_comps),
            "invented no-comps valuation was accepted",
        )

    checks.append(("AT-UW-02", check_no_comps_honesty, "honest null with no comps"))
    checks.append(
        (
            "AT-UW-03",
            lambda: validate_v2_projection(
                synthetic["underwriting_result"]["v2_projection"]
            ),
            "v2 projection fail-closed envelope parsed",
        )
    )

    def check_underwriting_gate() -> None:
        proposal = copy.deepcopy(synthetic["action_proposal"])
        proposal["proposed_action"] = "create_contract"
        proposal["reversibility"] = "irreversible"
        proposal["prerequisites"] = ["underwriting_human_approved"]
        validate_action_semantics(
            proposal, underwriting=synthetic["underwriting_result"]
        )
        pending = copy.deepcopy(synthetic["underwriting_result"])
        pending["human_approval"]["status"] = "pending"
        _assert_raises(
            lambda: validate_action_semantics(proposal, underwriting=pending),
            "contract path accepted pending underwriting",
        )

    checks.append(("AT-UW-04", check_underwriting_gate, "offer/contract human gate"))
    checks.append(
        (
            "AT-AP-01",
            lambda: validate_contract("action_proposal", synthetic["action_proposal"]),
            "ActionProposalV1 parsed",
        )
    )

    def check_irreversible_gate() -> None:
        proposal = copy.deepcopy(synthetic["action_proposal"])
        proposal["proposed_action"] = "dial_owner"
        proposal["reversibility"] = "irreversible"
        proposal["approval"]["status"] = "pending"
        _assert_raises(
            lambda: validate_action_semantics(proposal),
            "irreversible action accepted without approval",
        )

    checks.append(("AT-AP-02", check_irreversible_gate, "irreversible human approval"))

    def check_expiry() -> None:
        proposal = synthetic["action_proposal"]
        _assert_raises(
            lambda: validate_action_semantics(
                proposal,
                execution_at=parse_datetime(proposal["expiry"]),
            ),
            "expired proposal was executable",
        )

    checks.append(("AT-AP-03", check_expiry, "expiry enforced at boundary"))
    checks.append(
        (
            "AT-OR-01",
            lambda: validate_contract("outcome_record", synthetic["outcome_record"]),
            "OutcomeRecordV1 parsed",
        )
    )

    def check_synthetic_exclusion() -> None:
        validate_outcome_semantics(synthetic["outcome_record"])
        _assert_raises(
            lambda: validate_outcome_semantics(negative["outcome_record"]),
            "synthetic outcome entered calibration",
        )

    checks.append(("AT-OR-02", check_synthetic_exclusion, "synthetic/test exclusion"))

    def check_deviation() -> None:
        validate_outcome_semantics(
            synthetic["outcome_record"],
            proposed_action=synthetic["action_proposal"]["proposed_action"],
        )
        deviated = copy.deepcopy(synthetic["outcome_record"])
        deviated["action_taken"] = "score_rescore"
        _assert_raises(
            lambda: validate_outcome_semantics(
                deviated, proposed_action="no_action"
            ),
            "unlisted action deviation was accepted",
        )

    checks.append(("AT-OR-03", check_deviation, "proposal deviation must be listed"))
    checks.append(
        (
            "AT-CR-01",
            lambda: validate_calibration_semantics(
                synthetic["calibration_record"], eligible_ids=[]
            ),
            "CalibrationRecordV1 parsed with eligible-only sample set",
        )
    )

    def check_weight_threshold() -> None:
        calibration = copy.deepcopy(synthetic["calibration_record"])
        calibration["recommended_adjustment"].update(
            {"type": "config_knob", "status": "accepted"}
        )
        _assert_raises(
            lambda: validate_calibration_semantics(calibration),
            "weight/config change accepted below 100 samples",
        )

    checks.append(("AT-CR-02", check_weight_threshold, "n>=100 weight/config threshold"))

    def check_claim_threshold() -> None:
        calibration = copy.deepcopy(synthetic["calibration_record"])
        calibration["recommended_adjustment"]["type"] = "data_quality"
        _assert_raises(
            lambda: validate_calibration_semantics(calibration),
            "model claim accepted below 30 samples",
        )

    checks.append(("AT-CR-03", check_claim_threshold, "n>=30 claim threshold"))

    def check_manifest_order() -> None:
        if tuple(synthetic["manifest"]["sections"]) != SECTION_ORDER:
            raise ContractViolation("fixture sections are not in fixed order")
        reordered = copy.deepcopy(synthetic["manifest"])
        keys = list(reordered["sections"])
        reordered["sections"] = {
            key: reordered["sections"][key] for key in reversed(keys)
        }
        _assert_raises(
            lambda: manifest_section_hashes(reordered["sections"]),
            "reordered manifest sections were accepted",
        )

    checks.append(("AT-MF-01", check_manifest_order, "fixed 9-section order"))

    sealed_manifest: dict[str, Any] = seal_manifest(synthetic["manifest"])
    checks.append(
        (
            "AT-MF-02",
            lambda: (
                None
                if verify_manifest_chain(sealed_manifest)
                else (_ for _ in ()).throw(
                    ContractViolation("manifest chain did not recompute")
                )
            ),
            "ordered section and chain hashes recomputed",
        )
    )

    def check_manifest_tamper() -> None:
        tampered = copy.deepcopy(sealed_manifest)
        tampered["sections"]["OUTCOME"]["notes"].append("tampered")
        if verify_manifest_chain(tampered):
            raise ContractViolation("manifest body tamper was not detected")

    checks.append(("AT-MF-03", check_manifest_tamper, "section tamper fails chain"))

    checks.append(
        (
            "AT-SS-01",
            lambda: (
                None
                if cases["STATE-UNDERWRITING-ZERO-PAYLOADS"]["observation"][
                    "underwriting_result_populated"
                ]
                == 0
                else (_ for _ in ()).throw(
                    ContractViolation("BLACK zero-payload baseline changed")
                )
            ),
            "pinned BLACK baseline records zero populated payloads",
        )
    )

    def check_stage_audit_scope() -> None:
        observation = cases["STATE-PIPELINE-STAGE-AUDIT-SCOPE"]["observation"]
        if (
            observation["table"] != "public.pipeline_stage_audit"
            or observation["trigger_on"] != "public.pipeline_stages"
            or observation["triggers_on_lead_drag"] is not False
        ):
            raise ContractViolation("pipeline_stage_audit was misclassified")

    checks.append(
        (
            "AT-SS-02",
            check_stage_audit_scope,
            "stage-definition audit is not lead-drag history",
        )
    )

    for acceptance_id, check, detail in checks:
        try:
            check()
        except Exception as exc:  # report every predicate in one offline run
            results.append(_fail(acceptance_id, exc))
        else:
            results.append(_pass(acceptance_id, detail))
    return results


def assert_all_pass(results: Sequence[AcceptanceResult]) -> None:
    failures = [result for result in results if result.status != "PASS"]
    if failures:
        detail = "; ".join(
            f"{failure.acceptance_id}: {failure.detail}" for failure in failures
        )
        raise ContractViolation(detail)

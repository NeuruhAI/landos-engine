"""Normative BLACK underwriting contract, honesty, and file-only producer tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.underwriting import (
    AcquisitionLeadUnderwritingResultAdapter,
    UnderwritingProductionError,
    UnderwritingResultV1,
    content_sha256,
    produce_underwriting_result,
    underwriting_run_context,
    verify_content_hash,
    verify_receipt,
)
from app.underwriting import producer
from app.underwriting.canonical import canonical_json
from app.underwriting.models import UnderwritingCaseV1
from scripts import generate_underwriting_fixture as cli

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "underwriting"
SYNTHETIC = FIXTURES / "synthetic_case_v1.json"
RED_TEAM = FIXTURES / "red_team_failure_v1.json"
EXPECTED = ROOT / "data" / "underwriting-fixtures" / "synthetic_result_v1.json"
BLACK = (
    Path("/Users/jeramiehicks/neuruh-workspace/audit-output")
    / "BLACK-CLOSED-LOOP-COURT-20260730T212633Z"
)
BLACK_CASES = BLACK / "tests" / "acceptance-cases.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _pending_no_comps_case() -> dict:
    raw = _load(RED_TEAM)
    raw["human_approval"] = {
        "required": True,
        "status": "pending",
        "actor_role": None,
        "actor_id_sha256": None,
        "approved_at": None,
        "notes": "No offer, mint, or contract authority.",
    }
    return raw


def test_syn_black_001_is_deterministic_and_matches_checked_example():
    first = produce_underwriting_result(_load(SYNTHETIC))
    second = produce_underwriting_result(_load(SYNTHETIC))
    first_json = canonical_json(first.model_dump(mode="json", by_alias=True))
    assert first_json == canonical_json(
        second.model_dump(mode="json", by_alias=True)
    )
    assert first_json == EXPECTED.read_text(encoding="utf-8").rstrip("\n")
    assert first.content_sha256 == (
        "8a3d7b8e3b50ee051029618fdadd8dc66986a98eb1fa5c1e452f0ea9516698e8"
    )


def test_output_is_exact_normative_black_envelope_and_run_id_is_external():
    result = produce_underwriting_result(_load(SYNTHETIC))
    payload = result.model_dump(mode="json", by_alias=True)
    assert set(payload) == {
        "schema",
        "underwriting_id",
        "generated_at",
        "method_version",
        "entity",
        "evidence_ids",
        "zoning",
        "constraints",
        "comps",
        "valuation",
        "assumptions",
        "ranges",
        "scenarios",
        "risk",
        "confidence",
        "counter_analysis",
        "human_approval",
        "v2_projection",
        "content_sha256",
    }
    assert payload["schema"] == "neuruh.underwriting_result.v1"
    assert "run_id" not in payload
    context = underwriting_run_context(_load(SYNTHETIC), result)
    assert context.run_id == "orange-run-syn-black-001"
    assert context.case_id == "SYN-BLACK-001"
    assert context.calibration_eligible is False
    assert context.underwriting_sha256 == result.content_sha256


def test_black_court_acceptance_fixture_parses_but_placeholder_hash_is_not_a_receipt():
    court = _load(BLACK_CASES)
    syn = next(case for case in court["cases"] if case["id"] == "SYN-BLACK-001")
    parsed = UnderwritingResultV1.model_validate(syn["underwriting_result"])
    assert parsed.schema_ == "neuruh.underwriting_result.v1"
    assert parsed.comps.comps_n == 3
    assert parsed.comps.items == []
    assert not verify_receipt(parsed)


def test_v2_projection_has_exact_preparation_only_shape():
    result = produce_underwriting_result(_load(SYNTHETIC))
    projected = result.v2_projection.model_dump(mode="json", by_alias=True)
    assert set(projected) == {
        "schema",
        "generated_at",
        "zoning",
        "constraints",
        "comps",
    }
    assert projected["schema"] == "underwriting_result.v2"
    assert projected["zoning"] == result.zoning.model_dump(mode="json")
    assert projected["constraints"] == result.constraints.model_dump(mode="json")
    assert projected["comps"] == result.comps.model_dump(mode="json")


def test_producer_delegates_to_existing_valuation_and_insight_modules(monkeypatch):
    calls = {"land": 0, "mao": 0, "insight": 0}
    original_land = producer.valuation.land_value
    original_mao = producer.valuation.compute_mao
    original_compose = producer.compose.compose

    def land(*args, **kwargs):
        calls["land"] += 1
        return original_land(*args, **kwargs)

    def mao(*args, **kwargs):
        calls["mao"] += 1
        return original_mao(*args, **kwargs)

    def insight(*args, **kwargs):
        calls["insight"] += 1
        return original_compose(*args, **kwargs)

    monkeypatch.setattr(producer.valuation, "land_value", land)
    monkeypatch.setattr(producer.valuation, "compute_mao", mao)
    monkeypatch.setattr(producer.compose, "compose", insight)
    produce_underwriting_result(_load(SYNTHETIC))
    assert calls == {"land": 1, "mao": 1, "insight": 1}


def test_content_sha256_excludes_self_and_detects_any_payload_change():
    result = produce_underwriting_result(_load(SYNTHETIC))
    payload = result.model_dump(mode="json", by_alias=True)
    assert verify_receipt(result)
    assert verify_content_hash(payload)
    assert payload["content_sha256"] == content_sha256(payload)
    payload["risk"]["market"] = "tampered"
    assert not verify_content_hash(payload)


def test_red_team_fixture_rejects_non_synthetic_fixture_approval():
    with pytest.raises(
        UnderwritingProductionError,
        match="only an explicitly synthetic case may carry fixture approval",
    ):
        produce_underwriting_result(_load(RED_TEAM))


def test_no_comps_emits_honest_null_and_pending_human_gate():
    result = produce_underwriting_result(_pending_no_comps_case())
    assert result.comps.comps_n == 0
    assert result.valuation.point_value is None
    assert result.valuation.range is None
    assert result.valuation.reason_if_null == (
        "no comparable sales for the subject — value not grounded"
    )
    assert result.valuation.mao["value"] is None
    assert result.confidence == "none"
    assert all(item.offer is None and item.exit is None for item in result.scenarios)
    assert result.human_approval.required is True
    assert result.human_approval.status == "pending"


def test_numeric_claims_link_evidence_or_explicit_method_assumption():
    result = produce_underwriting_result(_load(SYNTHETIC))
    evidence_ids = {str(item) for item in result.evidence_ids}
    assert evidence_ids
    assert all(
        item["evidence_id"] in evidence_ids and item["method"] == "comp_median"
        for item in result.comps.items
    )
    assert all(
        set(item["evidence_ids"]) == evidence_ids
        and item["method"] == "app.valuation.land_value"
        for item in result.valuation.adjustments
    )
    assert all(
        "method=" in note and "evidence_ids=" in note
        for scenario in result.scenarios
        for note in scenario.notes
    )
    assert any("method=app.valuation.compute_mao" in item for item in result.assumptions)


def test_human_gate_defaults_pending_and_explicit_synthetic_approval_is_narrow():
    raw = _load(SYNTHETIC)
    raw.pop("human_approval")
    result = produce_underwriting_result(raw)
    assert result.human_approval.status == "pending"
    assert result.human_approval.required is True
    approved = produce_underwriting_result(_load(SYNTHETIC))
    assert approved.human_approval.status == "approved"
    assert "synthetic" in approved.human_approval.notes
    assert "no offer or contract authority" in approved.human_approval.notes


def test_input_is_fixture_only_and_forbids_write_configuration():
    raw = _load(SYNTHETIC)
    raw["database_url"] = "forbidden"
    with pytest.raises(UnderwritingProductionError, match="database_url"):
        produce_underwriting_result(raw)
    raw = _load(SYNTHETIC)
    raw["fixture_mode"] = False
    with pytest.raises(Exception, match="fixture_mode"):
        UnderwritingCaseV1.model_validate(raw)


def test_adapter_prepares_only_exact_v2_and_has_no_write_api():
    result = produce_underwriting_result(_load(SYNTHETIC))
    adapter = AcquisitionLeadUnderwritingResultAdapter()
    assert not hasattr(adapter, "write")
    assert not hasattr(adapter, "execute")
    assert not hasattr(adapter, "connect")
    with pytest.raises(ValueError, match="no acquisition_lead_id"):
        adapter.prepare(result)

    payload = result.model_dump(mode="json", by_alias=True)
    payload["entity"]["acquisition_lead_id"] = (
        "11111111-1111-4111-8111-111111111111"
    )
    payload["content_sha256"] = content_sha256(payload)
    prepared = adapter.prepare(UnderwritingResultV1.model_validate(payload))
    assert prepared["write_performed"] is False
    assert prepared["preparation_only"] is True
    assert set(prepared["underwriting_result"]) == {
        "schema",
        "generated_at",
        "zoning",
        "constraints",
        "comps",
    }


def test_file_cli_writes_normative_artifact_and_run_context_atomically(
    tmp_path, capsys
):
    output = tmp_path / "result.json"
    context = tmp_path / "context.json"
    assert (
        cli.main(
            [
                "--input",
                str(SYNTHETIC),
                "--output",
                str(output),
                "--context-output",
                str(context),
            ]
        )
        == 0
    )
    parsed = UnderwritingResultV1.model_validate(_load(output))
    assert verify_receipt(parsed)
    assert _load(context)["run_id"] == "orange-run-syn-black-001"
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        cli.main(["--input", str(SYNTHETIC), "--output", str(output)])
    assert "UNDERWRITING_FIXTURE_OK" in capsys.readouterr().err


def test_file_cli_expected_rejection_has_zero_exit(capsys):
    assert cli.main(["--input", str(RED_TEAM), "--expect-failure"]) == 0
    assert "EXPECTED_REJECTION" in capsys.readouterr().err

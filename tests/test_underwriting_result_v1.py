"""UnderwritingResultV1 contract, honesty, receipt, and file-only CLI tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.underwriting import (
    AcquisitionLeadUnderwritingResultAdapter,
    UnderwritingProductionError,
    UnderwritingResultV1,
    produce_underwriting_result,
    verify_receipt,
)
from app.underwriting.canonical import canonical_json
from app.underwriting.models import UnderwritingCaseV1
from app.underwriting import producer
from scripts import generate_underwriting_fixture as cli

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "underwriting"
SYNTHETIC = FIXTURES / "synthetic_case_v1.json"
RED_TEAM = FIXTURES / "red_team_failure_v1.json"
EXPECTED = ROOT / "data" / "underwriting-fixtures" / "synthetic_result_v1.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_synthetic_fixture_is_deterministic_and_matches_checked_example():
    first = produce_underwriting_result(_load(SYNTHETIC))
    second = produce_underwriting_result(_load(SYNTHETIC))
    first_json = canonical_json(first.model_dump(mode="json"))
    second_json = canonical_json(second.model_dump(mode="json"))
    assert first_json == second_json
    assert first_json == EXPECTED.read_text(encoding="utf-8").rstrip("\n")
    assert first.receipt.payload_sha256 == (
        "5bb8e1e6119176f59edb300d6e94bccf64109304e5bc76b65fe16440e930f5a8"
    )


def test_result_has_required_nonproduction_and_human_gates():
    result = produce_underwriting_result(_load(SYNTHETIC))
    assert result.schema_version == "underwriting-result-v1"
    assert result.fixture_mode is True
    assert result.production_write_performed is False
    assert result.human_approval_required is True
    assert result.synthetic_case_id and result.lead_id is None
    assert result.source_sha == "e231f00a9994a2c46afa72b5d6b59991bd89a7ad"
    assert result.market_comp_evidence
    assert result.risk_register
    assert result.zoning_land_constraints
    assert result.red_team_objections
    assert result.missing_evidence
    assert result.arv.value is None and result.arv.unknown_reason
    assert result.rent_range.low is None and result.rent_range.unknown_reason


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


def test_receipt_detects_any_payload_change():
    result = produce_underwriting_result(_load(SYNTHETIC))
    assert verify_receipt(result)
    tampered = result.model_dump(mode="json")
    tampered["decision_rationale"] = "tampered"
    assert not verify_receipt(tampered)


def test_red_team_fixture_fails_closed_without_grounded_comps():
    with pytest.raises(
        UnderwritingProductionError, match="proceed decision requires comparable sales"
    ):
        produce_underwriting_result(_load(RED_TEAM))


def test_unknown_repairs_must_be_named_as_missing_evidence():
    raw = _load(RED_TEAM)
    raw["recommended_decision"] = "hold_for_evidence"
    raw["missing_evidence"] = ["title"]
    with pytest.raises(
        UnderwritingProductionError,
        match="unknown repair range must be named in missing_evidence",
    ):
        produce_underwriting_result(raw)


def test_honest_hold_emits_explicit_unknowns_instead_of_values():
    raw = _load(RED_TEAM)
    raw["recommended_decision"] = "hold_for_evidence"
    result = produce_underwriting_result(raw)
    assert result.valuation_range.low is None
    assert result.valuation_range.unknown_reason
    assert result.offer_mao_logic.basis.value is None
    assert result.offer_mao_logic.basis.unknown_reason
    assert result.offer_mao_logic.repairs.value is None
    assert result.offer_mao_logic.repairs.unknown_reason
    assert result.offer_mao_logic.mao.low is None
    assert result.offer_mao_logic.mao.unknown_reason
    assert result.confidence.level == "none"
    assert result.strategy_scenarios[0].status == "insufficient_evidence"


def test_schema_forbids_undeclared_fields_and_exposes_required_contract():
    schema = UnderwritingResultV1.model_json_schema()
    required = set(schema["required"])
    assert {
        "schema_version",
        "run_id",
        "property_identity",
        "valuation_range",
        "offer_mao_logic",
        "risk_register",
        "human_approval_required",
        "source_sha",
        "generated_at",
        "receipt",
    } <= required
    raw = _load(SYNTHETIC)
    raw["database_url"] = "forbidden"
    with pytest.raises(UnderwritingProductionError, match="database_url"):
        produce_underwriting_result(raw)


def test_future_adapter_only_prepares_payload_and_cannot_write():
    result = produce_underwriting_result(_load(SYNTHETIC))
    adapter = AcquisitionLeadUnderwritingResultAdapter()
    assert not hasattr(adapter, "write")
    assert not hasattr(adapter, "execute")
    with pytest.raises(ValueError, match="synthetic fixture"):
        adapter.prepare(result)

    lead_case = _load(SYNTHETIC)
    lead_case["lead_id"] = "11111111-1111-1111-1111-111111111111"
    lead_case["synthetic_case_id"] = None
    lead_result = produce_underwriting_result(lead_case)
    prepared = adapter.prepare(lead_result)
    assert prepared["lead_id"] == lead_case["lead_id"]
    assert prepared["expected_target"] == (
        "public.acquisition_leads.underwriting_result"
    )
    assert prepared["write_performed"] is False


def test_file_cli_generates_atomically_and_refuses_overwrite(tmp_path, capsys):
    output = tmp_path / "result.json"
    assert cli.main(["--input", str(SYNTHETIC), "--output", str(output)]) == 0
    parsed = UnderwritingResultV1.model_validate(_load(output))
    assert verify_receipt(parsed)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        cli.main(["--input", str(SYNTHETIC), "--output", str(output)])
    assert cli.main(
        [
            "--input",
            str(SYNTHETIC),
            "--output",
            str(output),
            "--overwrite",
        ]
    ) == 0
    assert "UNDERWRITING_FIXTURE_OK" in capsys.readouterr().err


def test_file_cli_expected_rejection_has_zero_exit(capsys):
    code = cli.main(
        ["--input", str(RED_TEAM), "--expect-failure"]
    )
    assert code == 0
    assert "EXPECTED_REJECTION" in capsys.readouterr().err


def test_input_schema_requires_fixture_mode_true():
    raw = _load(SYNTHETIC)
    raw["fixture_mode"] = False
    with pytest.raises(Exception, match="fixture_mode"):
        UnderwritingCaseV1.model_validate(raw)

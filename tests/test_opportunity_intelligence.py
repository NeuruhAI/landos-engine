"""Courts for the pure Recipe Engine -> LandOS intelligence seam."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib
import json
import sys

import pytest

from app.opportunity_intelligence import (
    INPUT_CONTRACT_VERSION,
    PROPERTY_POPULATION_CONTRACT_VERSION,
    RECEIPT_CONTRACT_VERSION,
    RECIPE_CONTRACT_VERSION,
    SCORE_VERSION,
    SOURCE_FACTORY_RUN_RECEIPT_VERSION,
    OpportunityIntelligenceError,
    evaluate_opportunity,
    verify_opportunity_receipt,
)
from app.receipt import section_hash


def source(source_id: str, mode: str = "AUTOMATIC") -> dict:
    return {
        "source_id": source_id,
        "mode": mode,
        "expected_information": f"answer from {source_id}",
    }


def recipe() -> dict:
    body = {
        "schema_version": RECIPE_CONTRACT_VERSION,
        "recipe_id": "test_land_acquisition",
        "version": "1.0.0-test",
        "score_version": SCORE_VERSION,
        "score": {"base": 0, "minimum": 0, "maximum": 100},
        "asset_aliases": {
            "vacant_land": "vacant_land", "single_family": "residential",
            "commercial": "commercial", "unknown": "unknown",
        },
        "signal_rules": [
            {
                "signal_type": "tax_delinquent", "label": "tax delinquency",
                "category": "distress", "points_if_present": 30,
                "points_if_absent": 0, "confidence_weight": 10,
                "material": True, "next_sources": [source("TAX_ROLL")],
            },
            {
                "signal_type": "title_conflict", "label": "title conflict",
                "category": "risk", "points_if_present": -20,
                "points_if_absent": 0, "confidence_weight": 10,
                "material": True, "next_sources": [source("DEED", "MANUAL")],
            },
            {
                "signal_type": "government_owner", "label": "government owner",
                "category": "exclusion", "points_if_present": -100,
                "points_if_absent": 0, "confidence_weight": 10,
                "material": True, "next_sources": [source("PARCEL_ROLL")],
            },
        ],
        "confidence": {
            "formula": "declared weighted coverage",
            "credits": {"HIGH": 1.0, "MEDIUM": 0.75, "LOW": 0.5, "UNKNOWN": 0.0},
            "conflicted_credit": 0.25,
            "readiness_fact_weight": 5,
        },
        "readiness": {
            "authority": ["legal_authority_verified"],
            "contact": ["identity_verified", "contact_point_verified", "contact_authorized"],
            "deal": [
                "identity_verified", "legal_authority_verified", "value_established",
                "underwriting_complete", "risk_review_complete",
            ],
            "offer": [
                "identity_verified", "legal_authority_verified", "value_established",
                "underwriting_complete", "risk_review_complete", "offer_authorized",
            ],
        },
        "action_rules": [
            {"fact": "identity_verified", "when": ["NO", "UNKNOWN"],
             "action": "RESOLVE_IDENTITY", "route": "IDENTITY_RESEARCH"},
            {"fact": "legal_authority_verified", "when": ["NO", "UNKNOWN"],
             "action": "ESTABLISH_LEGAL_AUTHORITY", "route": "AUTHORITY_RESEARCH"},
            {"fact": "value_established", "when": ["NO", "UNKNOWN"],
             "action": "ESTABLISH_VALUE", "route": "UNDERWRITING"},
            {"fact": "underwriting_complete", "when": ["NO", "UNKNOWN"],
             "action": "COMPLETE_UNDERWRITING", "route": "UNDERWRITING"},
            {"fact": "risk_review_complete", "when": ["NO", "UNKNOWN"],
             "action": "COMPLETE_RISK_REVIEW", "route": "DUE_DILIGENCE"},
            {"fact": "contact_point_verified", "when": ["NO", "UNKNOWN"],
             "action": "RESOLVE_CONTACT", "route": "CONTACT_RESEARCH"},
            {"fact": "contact_authorized", "when": ["NO", "UNKNOWN"],
             "action": "VERIFY_CONTACT_AUTHORIZATION", "route": "COMPLIANCE_REVIEW"},
            {"fact": "offer_authorized", "when": ["NO", "UNKNOWN"],
             "action": "REQUEST_OFFER_AUTHORIZATION", "route": "ACQUISITION_REVIEW"},
        ],
        "fact_sources": {
            "identity_verified": [source("PARCEL_ROLL")],
            "legal_authority_verified": [source("PROBATE"), source("DEED", "MANUAL")],
            "value_established": [source("VALUATION")],
            "underwriting_complete": [source("LAND_DD")],
            "risk_review_complete": [source("LAND_DD")],
            "contact_point_verified": [source("CONTACT_ROUTER")],
            "contact_authorized": [source("CONTACT_GATE")],
            "offer_authorized": [source("OFFER_GATE", "MANUAL")],
        },
        "priority_bands": [
            {"min_score": 25, "min_confidence": 50, "priority": "P1"},
            {"min_score": 0, "min_confidence": 0, "priority": "P3_RESEARCH"},
        ],
    }
    body["config_digest"] = section_hash(body)
    return body


def redigest_recipe(body: dict) -> None:
    effective = dict(body)
    effective.pop("config_digest", None)
    body["config_digest"] = section_hash(effective)


def signal(signal_id: str, signal_type: str, state: str, confidence: str = "HIGH") -> dict:
    return {
        "signal_id": signal_id,
        "signal_type": signal_type,
        "state": state,
        "confidence": confidence,
        "evidence_refs": [f"evidence:{signal_id}"],
        "source_families": [f"source:{signal_type}"],
    }


def property_population(signal_count: int) -> dict:
    return {
        "schema_version": PROPERTY_POPULATION_CONTRACT_VERSION,
        "scope": "SUBJECT_SIGNAL_SET",
        "property_identity": {
            "kind": "parcel", "key": "37183|P|APN1",
            "county_fips": "37183", "basis_value": "APN1",
        },
        "signal_observation_count": signal_count,
        "property_count": 1,
        "unresolved_signal_count": 0,
        "signal_amplification": float(signal_count),
        "identity_kinds": {"parcel": signal_count},
        "dedupe_provenance": {
            "identity_method": "county_fips+normalized_parcel_apn_else_raw_address",
            "producer_contract": PROPERTY_POPULATION_CONTRACT_VERSION,
            "producer": "LANDOS_UNIT_FIXTURE",
        },
    }


def sync_property_population(body: dict) -> None:
    count = len(body["signals"])
    body["property_population"]["signal_observation_count"] = count
    body["property_population"]["signal_amplification"] = float(count)
    body["property_population"]["identity_kinds"] = {"parcel": count}


def payload(**overrides) -> dict:
    body = {
        "schema_version": INPUT_CONTRACT_VERSION,
        "as_of": "2026-08-21T00:00:00Z",
        "recipe": recipe(),
        "subject": {
            "subject_id": "subject-1", "county_fips": "37183",
            "parcel_apn": "apn-1",
        },
        "asset": {"declared_class": "vacant_land"},
        "signals": [
            signal("tax-1", "tax_delinquent", "PRESENT"),
            signal("title-1", "title_conflict", "ABSENT"),
            signal("gov-1", "government_owner", "ABSENT"),
        ],
        "property_population": property_population(3),
        "readiness": {
            "identity_verified": "YES", "legal_authority_verified": "YES",
            "contact_point_verified": "YES", "contact_authorized": "YES",
            "value_established": "YES", "underwriting_complete": "YES",
            "risk_review_complete": "YES", "offer_authorized": "UNKNOWN",
        },
        "research_attempts": [],
        "lineage": {
            "evidence_refs": ["evidence:tax-1"], "contact_evidence_refs": [],
            "source_families": ["source:tax"], "relationship_node_refs": [],
            "relationship_edge_refs": [], "quarantined_identity_refs": [],
        },
    }
    body.update(overrides)
    return body


def source_factory_compatibility_payload() -> dict:
    property_key = "37183|P|0609751927"
    observations = []
    for ordinal in range(1, 24):
        observation = signal(
            f"source-factory-wake-{ordinal:02d}",
            "tax_delinquent",
            "PRESENT",
        )
        observation.update({
            "geography": "37183",
            "parcel_apn": "06-097-51927" if ordinal % 2 else "0609751927",
            "property_key": property_key,
            "source_observation_ordinal": ordinal,
        })
        observations.append(observation)
    source_rollup = {
        "landed_signal_count": 14233,
        "property_count": 6052,
        "unresolved_signal_count": 0,
        "signal_amplification": 2.3518,
        "identity_kinds": {"parcel": 14233},
        "identity_basis": {"parcel": 12320, "tax_account": 1913},
        "multiplicity_histogram": {
            "1": 4285, "2": 708, "3": 313, "4": 136, "5": 72,
            "6": 55, "7": 52, "8": 81, "9": 32, "10": 37,
            "11": 70, "12": 16, "13": 18, "14": 16, "15": 9,
            "16": 13, "17": 10, "18": 28, "19": 8, "20": 6,
            "21": 6, "22": 9, "23": 72,
        },
        "duplicates_dropped": 0,
        "quarantined_row_count": 15,
    }
    population = {
        "schema_version": PROPERTY_POPULATION_CONTRACT_VERSION,
        "scope": "SUBJECT_SIGNAL_SET",
        "property_identity": {
            "kind": "parcel", "key": property_key,
            "county_fips": "37183", "basis_value": "0609751927",
        },
        "signal_observation_count": 23,
        "property_count": 1,
        "unresolved_signal_count": 0,
        "signal_amplification": 23.0,
        "identity_kinds": {"parcel": 23},
        "dedupe_provenance": {
            "identity_method": "county_fips+normalized_parcel_apn_else_raw_address",
            "producer_contract": SOURCE_FACTORY_RUN_RECEIPT_VERSION,
            "run_id": "run-20260821T215734Z",
            "factory_version": "1.0.0",
            "producer_commit": "f857bdbeaf4ce5c366e5837beab98f5767e11edf",
            "source_rollup_sha256": section_hash(source_rollup),
            "identity_component": "strategy/source_factory/identity.py",
            "territory_compatibility": {
                "consumer_commit": "b1be5d3afbf2308b565c41c057216ec11e26ddaf",
                "consumer_component": "web/lib/jurisdiction/parcelIdentity.ts",
            },
            "source_rollup": source_rollup,
        },
    }
    return payload(
        subject={
            "subject_id": "source-factory-wake-property",
            "county_fips": "37183",
            "parcel_apn": "06-097-51927",
        },
        signals=observations,
        property_population=population,
    )


def test_named_deltas_produce_score_and_deterministic_receipt():
    p = payload()
    first = evaluate_opportunity(p)
    second = evaluate_opportunity(p)
    assert first == second
    assert first["opportunity_score"]["score"] == 30
    assert first["explanation"]["positive_factors"][0]["factor_id"] == "tax_delinquent"
    assert first["next_best_action"]["action"] == "REQUEST_OFFER_AUTHORIZATION"
    assert first["receipt"]["schema_version"] == RECEIPT_CONTRACT_VERSION
    assert verify_opportunity_receipt(first, p)


def test_mutating_an_explanation_breaks_the_receipt():
    p = payload()
    result = evaluate_opportunity(p)
    result["explanation"]["why"][0] = "different"
    assert not verify_opportunity_receipt(result, p)


def test_receipt_verification_requires_and_binds_the_original_input():
    p = payload()
    result = evaluate_opportunity(p)
    assert verify_opportunity_receipt(result, p)
    assert not verify_opportunity_receipt(result)
    different_input = deepcopy(p)
    different_input["as_of"] = "2026-08-22T00:00:00Z"
    assert not verify_opportunity_receipt(result, different_input)


def test_duplicate_observations_stack_evidence_but_not_points():
    p = payload()
    p["signals"].append(signal("tax-2", "tax_delinquent", "PRESENT", "MEDIUM"))
    sync_property_population(p)
    result = evaluate_opportunity(p)
    factor = next(f for f in result["stacked_signals"]["normalized_factors"]
                  if f["factor_id"] == "tax_delinquent")
    assert result["opportunity_score"]["score"] == 30
    assert factor["signal_ids"] == ["tax-1", "tax-2"]
    assert factor["confidence"] == "MEDIUM", "consistent evidence uses conservative minimum"


def test_source_factory_population_is_lossless_and_never_counted_as_properties():
    p = source_factory_compatibility_payload()
    result = evaluate_opportunity(p)
    population = result["property_population"]
    rollup = population["dedupe_provenance"]["source_rollup"]

    assert result["stacked_signals"]["observation_count"] == 23
    assert result["stacked_signals"]["count_kind"] == "SIGNAL_OBSERVATIONS_NOT_PROPERTIES"
    assert population["signal_observation_count"] == 23
    assert population["property_count"] == 1
    assert rollup["landed_signal_count"] == 14233
    assert rollup["property_count"] == 6052
    assert rollup["landed_signal_count"] != rollup["property_count"]
    assert result["opportunity_score"]["score"] == 30
    assert result["opportunity_genome"]["property_population"] == p["property_population"]
    assert verify_opportunity_receipt(result, p)


def test_population_cardinality_conflation_and_provenance_tamper_fail_closed():
    conflated = source_factory_compatibility_payload()
    conflated["property_population"]["property_count"] = 23
    conflated["property_population"]["signal_amplification"] = 1.0
    with pytest.raises(OpportunityIntelligenceError, match="must be 1 for a parcel"):
        evaluate_opportunity(conflated)

    mismatched_signal_count = source_factory_compatibility_payload()
    mismatched_signal_count["property_population"]["signal_observation_count"] = 14233
    with pytest.raises(OpportunityIntelligenceError, match=r"len\(input.signals\)"):
        evaluate_opportunity(mismatched_signal_count)

    tampered_rollup = source_factory_compatibility_payload()
    tampered_rollup["property_population"]["dedupe_provenance"]["source_rollup"][
        "quarantined_row_count"
    ] = 16
    with pytest.raises(OpportunityIntelligenceError, match="source_rollup_sha256"):
        evaluate_opportunity(tampered_rollup)


def test_conflicting_signal_is_zero_not_a_guessed_truth():
    p = payload()
    p["signals"].append(signal("tax-refute", "tax_delinquent", "ABSENT"))
    sync_property_population(p)
    result = evaluate_opportunity(p)
    factor = next(f for f in result["stacked_signals"]["normalized_factors"]
                  if f["factor_id"] == "tax_delinquent")
    assert factor["state"] == "CONFLICTED"
    assert factor["delta"] == 0
    assert any(u["factor_id"] == "tax_delinquent"
               for u in result["explanation"]["unknown_material_factors"])


def test_unknown_signal_type_is_preserved_and_explicitly_unscored():
    p = payload()
    rumor = signal("rumor-1", "unversioned_rumor", "PRESENT", "LOW")
    rumor["vendor_extension"] = {"opaque": "preserve me"}
    p["signals"].append(rumor)
    sync_property_population(p)
    result = evaluate_opportunity(p)
    assert result["stacked_signals"]["unrecognized_observations"] == [rumor]
    unknown = next(u for u in result["explanation"]["unknown_material_factors"]
                   if u["factor_id"] == "unversioned_rumor")
    assert unknown["possible_delta"] == {"minimum": None, "maximum": None}


def test_t50_attempts_are_normalized_beside_lossless_raw_payloads():
    attempts = [
        {"attempt_id": "a1", "source_id": "PROBATE", "result_state": "PUBLIC_INDEX_NO_MATCH",
         "evidence_refs": ["ev-1"], "custom_t50_field": {"keep": True}},
        {"attempt_id": "a2", "source_id": "PORTAL", "result_state": "BLOCKED",
         "evidence_refs": [], "notes": "instrument state, not subject evidence"},
    ]
    result = evaluate_opportunity(payload(research_attempts=attempts))
    research = result["research_results"]
    assert research["raw_attempts"] == attempts
    assert research["normalized_attempts"][0]["generic_state"] == "NO_MATCH"
    assert research["normalized_attempts"][0]["is_evidential"] is True
    assert research["normalized_attempts"][1]["is_evidential"] is False
    assert research["exhausted_source_ids"] == ["PROBATE"]


def test_unknown_t50_result_fails_closed():
    p = payload(research_attempts=[{"source_id": "X", "result_state": "PROBABLY"}])
    with pytest.raises(OpportunityIntelligenceError, match="T50 contract"):
        evaluate_opportunity(p)


def test_lineage_cardinalities_and_values_are_not_reduced():
    lineage = {
        "evidence_refs": [f"e-{i}" for i in range(54)],
        "contact_evidence_refs": [f"c-{i}" for i in range(31)],
        "source_families": [f"s-{i}" for i in range(22)],
        "relationship_node_refs": [f"n-{i}" for i in range(10)],
        "relationship_edge_refs": [f"r-{i}" for i in range(15)],
        "quarantined_identity_refs": [f"q-{i}" for i in range(7)],
    }
    result = evaluate_opportunity(payload(lineage=lineage))
    genome = result["opportunity_genome"]
    assert genome["lineage"] == lineage
    assert genome["lineage_counts"] == {key: len(values) for key, values in lineage.items()}


def test_no_authority_is_not_seller_ready_and_never_offer_ready():
    p = payload()
    p["readiness"]["legal_authority_verified"] = "NO"
    p["readiness"]["offer_authorized"] = "YES"
    result = evaluate_opportunity(p)
    assert result["readiness"]["authority_readiness"]["state"] == "NOT_READY"
    assert result["readiness"]["seller_readiness"]["state"] == "NOT_READY"
    assert result["readiness"]["offer_readiness"]["state"] == "NOT_READY"
    assert result["readiness"]["system_may_make_offer"] is False
    assert result["next_best_action"]["action"] == "ESTABLISH_LEGAL_AUTHORITY"
    assert result["safety"]["offer_sent_or_made"] is False


def test_no_value_is_not_offer_ready_even_when_offer_flag_is_yes():
    p = payload()
    p["readiness"]["value_established"] = "NO"
    p["readiness"]["offer_authorized"] = "YES"
    result = evaluate_opportunity(p)
    assert result["readiness"]["offer_readiness"]["state"] == "NOT_READY"
    assert result["next_best_action"]["action"] == "ESTABLISH_VALUE"


def test_government_owner_exclusion_forces_no_contact_offer_or_source_route():
    p = payload()
    for fact in p["readiness"]:
        p["readiness"][fact] = "YES"
    government = next(
        signal for signal in p["signals"]
        if signal["signal_type"] == "government_owner"
    )
    government["state"] = "PRESENT"
    result = evaluate_opportunity(p)

    assert result["next_best_action"]["action"] == "HOLD_NOT_A_SELLER"
    assert result["next_best_action"]["route"] == "DO_NOT_ROUTE"
    assert result["next_best_source"]["source_id"] == "NONE"
    assert result["next_best_source"]["mode"] == "NONE"
    assert "PROBATE" not in json.dumps(result["next_best_source"])
    assert result["readiness"]["system_may_contact"] is False
    assert result["readiness"]["system_may_make_offer"] is False
    assert result["priority"]["priority"] == "DO_NOT_ROUTE"
    assert result["routing"]["route"] == "DO_NOT_ROUTE"


def test_caller_top_level_extensions_are_lossless_in_genome_and_result():
    fixture_notice = {
        "kind": "CALLER_EXTENSION_COURT",
        "nested": {"preserve": [1, "two", None]},
    }
    opaque = {"vendor": "future", "payload": {"x": True}}
    p = payload(
        world_mode="FIXTURE",
        fixture_notice=fixture_notice,
        opaque_top_level=opaque,
        extensions={"named": {"keep": "exact"}},
    )
    result = evaluate_opportunity(p)
    expected = {
        "world_mode": "FIXTURE",
        "fixture_notice": fixture_notice,
        "opaque_top_level": opaque,
        "extensions": {"named": {"keep": "exact"}},
    }
    assert result["caller_top_level_extensions"] == expected
    assert result["opportunity_genome"]["caller_top_level_extensions"] == expected
    assert verify_opportunity_receipt(result, p)


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda p: p["recipe"]["confidence"].update(formula=""), "confidence.formula"),
        (
            lambda p: p["recipe"]["confidence"]["credits"].update(HIGH=2.0),
            "credits.HIGH",
        ),
        (
            lambda p: p["recipe"]["confidence"].update(conflicted_credit=-0.1),
            "conflicted_credit",
        ),
    ],
)
def test_invalid_confidence_policy_fails_as_contract_error(mutation, match):
    p = payload()
    mutation(p)
    redigest_recipe(p["recipe"])
    with pytest.raises(OpportunityIntelligenceError, match=match):
        evaluate_opportunity(p)


def test_empty_evidence_and_lineage_refs_fail_closed():
    p = payload()
    p["signals"][0]["evidence_refs"] = [""]
    with pytest.raises(OpportunityIntelligenceError, match="evidence_refs item"):
        evaluate_opportunity(p)

    p = payload()
    p["lineage"]["evidence_refs"] = [""]
    with pytest.raises(OpportunityIntelligenceError, match="lineage.evidence_refs item"):
        evaluate_opportunity(p)


def test_asset_conflict_is_explicit_unknown():
    result = evaluate_opportunity(payload(asset={
        "declared_class": "commercial", "property_type": "single_family",
    }))
    assert result["asset_classification"]["asset_class"] == "unknown"
    assert result["asset_classification"]["state"] == "CONFLICTED"


def test_recipe_tamper_is_rejected_before_scoring():
    p = payload()
    p["recipe"]["signal_rules"][0]["points_if_present"] = 99
    with pytest.raises(OpportunityIntelligenceError, match="config_digest"):
        evaluate_opportunity(p)


def test_resolution_context_uses_the_existing_landos_router():
    p = payload()
    p["readiness"]["legal_authority_verified"] = "UNKNOWN"
    p["resolution_context"] = {
        "owner_class": "ESTATE", "owner_condition": "DECEASED",
        "contact_resolution_state": "UNRESOLVED", "county": "Wake",
        "has_living_identity": False, "estate_public_index_searched": True,
    }
    result = evaluate_opportunity(p)
    assert result["next_best_source"]["origin"].startswith("next_best_source.")
    assert result["next_best_source"]["source_id"] == "COUNTY_ESTATE_INDEX_OFFLINE / RECORDED_DEED"


@pytest.fixture
def intelligence_api(monkeypatch):
    """Load the real FastAPI route without opening the unrelated DB pool."""
    import psycopg_pool

    class ClosedTestPool:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

        def connection(self):  # pragma: no cover - endpoint must remain pure
            raise AssertionError("opportunity-intelligence endpoint touched the database")

    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@127.0.0.1:1/test")
    monkeypatch.setenv("LANDOS_SECRET", "opportunity-api-test-secret")
    monkeypatch.delenv("NVE_LAND_TOKEN", raising=False)
    monkeypatch.setattr(psycopg_pool, "ConnectionPool", ClosedTestPool)
    for module in ("app.main", "app.db", "app.config"):
        sys.modules.pop(module, None)
    main = importlib.import_module("app.main")

    class AsgiClient:
        """Tiny dependency-free HTTP court over the actual ASGI app."""

        @staticmethod
        def post(path, *, headers, json_body):
            async def request():
                body = json.dumps(json_body).encode("utf-8")
                messages = []
                received = False

                async def receive():
                    nonlocal received
                    if received:
                        return {"type": "http.disconnect"}
                    received = True
                    return {"type": "http.request", "body": body, "more_body": False}

                async def send(message):
                    messages.append(message)

                scope = {
                    "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                    "http_version": "1.1", "method": "POST", "scheme": "http",
                    "path": path, "raw_path": path.encode("ascii"), "root_path": "",
                    "query_string": b"", "client": ("test", 123), "server": ("test", 80),
                    "headers": [
                        (key.lower().encode("ascii"), value.encode("ascii"))
                        for key, value in {"content-type": "application/json", **headers}.items()
                    ],
                }
                await main.app(scope, receive, send)
                status = next(m["status"] for m in messages if m["type"] == "http.response.start")
                response_body = b"".join(
                    m.get("body", b"") for m in messages if m["type"] == "http.response.body"
                )
                return status, json.loads(response_body or b"null")

            return asyncio.run(request())

    return AsgiClient()


def test_authenticated_endpoint_success(intelligence_api):
    status, body = intelligence_api.post(
        "/v1/opportunity-intelligence/evaluate",
        headers={"X-LANDOS-SECRET": "opportunity-api-test-secret"},
        json_body=payload(),
    )
    assert status == 200
    assert body["opportunity_score"]["score"] == 30
    assert body["receipt"]["schema_version"] == RECEIPT_CONTRACT_VERSION


def test_endpoint_rejects_tampered_recipe_with_422(intelligence_api):
    p = payload()
    p["recipe"]["signal_rules"][0]["points_if_present"] = 99
    status, body = intelligence_api.post(
        "/v1/opportunity-intelligence/evaluate",
        headers={"X-LANDOS-SECRET": "opportunity-api-test-secret"},
        json_body=p,
    )
    assert status == 422
    assert "config_digest" in body["detail"]


def test_endpoint_returns_422_not_500_for_invalid_confidence_policy(intelligence_api):
    p = payload()
    p["recipe"]["confidence"]["credits"]["HIGH"] = "certain"
    redigest_recipe(p["recipe"])
    status, body = intelligence_api.post(
        "/v1/opportunity-intelligence/evaluate",
        headers={"X-LANDOS-SECRET": "opportunity-api-test-secret"},
        json_body=p,
    )
    assert status == 422
    assert "credits.HIGH" in body["detail"]


def test_endpoint_requiredness_matches_wire_schema(intelligence_api):
    p = payload()
    del p["lineage"]
    status, body = intelligence_api.post(
        "/v1/opportunity-intelligence/evaluate",
        headers={"X-LANDOS-SECRET": "opportunity-api-test-secret"},
        json_body=p,
    )
    assert status == 422
    assert any(error["loc"][-1] == "lineage" for error in body["detail"])


def test_endpoint_rejects_bad_auth(intelligence_api):
    status, _body = intelligence_api.post(
        "/v1/opportunity-intelligence/evaluate",
        headers={"X-LANDOS-SECRET": "wrong"},
        json_body=payload(),
    )
    assert status == 401

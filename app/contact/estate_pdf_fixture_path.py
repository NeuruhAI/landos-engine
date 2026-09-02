"""Controlled synthetic estate PDF fixture path (no DB, no Tracerfy, skip-trace OFF).

Proves, in process, the LandOS-owned chain:

    AOC PDF → estate CSV → import contract → LandOS candidate → recipe → contact state

This module never writes landos-ingest, DeedSonar, or recipe-engine. It never
imports Tracerfy. Skip-trace stays OFF. Attorney phones are never accepted
(the PDF intake already refuses them; this path does not re-introduce them).
Estate import never writes LEGAL_AUTHORITY. Callability stays NOT_AUTHORIZED.

The court-safe projection deliberately omits opportunity-score deltas, fusion
weights, and recipe point tables. Downstream SKUs consume recipe_id +
config_digest + contact state + next action, not scoring internals.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.contact.estate_import import load_estate_import_csv
from app.contact.estate_import_validate import validate_estate_import_file
from app.contact.estate_pdf_intake import intake_estate_pdfs
from app.contact.identity_tuple import identity_tuple_hash
from app.contact.owner_class import owner_route_class
from app.contact.owner_identity import parse_owner_name
from app.contact.resolution import (
    CALLABILITY_NOT_AUTHORIZED,
    build_resolution_ledger,
)
from app.opportunity_intelligence import evaluate_opportunity

MISSION_ID = "NEURUH-LANDOS-PDF-FIXTURE-20260902"
FIXTURE_RECIPE_ID = "synthetic_estate_pdf_fixture_v1"
SYNTHETIC_CANDIDATE_ID = "synthetic-wake-estate-001"
SYNTHETIC_APN = "SYNTH-WAKE-0001"
# Closest existing resolution vocabulary: living heir/representative named,
# no accepted phone. Estate-file identity is not a voter match and not LEGAL_AUTHORITY.
ESTATE_IMPORT_NO_PHONE_STATE = "HEIR_ON_ROLL_NO_ACCEPTED_PHONE"

# Buyer / court projection: never copy these keys out of evaluate_opportunity.
_SCORE_INTERNAL_KEYS = frozenset({
    "opportunity_score",
    "confidence_score",
    "stacked_signals",
    "explanation",
    "opportunity_genome",
})


def synthetic_landos_candidate() -> dict:
    """In-memory CURRENT estate row. Synthetic names only. No database write."""
    return {
        "candidate_id": SYNTHETIC_CANDIDATE_ID,
        "apn": SYNTHETIC_APN,
        "county": "Wake",
        "fips": "37183",
        "owner_name": "ALDERNEY, QUINCY P HEIRS",
        "property_address_raw": "100 FIXTURE ST",
        "property_city": "RALEIGH",
        "property_state": "NC",
        "property_zip": "27601",
        "property_address_normalized": "100 FIXTURE ST",
        "mailing_address": "100 FIXTURE ST",
        "mailing_city": "RALEIGH",
        "mailing_state": "NC",
        "mailing_zip": "27601",
        "primary_recipe": FIXTURE_RECIPE_ID,
        "qualification_state": "QUALIFIED_SYNTHETIC_FIXTURE",
        "score": "",
        "global_rank": "1",
        "dnc_status": "UNKNOWN_NOT_EVALUATED",
    }


def decedent_lookup_key(decedent_name: str) -> str:
    return "".join(ch for ch in decedent_name.upper().replace("HEIRS", "") if ch.isalpha())


def _parcel_identity(county_fips: str, parcel_apn: str) -> dict:
    apn = re.sub(r"[^A-Za-z0-9]+", "", parcel_apn).upper()
    return {
        "kind": "parcel",
        "key": f"{county_fips}|P|{apn}",
        "county_fips": county_fips,
        "basis_value": apn,
    }


def bind_people_to_candidate(
    people: Sequence[Any], candidate: Mapping[str, str]
) -> List[Any]:
    cid = candidate["candidate_id"]
    return [p for p in people if p.property_candidate_id == cid]


def contact_ledger_for_fixture(
    candidate: Mapping[str, str],
    living_people: Sequence[Any],
) -> List[dict]:
    """Resolution ledger with skip-trace OFF and no phone append.

    Living estate-import people prove identity. They do not prove a phone, DNC,
    LEGAL_AUTHORITY, or callability.
    """
    estate = [dict(candidate)]
    parsed = parse_owner_name(candidate.get("owner_name") or "")
    oc = owner_route_class(candidate.get("owner_name") or "", parsed)
    owner_class = {candidate["candidate_id"]: oc}
    prior_reasons: Dict[str, str] = {}
    if living_people:
        prior_reasons[candidate["candidate_id"]] = ESTATE_IMPORT_NO_PHONE_STATE
    return build_resolution_ledger(
        estate,
        owner_class,
        phones_by_cand={},
        identity_tuple_hash=identity_tuple_hash,
        identity_cands=set(),
        prior_reasons=prior_reasons,
    )


def _court_safe_opportunity(result: Mapping[str, Any]) -> dict:
    recipe = dict(result.get("recipe") or {})
    recipe.pop("signal_rules", None)
    return {
        "schema_version": result.get("schema_version"),
        "subject_id": result.get("subject_id"),
        "recipe": {
            "recipe_id": recipe.get("recipe_id"),
            "version": recipe.get("version"),
            "config_digest": recipe.get("config_digest"),
            "score_version": recipe.get("score_version"),
        },
        "asset_classification": {
            "asset_class": (result.get("asset_classification") or {}).get("asset_class"),
            "state": (result.get("asset_classification") or {}).get("state"),
        },
        "readiness": {
            "authority_readiness": (result.get("readiness") or {}).get("authority_readiness"),
            "contact_readiness": (result.get("readiness") or {}).get("contact_readiness"),
            "seller_readiness": (result.get("readiness") or {}).get("seller_readiness"),
            "offer_readiness": (result.get("readiness") or {}).get("offer_readiness"),
            "system_may_contact": (result.get("readiness") or {}).get("system_may_contact"),
            "system_may_make_offer": (result.get("readiness") or {}).get("system_may_make_offer"),
        },
        "next_best_action": {
            "action": (result.get("next_best_action") or {}).get("action"),
            "route": (result.get("next_best_action") or {}).get("route"),
            "trigger_fact": (result.get("next_best_action") or {}).get("trigger_fact"),
            "execution_authority": (result.get("next_best_action") or {}).get("execution_authority"),
        },
        "next_best_source": {
            "source_id": (result.get("next_best_source") or {}).get("source_id"),
            "origin": (result.get("next_best_source") or {}).get("origin"),
            "operator_queue": (result.get("next_best_source") or {}).get("operator_queue"),
            "mode": (result.get("next_best_source") or {}).get("mode"),
        },
        "routing": {
            "route": (result.get("routing") or {}).get("route"),
            "operator_queue": (result.get("routing") or {}).get("operator_queue"),
        },
        "safety": dict(result.get("safety") or {}),
        "receipt": {
            "schema_version": (result.get("receipt") or {}).get("schema_version"),
            "input_digest": (result.get("receipt") or {}).get("input_digest"),
            "recipe_digest": (result.get("receipt") or {}).get("recipe_digest"),
            "result_digest": (result.get("receipt") or {}).get("result_digest"),
            "receipt_sha256": (result.get("receipt") or {}).get("receipt_sha256"),
        },
    }


def opportunity_payload_for_fixture(
    candidate: Mapping[str, str],
    ledger_row: Mapping[str, str],
    living_people: Sequence[Any],
    recipe: Mapping[str, Any],
    *,
    as_of: str,
) -> dict:
    has_living = bool(living_people)
    return {
        "schema_version": "neuruh.opportunity-intelligence.input.v1",
        "as_of": as_of,
        "recipe": recipe,
        "subject": {
            "subject_id": candidate["candidate_id"],
            "county_fips": candidate["fips"],
            "parcel_apn": candidate["apn"],
        },
        "asset": {"declared_class": "unknown"},
        "signals": [
            {
                "signal_id": "fixture-gov-1",
                "signal_type": "government_owner",
                "state": "ABSENT",
                "confidence": "HIGH",
                "evidence_refs": ["evidence:fixture-gov-1"],
                "source_families": ["source:synthetic_estate_pdf"],
            },
        ],
        "property_population": {
            "schema_version": "neuruh.opportunity-intelligence.property-population.v1",
            "scope": "SUBJECT_SIGNAL_SET",
            "property_identity": _parcel_identity(candidate["fips"], candidate["apn"]),
            "signal_observation_count": 1,
            "property_count": 1,
            "unresolved_signal_count": 0,
            "signal_amplification": 1.0,
            "identity_kinds": {"parcel": 1},
            "dedupe_provenance": {
                "identity_method": "county_fips+normalized_parcel_apn_else_raw_address",
                "producer_contract": "neuruh.opportunity-intelligence.property-population.v1",
                "producer": "LANDOS_ESTATE_PDF_FIXTURE",
            },
        },
        "readiness": {
            "identity_verified": "YES" if has_living else "NO",
            "legal_authority_verified": "NO",
            "contact_point_verified": "NO",
            "contact_authorized": "NO",
            "value_established": "UNKNOWN",
            "underwriting_complete": "UNKNOWN",
            "risk_review_complete": "UNKNOWN",
            "offer_authorized": "NO",
        },
        "research_attempts": [
            {
                "attempt_id": "fixture-estate-pdf-1",
                "source_id": "ESTATE_IMPORT",
                "result_state": (
                    "OFFICIAL_ESTATE_CASE_FOUND" if has_living else "PUBLIC_INDEX_NO_MATCH"
                ),
                "evidence_refs": ["evidence:synthetic-aoc-e-201"],
            },
        ],
        "lineage": {
            "evidence_refs": ["evidence:synthetic-aoc-e-201"],
            "contact_evidence_refs": [
                f"heir:{p.heir_person_id}" for p in living_people
            ],
            "source_families": ["source:synthetic_estate_pdf"],
            "relationship_node_refs": [],
            "relationship_edge_refs": [],
            "quarantined_identity_refs": [],
        },
        "resolution_context": {
            "owner_class": ledger_row.get("owner_class") or "",
            "owner_condition": "DECEASED",
            "contact_resolution_state": ledger_row.get("contact_resolution_state") or "",
            "county": candidate.get("county") or "",
            "has_living_identity": has_living,
            "has_high_phone": False,
            "estate_public_index_searched": False,
        },
    }


def run_estate_pdf_fixture_path(
    raw_dir: Path,
    *,
    recipe: Mapping[str, Any],
    candidate: Optional[Mapping[str, str]] = None,
    candidate_lookup: Optional[Dict[str, Tuple[str, str]]] = None,
    today: Optional[date] = None,
) -> dict:
    """Execute the controlled fixture path. Returns a court-safe receipt.

    `recipe` is supplied by the caller (tests). This function does not serialize
    recipe point tables into the returned receipt.
    """
    cand = dict(candidate or synthetic_landos_candidate())
    stamp = today or date(2026, 9, 2)
    lookup = candidate_lookup or {
        decedent_lookup_key("ALDERNEY, QUINCY P"): (cand["candidate_id"], cand["apn"]),
    }
    receipts = intake_estate_pdfs(raw_dir, today=stamp, candidate_lookup=lookup)
    csv_path = Path(raw_dir) / "estate" / f"wake_estate_{stamp.isoformat()}.csv"
    report = validate_estate_import_file(csv_path, expect_placement=True) if csv_path.exists() else {
        "overall": "FAIL", "imported": False, "checks": {"PATH": {"result": "FAIL"}},
    }
    people = load_estate_import_csv(csv_path) if csv_path.exists() else []
    bound = bind_people_to_candidate(people, cand)
    ledger = contact_ledger_for_fixture(cand, bound)
    ledger_row = ledger[0] if ledger else {}
    as_of = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    payload = opportunity_payload_for_fixture(
        cand, ledger_row, bound, recipe, as_of=as_of,
    )
    evaluated = evaluate_opportunity(payload)
    pdf = receipts[0] if receipts else None
    joined = ""
    if csv_path.exists():
        joined = csv_path.read_text(encoding="utf-8")
    return {
        "mission_id": MISSION_ID,
        "status": "LIVE_PROVEN" if (
            pdf is not None
            and pdf.state == "ROWS_WRITTEN"
            and report.get("overall") == "PASS"
            and bound
            and ledger_row.get("callability_status") == CALLABILITY_NOT_AUTHORIZED
            and ledger_row.get("phone_present") == "NO"
            and evaluated.get("safety", {}).get("seller_contact_performed") is False
        ) else "NOT_PROVEN",
        "skip_trace": "OFF",
        "tracerfy": "NOT_IMPORTED",
        "legal_authority_written": False,
        "pdf_intake": {
            "state": getattr(pdf, "state", ""),
            "form": getattr(pdf, "form", ""),
            "case_number": getattr(pdf, "case_number", ""),
            "decedent_name": getattr(pdf, "decedent_name", ""),
            "people_written": getattr(pdf, "people_written", 0),
            "attorney_names_ignored": getattr(pdf, "attorney_names_ignored", 0),
            "phones_seen_and_refused": getattr(pdf, "phones_seen_and_refused", 0),
            "sha256": getattr(pdf, "sha256", ""),
        },
        "import_contract": {
            "overall": report.get("overall"),
            "imported": report.get("imported"),
            "csv_path": str(csv_path) if csv_path.exists() else "",
            "living_people": len(people),
            "bound_people": len(bound),
            "phone_digits_in_csv": "555" in joined,
        },
        "landos_candidate": {
            "candidate_id": cand["candidate_id"],
            "apn": cand["apn"],
            "county": cand["county"],
            "fips": cand["fips"],
            "owner_class": ledger_row.get("owner_class"),
            "primary_recipe": cand.get("primary_recipe"),
            "database_written": False,
        },
        "contact_state": {
            "contact_resolution_state": ledger_row.get("contact_resolution_state"),
            "phone_present": ledger_row.get("phone_present"),
            "callability_status": ledger_row.get("callability_status"),
            "dnc_status": ledger_row.get("dnc_status"),
            "has_living_identity": bool(bound),
            "attorney_phone_accepted": False,
        },
        "opportunity": _court_safe_opportunity(evaluated),
        "invariants": {
            "skip_trace": "OFF",
            "tracerfy_called": False,
            "attorney_phones_accepted": False,
            "legal_authority_asserted": False,
            "scoring_weights_exposed": False,
            "real_pii": False,
            "landos_ingest_written": False,
            "deedsonar_written": False,
            "recipe_engine_written": False,
            "property_scan_written": False,
            "fulfillment_pin_unfrozen": False,
        },
    }


def assert_no_score_weights(blob: Mapping[str, Any]) -> None:
    """Court helper: the projected receipt must not carry point tables."""
    dumped = repr(blob)
    for needle in ("points_if_present", "points_if_absent", "fusion_weight",
                   "confidence_weight", "scoring_weights"):
        if needle in dumped:
            raise AssertionError(f"scoring internals leaked into court receipt: {needle}")
    for key in _SCORE_INTERNAL_KEYS:
        if key in blob:
            raise AssertionError(f"score-internal key {key} present on court receipt")
        opportunity = blob.get("opportunity")
        if isinstance(opportunity, Mapping) and key in opportunity:
            raise AssertionError(f"score-internal key {key} present under opportunity")

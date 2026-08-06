from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.nc_dps_contract import (
    composite_priority,
    normalized_dps_score,
    raw_dps_score,
    route_priority,
    validate_opportunity_lanes,
)

COUNTY_NAMES = {"37119": "Mecklenburg", "37179": "Union", "37135": "Orange", "37057": "Davidson"}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def owner_type(owner: str | None) -> str:
    text = (owner or "").upper()
    if " LLC" in text or text.endswith("LLC"):
        return "llc"
    if " TRUST" in text:
        return "trust"
    if " ESTATE" in text or "HEIRS" in text:
        return "estate"
    if any(token in text for token in (" INC", " CORP", " CORPORATION")):
        return "corporation"
    return "individual" if text else "unknown"


def build_property(signals: list[dict[str, Any]], schema: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    base = max(signals, key=lambda s: (s["source_type"] == "title_instrument", s.get("confidence", 0)))
    details = [s.get("detail_json") or {} for s in signals]
    fips = base["geography"]
    parcel = base["parcel_apn"]
    owner = base.get("owner_name") or None
    property_id = f"NC:{fips}:{parcel}"

    assessed_values = [d.get("assessed_value") for d in details if isinstance(d.get("assessed_value"), (int, float))]
    assessed = max(assessed_values) if assessed_values else None
    tax_balances = [d.get("tax_balance") for d in details if isinstance(d.get("tax_balance"), (int, float))]
    tax_balance = sum(tax_balances) if tax_balances else None
    tax_years = sorted({int(d["tax_year"]) for d in details if isinstance(d.get("tax_year"), int)})
    out_of_state = any(d.get("out_of_state") is True for d in details)
    mailing = next((d.get("mailing_address") for d in details if d.get("mailing_address")), None)
    address = next((s.get("raw_address") for s in signals if s.get("raw_address")), None)
    types = {s["source_type"] for s in signals}

    triggers = []
    if "taxdelinquent" in types or "taxsale" in types or "tax_foreclosure_case" in types:
        triggers.append("tax_distress")
    if "civil_case" in types or "lis_pendens" in types or "tax_foreclosure_case" in types:
        triggers.append("court_action")
    if "probate" in types or "pre_probate_death" in types:
        triggers.append("death_or_probate")
    if "judgment_lien" in types:
        triggers.append("recorded_lien_or_judgment")
    if "vacancy" in types or "code_violation" in types:
        triggers.append("vacancy_or_code")
    if "entity_status" in types:
        triggers.append("entity_failure")
    if out_of_state:
        triggers.append("absentee_or_out_of_state")
    triggers = list(dict.fromkeys(triggers))

    active = set()
    if out_of_state:
        active.update({"absentee_owner", "out_of_state_owner"})
    if len(tax_years) >= 5:
        active.add("taxes_delinquent_5_plus_years")
    elif len(tax_years) >= 2:
        active.add("taxes_delinquent_2_4_years")

    raw_score = raw_dps_score(active)
    normalized = normalized_dps_score(active)

    equity_lane = 5.0
    if assessed is not None:
        equity_lane = 20.0 if assessed >= 250000 else 15.0 if assessed >= 150000 else 10.0
    distress_lane = 25.0 if "tax_foreclosure_case" in types or "taxsale" in types else 18.0 if "taxdelinquent" in types else 0.0
    title_lane = 8.0 if "judgment_lien" in types else 0.0
    owner_lane = 10.0 if out_of_state else 0.0
    market_lane = 5.0 if assessed and assessed >= 125000 else 2.0
    lanes = {
        "equity_quality": equity_lane,
        "tax_and_court_distress": distress_lane,
        "title_curative_opportunity": title_lane,
        "owner_detachment": owner_lane,
        "market_liquidity": market_lane,
    }
    opportunity = validate_opportunity_lanes(lanes)
    urgency = 75.0 if "taxsale" in types else 62.0 if "tax_foreclosure_case" in types else 48.0 if "taxdelinquent" in types else 28.0
    evidence = round(sum(float(s.get("confidence", 0)) for s in signals) / len(signals) * 100, 2)
    priority = composite_priority(opportunity, urgency, evidence)
    route = route_priority(priority)

    years_bucket = "unknown"
    if tax_years:
        years_bucket = "one_year" if len(tax_years) == 1 else "two_to_four_years" if len(tax_years) <= 4 else "five_plus_years"
    elif "taxdelinquent" not in types:
        years_bucket = "none"

    matched_recipes = []
    if "tax_distress" in triggers and out_of_state:
        matched_recipes.append("tax_distress_absentee_owner")
    if "tax_distress" in triggers:
        matched_recipes.append("published_tax_distress")
    if out_of_state:
        matched_recipes.append("long_term_absentee_enrichment")

    governance_keys = schema["properties"]["governance"]["properties"].keys()
    packet = {
        "schema_version": "nc_dps_property_packet.v1",
        "property_id": property_id,
        "state": "NC",
        "county": COUNTY_NAMES.get(fips, fips),
        "county_fips": fips,
        "parcel_id": parcel,
        "identity": {
            "property_address": address,
            "owner_of_record": owner,
            "owner_mailing_address": mailing,
            "occupancy_indicator": "unknown",
        },
        "valuation": {
            "conservative_as_is_value": assessed,
            "estimated_mortgage_balance": None,
            "tax_balance": tax_balance,
            "judgment_and_lien_balance": None,
            "estimated_equity": None,
            "equity_ratio": None,
            "equity_confidence": 25.0 if assessed is not None else 0.0,
        },
        "tax_distress": {
            "delinquent_years": tax_years,
            "oldest_delinquent_year": min(tax_years) if tax_years else None,
            "tax_balance": tax_balance,
            "delinquency_bucket": years_bucket,
            "foreclosure_case_number": None,
            "foreclosure_stage": "unknown" if "taxdelinquent" in types else "none",
            "sale_date": None,
            "upset_bid_deadline": None,
        },
        "court": {
            "case_age_days": None,
            "days_since_last_activity": None,
            "failed_service_count": 0,
            "unserved_party_count": 0,
            "amended_complaint_count": 0,
            "unknown_heir_flag": False,
            "publication_flag": False,
            "ad_litem_flag": False,
            "bankruptcy_status": "unknown",
        },
        "ownership": {
            "absentee": True if out_of_state else None,
            "out_of_state": True if out_of_state else None,
            "years_owned": None,
            "death_status": "unknown",
            "probate_status": "unknown",
            "interested_party_count": None,
            "required_signature_count": None,
            "fractional_ownership_flag": False,
        },
        "title": {
            "free_and_clear_status": "unknown",
            "deed_chain_status": "unknown",
            "title_issue_codes": [],
            "competing_deed_flag": False,
            "fraud_flag": False,
        },
        "physical": {
            "vacant": None,
            "code_case_open": None,
            "code_case_age_days": None,
            "condemned": None,
            "demolition_pending": None,
        },
        "entity": {
            "owner_type": owner_type(owner),
            "entity_status": "not_applicable" if owner_type(owner) == "individual" else "unknown",
        },
        "scores": {
            "raw_dps_score": raw_score,
            "normalized_dps_score": normalized,
            "opportunity_score": opportunity,
            "urgency_score": urgency,
            "evidence_confidence": evidence,
            "composite_priority": priority,
            "trigger_count": len(triggers),
            "trigger_classes": triggers,
        },
        "recipes": {
            "matched_recipes": matched_recipes,
            "recommended_recipe": matched_recipes[0] if matched_recipes else None,
        },
        "risks": [
            "assessed_value_is_not_verified_market_value",
            "mortgage_and_title_balances_not_yet_verified",
        ],
        "next_research_steps": [
            "Verify current tax balance and foreclosure stage with the county tax office.",
            "Pull the current deed and prior conveyances from the Register of Deeds.",
            "Search civil, special-proceeding, probate, judgment, and bankruptcy records.",
            "Verify mortgage releases, liens, ownership interests, and required signatures.",
            "Complete market valuation and occupancy verification before any contact or offer.",
        ],
        "source_records": [
            {
                "source_id": s["signal_id"],
                "source_url": (s.get("detail_json") or {}).get("source_url") or "unknown://source",
                "retrieved_at": s.get("fetched_at") or utc_now(),
                "evidence_class": "verified_public_record",
                "confidence": float(s.get("confidence", 0)),
                "field_paths": ["identity", "valuation", "ownership", "tax_distress"],
            }
            for s in signals
        ],
        "governance": {key: False for key in governance_keys},
    }

    packet_hash = sha256(packet)
    receipt = {
        "schema_version": "nc_dps_wave_a_score_receipt.v1",
        "property_id": property_id,
        "generated_at": utc_now(),
        "active_raw_signals": sorted(active),
        "opportunity_lanes": lanes,
        "route": route,
        "property_packet_sha256": packet_hash,
        "input_signal_sha256": sha256(signals),
        "score_components": packet["scores"],
    }
    receipt["receipt_sha256"] = sha256(receipt)
    return packet, receipt


def build(signals_path: Path, output_dir: Path, schema_path: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = read_jsonl(signals_path)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        if signal.get("geography") and signal.get("parcel_apn"):
            groups[(signal["geography"], signal["parcel_apn"])].append(signal)

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    packets, receipts = [], []
    for _, rows in groups.items():
        packet, receipt = build_property(rows, schema)
        packets.append(packet)
        receipts.append(receipt)
    packets.sort(key=lambda p: p["scores"]["composite_priority"], reverse=True)
    receipt_by_property = {r["property_id"]: r for r in receipts}

    write_jsonl(output_dir / "property_packets.jsonl", packets)
    write_jsonl(output_dir / "score_receipts.jsonl", [receipt_by_property[p["property_id"]] for p in packets])

    with (output_dir / "ranked_properties.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "rank", "property_id", "county", "parcel_id", "property_address", "owner_of_record",
            "assessed_value_proxy", "tax_balance", "composite_priority", "route", "triggers",
        ])
        writer.writeheader()
        for rank, packet in enumerate(packets, 1):
            writer.writerow({
                "rank": rank,
                "property_id": packet["property_id"],
                "county": packet["county"],
                "parcel_id": packet["parcel_id"],
                "property_address": packet["identity"]["property_address"],
                "owner_of_record": packet["identity"]["owner_of_record"],
                "assessed_value_proxy": packet["valuation"]["conservative_as_is_value"],
                "tax_balance": packet["valuation"]["tax_balance"],
                "composite_priority": packet["scores"]["composite_priority"],
                "route": receipt_by_property[packet["property_id"]]["route"],
                "triggers": "|".join(packet["scores"]["trigger_classes"]),
            })

    crm, transactions, dispositions = [], [], []
    for packet in packets:
        priority = packet["scores"]["composite_priority"]
        route = receipt_by_property[packet["property_id"]]["route"]
        source_urls = {
            source.get("source_url")
            for source in packet.get("source_records", [])
            if source.get("source_url")
        }
        corroborated = len(source_urls) >= 2
        if priority >= 45 and corroborated:
            crm.append({
                "projection_version": "nc_dps_crm_projection.v1",
                "external_key": packet["property_id"],
                "pipeline": "acquisition",
                "stage": "research_queue",
                "status": route,
                "priority": priority,
                "property": packet["identity"],
                "county_fips": packet["county_fips"],
                "parcel_id": packet["parcel_id"],
                "recommended_recipe": packet["recipes"]["recommended_recipe"],
                "next_actions": packet["next_research_steps"],
                "source_count": len(packet["source_records"]),
                "write_authorized": False,
            })
        if priority >= 55:
            transactions.append({
                "projection_version": "nc_dps_transaction_projection.v1",
                "property_id": packet["property_id"],
                "status": "pre_transaction_diligence",
                "tasks": [
                    "verify_title_and_all_interest_holders",
                    "verify_taxes_liens_mortgages_and_bankruptcy",
                    "verify_occupancy_and_property_condition",
                    "prepare_primary_fallback_and_floor_strategy",
                    "obtain_principal_and_legal_approval_before_execution",
                ],
                "execution_authorized": False,
            })
        if priority >= 55:
            dispositions.append({
                "projection_version": "nc_dps_disposition_projection.v1",
                "property_id": packet["property_id"],
                "status": "blocked_until_control_and_title_verified",
                "candidate_channels": ["broker_mls", "investor_sale", "builder_land_sale", "hold_or_rental"],
                "pricing_authorized": False,
                "marketing_authorized": False,
            })

    write_jsonl(output_dir / "crm_projection.jsonl", crm)
    write_jsonl(output_dir / "transaction_projection.jsonl", transactions)
    write_jsonl(output_dir / "disposition_projection.jsonl", dispositions)

    summary = {
        "generated_at": utc_now(),
        "input_signals": len(signals),
        "property_packets": len(packets),
        "crm_projections": len(crm),
        "transaction_projections": len(transactions),
        "disposition_projections": len(dispositions),
        "top_priority": packets[0]["scores"]["composite_priority"] if packets else None,
    }
    (output_dir / "engine_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary

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

COUNTY_NAMES = {
    "37119": "Mecklenburg",
    "37179": "Union",
    "37135": "Orange",
    "37057": "Davidson",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def numeric(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


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


def build_property(
    signals: list[dict[str, Any]],
    schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    distress = [
        row for row in signals
        if row.get("source_type") in {"taxdelinquent", "taxsale", "tax_foreclosure_case"}
    ]
    context = [
        row for row in signals
        if row.get("source_type") == "title_instrument"
    ]
    if not distress:
        raise ValueError("qualified property group has no distress evidence")

    base = max(context or distress, key=lambda row: float(row.get("confidence", 0)))
    details = [row.get("detail_json") or {} for row in signals]
    fips = str(base["geography"])
    parcel = str(base["parcel_apn"])
    owner = base.get("owner_name") or distress[0].get("owner_name") or None
    property_id = f"NC:{fips}:{parcel}"

    assessed_values = [
        numeric(detail.get("assessed_value"))
        for detail in details
    ]
    assessed_values = [value for value in assessed_values if value is not None]
    assessed = max(assessed_values) if assessed_values else None

    balances = [
        numeric((row.get("detail_json") or {}).get("tax_balance"))
        for row in distress
    ]
    balances = [value for value in balances if value is not None and value > 0]
    tax_balance = sum(balances) if balances else None

    tax_years = sorted({
        int((row.get("detail_json") or {})["tax_year"])
        for row in distress
        if isinstance((row.get("detail_json") or {}).get("tax_year"), int)
    })

    out_of_state = any(detail.get("out_of_state") is True for detail in details)
    mailing = next(
        (detail.get("mailing_address") for detail in details if detail.get("mailing_address")),
        None,
    )
    address = next(
        (row.get("raw_address") for row in context + distress if row.get("raw_address")),
        None,
    )

    types = {row.get("source_type") for row in signals}
    source_urls = {
        str((row.get("detail_json") or {}).get("source_url") or row.get("source") or "")
        for row in signals
    }
    source_urls.discard("")
    corroborated = bool(context) and len(source_urls) >= 2

    triggers = ["tax_distress"]
    if types & {"civil_case", "lis_pendens", "tax_foreclosure_case"}:
        triggers.append("court_action")
    if types & {"probate", "pre_probate_death"}:
        triggers.append("death_or_probate")
    if "judgment_lien" in types:
        triggers.append("recorded_lien_or_judgment")
    if types & {"vacancy", "code_violation"}:
        triggers.append("vacancy_or_code")
    if out_of_state:
        triggers.append("absentee_or_out_of_state")
    triggers = list(dict.fromkeys(triggers))

    active: set[str] = set()
    if out_of_state:
        active.update({"absentee_owner", "out_of_state_owner"})
    if len(tax_years) >= 5:
        active.add("taxes_delinquent_5_plus_years")
    elif len(tax_years) >= 2:
        active.add("taxes_delinquent_2_4_years")

    raw_score = raw_dps_score(active)
    normalized = normalized_dps_score(active)

    if assessed is None:
        equity_lane = 0.0
    elif assessed >= 250000:
        equity_lane = 20.0
    elif assessed >= 150000:
        equity_lane = 15.0
    elif assessed >= 75000:
        equity_lane = 10.0
    else:
        equity_lane = 5.0

    if "taxsale" in types:
        distress_lane = 25.0
    elif "tax_foreclosure_case" in types:
        distress_lane = 20.0
    else:
        distress_lane = 8.0

    lanes = {
        "equity_quality": equity_lane,
        "tax_and_court_distress": distress_lane,
        "title_curative_opportunity": 8.0 if "judgment_lien" in types else 0.0,
        "owner_detachment": 10.0 if out_of_state else 0.0,
        "market_liquidity": 5.0 if assessed is not None and assessed >= 250000 else 2.0 if assessed is not None and assessed >= 125000 else 0.0,
    }
    opportunity = validate_opportunity_lanes(lanes)
    urgency = 75.0 if "taxsale" in types else 62.0 if "tax_foreclosure_case" in types else 35.0

    average_confidence = round(
        sum(float(row.get("confidence", 0)) for row in signals) / len(signals) * 100,
        2,
    )
    evidence = average_confidence if corroborated else min(35.0, average_confidence)
    priority = composite_priority(opportunity, urgency, evidence)
    route = route_priority(priority)

    if not corroborated:
        queue = "enrichment_backlog"
    elif priority >= 45:
        queue = "crm_research"
    else:
        queue = "analyst_research"

    years_bucket = (
        "one_year" if len(tax_years) == 1
        else "two_to_four_years" if 2 <= len(tax_years) <= 4
        else "five_plus_years" if len(tax_years) >= 5
        else "unknown"
    )

    if "court_action" in triggers:
        recommended = "stalled_tax_case_resolution"
    elif corroborated and out_of_state:
        recommended = "tax_distress_absentee_owner"
    else:
        recommended = "monitor"

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
            "equity_confidence": 35.0 if corroborated and assessed is not None else 0.0,
        },
        "tax_distress": {
            "delinquent_years": tax_years,
            "oldest_delinquent_year": min(tax_years) if tax_years else None,
            "tax_balance": tax_balance,
            "delinquency_bucket": years_bucket,
            "foreclosure_case_number": None,
            "foreclosure_stage": "unknown" if "court_action" in triggers else "none",
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
            "deed_chain_status": "incomplete" if corroborated else "unknown",
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
            "matched_recipes": [recommended] if recommended != "monitor" else [],
            "recommended_recipe": recommended,
        },
        "risks": [
            "assessed_value_is_not_verified_market_value",
            "mortgage_and_title_balances_not_yet_verified",
        ] + ([] if corroborated else ["single_source_distress_requires_enrichment"]),
        "next_research_steps": [
            "Verify current tax balance and foreclosure stage with the county tax office.",
            "Pull the current deed and prior conveyances from the Register of Deeds.",
            "Search civil, special-proceeding, probate, judgment, and bankruptcy records.",
            "Verify mortgage releases, liens, ownership interests, and required signatures.",
            "Complete market valuation and occupancy verification before any contact or offer.",
        ],
        "source_records": [
            {
                "source_id": str(row["signal_id"]),
                "source_url": str((row.get("detail_json") or {}).get("source_url") or row.get("source") or "unknown://source"),
                "retrieved_at": str(row.get("fetched_at") or utc_now()),
                "evidence_class": "verified_public_record",
                "confidence": float(row.get("confidence", 0)),
                "field_paths": ["identity", "valuation", "ownership", "tax_distress"],
            }
            for row in signals
        ],
        "governance": {key: False for key in governance_keys},
    }

    receipt = {
        "schema_version": "nc_dps_wave_a_score_receipt.v2",
        "property_id": property_id,
        "generated_at": utc_now(),
        "active_raw_signals": sorted(active),
        "opportunity_lanes": lanes,
        "route": route,
        "queue": queue,
        "corroborated": corroborated,
        "source_family_count": len(source_urls),
        "property_packet_sha256": sha256(packet),
        "input_signal_sha256": sha256(signals),
        "score_components": packet["scores"],
    }
    receipt["receipt_sha256"] = sha256(receipt)
    return packet, receipt


def build(signals_path: Path, output_dir: Path, schema_path: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = read_jsonl(signals_path)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    for row in signals:
        if row.get("geography") and row.get("parcel_apn"):
            groups[(str(row["geography"]), str(row["parcel_apn"]))].append(row)

    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    packets: list[dict[str, Any]] = []
    receipts: list[dict[str, Any]] = []

    for group in groups.values():
        packet, receipt = build_property(group, schema)
        packets.append(packet)
        receipts.append(receipt)

    packets.sort(key=lambda packet: packet["scores"]["composite_priority"], reverse=True)
    by_property = {receipt["property_id"]: receipt for receipt in receipts}
    ordered_receipts = [by_property[packet["property_id"]] for packet in packets]

    write_jsonl(output_dir / "property_packets.jsonl", packets)
    write_jsonl(output_dir / "score_receipts.jsonl", ordered_receipts)

    with (output_dir / "ranked_properties.csv").open("w", newline="", encoding="utf-8") as fh:
        fields = [
            "rank", "property_id", "county", "parcel_id", "property_address",
            "owner_of_record", "assessed_value_proxy", "tax_balance",
            "composite_priority", "route", "queue", "corroborated",
            "source_family_count", "triggers",
        ]
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for rank, packet in enumerate(packets, 1):
            receipt = by_property[packet["property_id"]]
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
                "route": receipt["route"],
                "queue": receipt["queue"],
                "corroborated": receipt["corroborated"],
                "source_family_count": receipt["source_family_count"],
                "triggers": "|".join(packet["scores"]["trigger_classes"]),
            })

    research = [
        packet for packet in packets
        if by_property[packet["property_id"]]["queue"] in {"analyst_research", "crm_research"}
    ]
    backlog = [
        packet for packet in packets
        if by_property[packet["property_id"]]["queue"] == "enrichment_backlog"
    ]
    crm = [
        {
            "projection_version": "nc_dps_crm_projection.v2",
            "external_key": packet["property_id"],
            "pipeline": "acquisition",
            "stage": "research_queue",
            "status": by_property[packet["property_id"]]["route"],
            "priority": packet["scores"]["composite_priority"],
            "property": packet["identity"],
            "county_fips": packet["county_fips"],
            "parcel_id": packet["parcel_id"],
            "recommended_recipe": packet["recipes"]["recommended_recipe"],
            "next_actions": packet["next_research_steps"],
            "source_count": len(packet["source_records"]),
            "write_authorized": False,
        }
        for packet in packets
        if by_property[packet["property_id"]]["queue"] == "crm_research"
    ]

    # Transactions and dispositions remain empty until verified equity and
    # control/title evidence exist. This is an honest gate, not a failed build.
    transactions: list[dict[str, Any]] = []
    dispositions: list[dict[str, Any]] = []

    write_jsonl(output_dir / "research_queue.jsonl", research)
    write_jsonl(output_dir / "enrichment_backlog.jsonl", backlog)
    write_jsonl(output_dir / "crm_projection.jsonl", crm)
    write_jsonl(output_dir / "transaction_projection.jsonl", transactions)
    write_jsonl(output_dir / "disposition_projection.jsonl", dispositions)

    summary = {
        "generated_at": utc_now(),
        "input_signals": len(signals),
        "property_packets": len(packets),
        "research_queue": len(research),
        "enrichment_backlog": len(backlog),
        "crm_projections": len(crm),
        "transaction_projections": 0,
        "disposition_projections": 0,
        "top_priority": packets[0]["scores"]["composite_priority"] if packets else None,
    }
    (output_dir / "engine_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary

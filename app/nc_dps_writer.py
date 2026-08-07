from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

CRM_READY_STATUS = "crm_ready"
QUALIFIED_STATUSES = {"credible_distress", "supporting_context"}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def property_key(signal: dict[str, Any]) -> tuple[str, str] | None:
    geography = str(signal.get("geography") or "").strip()
    parcel = str(signal.get("parcel_apn") or "").strip()
    if not geography or not parcel:
        return None
    return geography, parcel


def qualification_status(signal: dict[str, Any]) -> str:
    return str((signal.get("detail_json") or {}).get("qualification_status") or "")


def validate_inputs(
    signals: list[dict[str, Any]],
    packets: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> None:
    invalid = [
        str(s.get("signal_id") or "<missing-id>")
        for s in signals
        if qualification_status(s) not in QUALIFIED_STATUSES
    ]
    if invalid:
        raise ValueError(
            "NC DPS writer refuses unqualified input signals: "
            f"{invalid[:5]}{' ...' if len(invalid) > 5 else ''}"
        )

    packet_ids = [str(p.get("property_id") or "") for p in packets]
    receipt_ids = [str(r.get("property_id") or "") for r in receipts]
    if len(packet_ids) != len(set(packet_ids)):
        raise ValueError("duplicate property_id in NC DPS property packets")
    if len(receipt_ids) != len(set(receipt_ids)):
        raise ValueError("duplicate property_id in NC DPS score receipts")
    if set(packet_ids) != set(receipt_ids):
        raise ValueError("property packets and score receipts do not have identical property IDs")


def signal_tags(packet: dict[str, Any]) -> list[str]:
    tags = ["nc_dps"]
    for trigger in packet["scores"]["trigger_classes"]:
        tag = f"nc_dps:{trigger}"
        if tag not in tags:
            tags.append(tag)
    return tags


def candidate_payload(
    packet: dict[str, Any],
    receipt: dict[str, Any],
    property_signals: list[dict[str, Any]],
) -> dict[str, Any]:
    qualification = receipt.get("qualification") or {}
    if qualification.get("crm_ready") is not True:
        raise ValueError(f"property {packet['property_id']} is not crm_ready")

    if not any(qualification_status(s) == "credible_distress" for s in property_signals):
        raise ValueError(f"property {packet['property_id']} has no credible distress signal")

    identity = packet["identity"]
    valuation = packet["valuation"]
    ownership = packet["ownership"]
    scores = packet["scores"]

    owner_mailing = identity.get("owner_mailing_address")
    out_of_state = ownership.get("out_of_state") is True
    confidence = max(float(s.get("confidence") or 0) for s in property_signals)

    emerging_signals = {
        "nc_dps": {
            "schema_version": packet["schema_version"],
            "property_id": packet["property_id"],
            "route": receipt["route"],
            "composite_priority": scores["composite_priority"],
            "opportunity_score": scores["opportunity_score"],
            "urgency_score": scores["urgency_score"],
            "evidence_confidence": scores["evidence_confidence"],
            "trigger_classes": scores["trigger_classes"],
            "recommended_recipe": packet["recipes"]["recommended_recipe"],
            "qualification": qualification,
            "score_receipt_sha256": receipt.get("receipt_sha256"),
        }
    }

    raw_meta = {
        "nc_dps": emerging_signals["nc_dps"],
        "signal_tags": signal_tags(packet),
        "amount_due": valuation.get("tax_balance"),
        "owner_out_of_state": out_of_state,
        "source_records": packet["source_records"],
    }

    return {
        "property_id": packet["property_id"],
        "tenant_id": None,
        "county_fips": packet["county_fips"],
        "parcel_apn": packet["parcel_id"],
        "owner_name": identity.get("owner_of_record"),
        "situs_address": identity.get("property_address"),
        "owner_mailing_address": owner_mailing,
        "absentee_flag": True if ownership.get("absentee") is True else None,
        "confidence_score": confidence,
        "emerging_score": float(scores["composite_priority"]),
        "emerging_signals": emerging_signals,
        "raw_meta": raw_meta,
        "source": "nc_dps",
        "provenance": "discovered_nc_dps",
        "signal_ids": [str(s["signal_id"]) for s in property_signals],
    }


def build_candidates(
    signals: list[dict[str, Any]],
    packets: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    validate_inputs(signals, packets, receipts)
    receipts_by_id = {r["property_id"]: r for r in receipts}
    signals_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        key = property_key(signal)
        if key:
            signals_by_key[key].append(signal)

    candidates: list[dict[str, Any]] = []
    blocked: list[str] = []
    for packet in packets:
        receipt = receipts_by_id[packet["property_id"]]
        qualification = receipt.get("qualification") or {}
        if qualification.get("crm_ready") is not True:
            blocked.append(packet["property_id"])
            continue
        key = (str(packet["county_fips"]), str(packet["parcel_id"]))
        property_signals = signals_by_key.get(key, [])
        candidates.append(candidate_payload(packet, receipt, property_signals))

    summary = {
        "qualified_signals": len(signals),
        "property_packets": len(packets),
        "crm_ready_properties": len(candidates),
        "blocked_properties": len(blocked),
        "blocked_property_ids": blocked,
        "crm_writer": "scripts.sync_to_crm",
        "contact_authorized": False,
        "skip_trace_authorized": False,
        "offer_authorized": False,
        "execution_authorized": False,
    }
    return candidates, summary

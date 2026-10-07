from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

RAW_OPPORTUNITY_TYPES = {
    "taxdelinquent",
    "taxsale",
    "tax_foreclosure_case",
    "judgment_lien",
    "vacancy",
    "entity_status",
    "civil_case",
    "code_violation",
    "lis_pendens",
    "probate",
    "pre_probate_death",
    "title_instrument",
}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def property_key(signal: dict[str, Any]) -> tuple[str, str] | None:
    county = str(signal.get("geography") or "").strip()
    parcel = str(signal.get("parcel_apn") or "").strip()
    if not county or not parcel:
        return None
    return county, parcel


def _detail(signal: dict[str, Any]) -> dict[str, Any]:
    value = signal.get("detail_json")
    return value if isinstance(value, dict) else {}


def _first(rows: list[dict[str, Any]], field: str) -> Any:
    for row in rows:
        value = row.get(field)
        if value not in (None, ""):
            return value
    return None


def _first_detail(rows: list[dict[str, Any]], field: str) -> Any:
    for row in rows:
        value = _detail(row).get(field)
        if value not in (None, ""):
            return value
    return None


def _numeric_details(rows: list[dict[str, Any]], field: str) -> list[float]:
    out: list[float] = []
    for row in rows:
        value = _detail(row).get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out.append(float(value))
    return out


def build_raw_candidates(signals: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the broad NC DPS research universe with no qualification threshold.

    Property identity + a recognized public-record signal is sufficient for intake.
    Scores, verified equity, court proof, probate, corroboration, contact data, and
    seller-contact authority are explicitly NOT prerequisites.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    skipped = 0

    for signal in signals:
        if str(signal.get("source_type") or "") not in RAW_OPPORTUNITY_TYPES:
            skipped += 1
            continue
        key = property_key(signal)
        if key is None:
            skipped += 1
            continue
        groups[key].append(signal)

    candidates: list[dict[str, Any]] = []
    for (county_fips, parcel_apn), rows in groups.items():
        rows = sorted(
            rows,
            key=lambda row: (
                str(row.get("source_type") or "") != "title_instrument",
                -float(row.get("confidence") or 0),
            ),
        )

        owner_name = _first(rows, "owner_name")
        situs_address = _first(rows, "raw_address")
        mailing_address = _first_detail(rows, "mailing_address")
        out_of_state_values = [_detail(row).get("out_of_state") for row in rows]
        out_of_state = True if True in out_of_state_values else None
        tax_balances = _numeric_details(rows, "tax_balance")
        tax_years = sorted({
            int(_detail(row)["tax_year"])
            for row in rows
            if isinstance(_detail(row).get("tax_year"), int)
            and not isinstance(_detail(row).get("tax_year"), bool)
        })
        signal_types = sorted({str(row.get("source_type") or "unknown") for row in rows})
        source_records = [
            {
                "signal_id": str(row.get("signal_id") or ""),
                "source_type": str(row.get("source_type") or ""),
                "source_url": _detail(row).get("source_url"),
                "fetched_at": row.get("fetched_at"),
                "confidence": float(row.get("confidence") or 0),
            }
            for row in rows
        ]

        tags = ["nc_dps", "nc_dps:raw_opportunity"]
        tags.extend(f"nc_dps:{source_type}" for source_type in signal_types)
        if out_of_state is True:
            tags.append("nc_dps:out_of_state")

        # Ranked-universe metadata rides along when the operator upload carries
        # it (DEEDSONAR_RAW_IMPORT columns land verbatim in detail_json.raw).
        ranked_meta: dict[str, Any] = {}
        for row in rows:
            raw = _detail(row).get("raw")
            if isinstance(raw, dict) and str(raw.get("recipe_id") or "").strip():
                score_text = str(raw.get("curative_priority_score") or "").strip()
                try:
                    score_value: float | None = float(score_text) if score_text else None
                except ValueError:
                    score_value = None
                ranked_meta = {
                    "recommended_recipe": str(raw.get("recipe_id")).strip(),
                    "curative_priority_score": score_value,
                    "priority_tier": str(raw.get("priority_tier") or "").strip() or None,
                    "trigger_classes": str(raw.get("trigger_classes") or "").strip() or None,
                    "scoring_version": str(raw.get("scoring_version") or "").strip() or None,
                    "recipe_version": str(raw.get("recipe_version") or "").strip() or None,
                }
                break

        emerging_signals = {
            "nc_dps": {
                "stage": "raw_opportunity",
                "county_fips": county_fips,
                "parcel_apn": parcel_apn,
                "signal_types": signal_types,
                "signal_count": len(rows),
                "tax_years": tax_years,
                "tax_balance_observed": sum(tax_balances) if tax_balances else None,
                "out_of_state_observed": out_of_state,
                "qualification_status": "not_yet_qualified",
                "verified_equity": None,
                "recommended_recipe": ranked_meta.get("recommended_recipe"),
                "contact_authorized": False,
            }
        }
        for key_name in (
            "curative_priority_score",
            "priority_tier",
            "trigger_classes",
            "scoring_version",
            "recipe_version",
        ):
            if ranked_meta.get(key_name) is not None:
                emerging_signals["nc_dps"][key_name] = ranked_meta[key_name]

        candidates.append({
            "property_id": f"NC:{county_fips}:{parcel_apn}",
            "county_fips": county_fips,
            "parcel_apn": parcel_apn,
            "owner_name": owner_name,
            "situs_address": situs_address,
            "owner_mailing_address": mailing_address,
            "absentee_flag": out_of_state,
            "confidence_score": max(float(row.get("confidence") or 0) for row in rows),
            "emerging_score": 0.0,
            "emerging_signals": emerging_signals,
            "raw_meta": {
                "nc_dps": emerging_signals["nc_dps"],
                "signal_tags": list(dict.fromkeys(tags)),
                "amount_due": emerging_signals["nc_dps"]["tax_balance_observed"],
                "owner_out_of_state": out_of_state,
                "source_records": source_records,
                "intake_mode": "raw_opportunity",
                "score_threshold_required": False,
                "contact_authorized": False,
            },
            "source": "nc_dps_raw",
            "provenance": "discovered_nc_dps_raw",
            "signal_ids": [str(row.get("signal_id") or "") for row in rows if row.get("signal_id")],
        })

    candidates.sort(key=lambda row: (row["county_fips"], row["parcel_apn"]))
    summary = {
        "source_signals": len(signals),
        "raw_opportunity_properties": len(candidates),
        "skipped_signals": skipped,
        "score_threshold_required": False,
        "qualification_required_for_intake": False,
        "contact_authorized": False,
        "crm_stage": "discovered",
    }
    return candidates, summary

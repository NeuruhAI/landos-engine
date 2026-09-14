#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from scripts import build_nc_dps_public_enrichment as core


def query_variants(value: Any) -> set[str]:
    raw = str(value or "").strip().upper().replace('="', '').replace('"', '')
    if not raw or raw.startswith(("ACCOUNT:", "ADDR:")):
        return set()
    normalized = core.norm_parcel(raw)
    return {x for x in (raw, normalized) if x}


def run(signals_path: Path, meck_path: Path | None, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    original_signals = core.read_jsonl(signals_path)
    signals = list(original_signals)
    meck = core.read_jsonl(meck_path)

    desired: dict[str, set[str]] = defaultdict(set)
    for signal in signals:
        fips = str(signal.get("geography") or "")
        if fips:
            desired[fips].update(query_variants(signal.get("parcel_apn")))

    parcel_context: dict[tuple[str, str], dict[str, Any]] = {}
    source_errors: list[str] = []
    multi_claimed: dict[str, int] = {}
    for fips, ids in sorted(desired.items()):
        try:
            records = core.query_ids(fips, sorted(ids))
            multi_claimed[fips] = core.index_parcel_context(parcel_context, fips, records)
            print(f"ONEMAP_{fips}_MATCHED_RECORDS={len(records)}")
        except Exception as exc:
            source_errors.append(f"{fips}:{type(exc).__name__}:{exc}")
            print(f"ONEMAP_{fips}_ERROR={exc}")

    meck_unresolved_report: list[dict[str, Any]] = []
    meck_signals: list[dict[str, Any]] = []
    if meck:
        target_names = {
            core.norm_owner(r.get("owner_name"))
            for r in meck
            if core.norm_owner(r.get("owner_name"))
        }
        try:
            matches = core.scan_county_for_owners("37119", target_names)
            meck_signals, meck_unresolved_report = core.build_mecklenburg_signals(meck, matches)
            signals.extend(meck_signals)
            multi_claimed["37119-owner-match"] = core.index_parcel_context(
                parcel_context, "37119", [record for candidates in matches.values() for record in candidates])
        except Exception as exc:
            source_errors.append(f"37119-owner-match:{type(exc).__name__}:{exc}")
            print(f"MECK_ONEMAP_ERROR={exc}")

    rows = core.aggregate(signals, parcel_context)
    rows.sort(key=lambda r: (r["county_fips"], str(r["parcel_apn"])))
    core.write_csv(output_dir / "LANDOS_ENRICHED_OPPORTUNITIES.csv", rows)
    core.write_jsonl(output_dir / "COMBINED_WITH_MECK_MATCHES.jsonl", signals)
    if meck_unresolved_report:
        core.write_csv(output_dir / "MECKLENBURG_UNRESOLVED_PROPERTY_MATCHES.csv", meck_unresolved_report)

    summary = {
        "source_signals": len(original_signals),
        "mecklenburg_staging_rows": len(meck),
        "mecklenburg_unique_property_matches": len(meck_signals),
        "enriched_properties": len(rows),
        "onemap_matched_properties": sum(1 for r in rows if r["parcel_match_status"] == "ONEMAP_MATCHED"),
        "out_of_state_properties": sum(1 for r in rows if r["out_of_state"] is True),
        "twenty_plus_year_ownership": sum(
            1 for r in rows if isinstance(r["years_owned"], int) and r["years_owned"] >= 20
        ),
        "heir_signal_properties": sum(1 for r in rows if r["heir_indicator"] is True),
        "tax_foreclosure_properties": sum(1 for r in rows if r["tax_foreclosure_indicator"] is True),
        "equity_verified_properties": 0,
        "equity_status": "UNKNOWN_UNTIL_DEBT_INSTRUMENT_SEARCH",
        "contact_authorized": False,
        "source_errors": source_errors,
        "onemap_keys_claimed_by_several_records": multi_claimed,
        "output": str(output_dir / "LANDOS_ENRICHED_OPPORTUNITIES.csv"),
    }
    (output_dir / "LANDOS_ENRICHMENT_SUMMARY.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--signals", required=True, type=Path)
    p.add_argument("--mecklenburg-taxpayers", type=Path)
    p.add_argument("--output", required=True, type=Path)
    args = p.parse_args()
    print(json.dumps(run(args.signals, args.mecklenburg_taxpayers, args.output), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

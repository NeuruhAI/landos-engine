#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts import build_nc_dps_public_enrichment as core
from scripts import build_nc_dps_public_enrichment_v2 as v2

# Current NC OneMap service host. POST is required for large parcel-id batches;
# long GET query strings were returning 404 for Davidson/Orange/Union.
ONEMAP = "https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/FeatureServer/0/query"


def _arcgis_date_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        try:
            # ArcGIS date fields are milliseconds since Unix epoch.
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).date().isoformat()
        except (ValueError, OSError, OverflowError):
            return ""
    return str(value).strip()


def request_json_post(params: dict[str, Any], *, attempts: int = 4) -> dict[str, Any]:
    payload_params = dict(params)
    payload_params.setdefault("f", "json")
    encoded = urllib.parse.urlencode(payload_params).encode("utf-8")
    last: Exception | None = None
    for attempt in range(attempts):
        req = urllib.request.Request(
            ONEMAP,
            data=encoded,
            method="POST",
            headers={
                "User-Agent": core.UA,
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if "error" in payload:
                raise RuntimeError(str(payload["error"]))
            for feature in payload.get("features", []):
                attrs = feature.get("attributes") or {}
                if not attrs.get("saledatetx") and attrs.get("saledate") not in (None, ""):
                    attrs["saledatetx"] = _arcgis_date_text(attrs.get("saledate"))
            return payload
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.25 * (attempt + 1))
    raise RuntimeError(f"NC OneMap POST query failed after {attempts} attempts: {last}")


def _institutional_owner(text: str) -> bool:
    return bool(re.search(
        r"\b(?:REAL\s+ESTATE|HOLDINGS?|LLC|L\.L\.C|INC|CORP(?:ORATION)?|COMPANY|BANK|AUTHORITY|CHURCH|ASSOCIATION)\b",
        text,
    ))


def heir_flag_strict(owner: str, rows: list[dict[str, Any]]) -> bool:
    text = re.sub(r"\s+", " ", (owner or "").upper()).strip()
    if any(core.detail(r).get("heir_indicator") is True for r in rows):
        return True
    if re.search(r"\bHEIRS?\b", text):
        return True
    if re.search(r"\bESTATE\s+OF\b", text):
        return True
    # A standalone ESTATE can be probative, but REAL ESTATE is not an estate/death signal.
    if re.search(r"\bESTATE\b", text) and not re.search(r"\bREAL\s+ESTATE\b", text):
        return True
    # HRS is a common county abbreviation for heirs, but can also be a business acronym.
    if re.search(r"\bHRS\b", text) and not _institutional_owner(text):
        return True
    return False


def fragmented_flag_strict(owner: str) -> bool:
    text = re.sub(r"\s+", " ", (owner or "").upper()).strip()
    institutional = _institutional_owner(text)
    if re.search(r"\bET\s+ALS?\b|\bETALS\b|\bHEIRS?\b", text):
        return True
    if re.search(r"\bHRS\b", text) and not institutional:
        return True
    if " & " in text and not institutional:
        return True
    return False


_DIRECTIONALS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW", "NORTH", "SOUTH", "EAST", "WEST"}


def situs_key(text: Any) -> tuple[str, str] | None:
    """Conservative street-number + street-name key for situs comparison.

    Mecklenburg's publication prefixes an optional unit number before the
    street number ("127 601 N TRYON ST"), so the last numeric token before the
    first non-directional street token is the street number.
    """
    cleaned = re.sub(r"[^A-Z0-9 ]", " ", str(text or "").upper())
    numbers: list[str] = []
    street = None
    for token in cleaned.split():
        if token.isdigit():
            numbers.append(token)
            continue
        if token in _DIRECTIONALS:
            continue
        street = token
        break
    if not numbers or not street:
        return None
    return numbers[-1], street


def build_mecklenburg_signals_addr(
    unresolved: list[dict[str, Any]],
    owner_matches: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Owner matching with publication-address corroboration.

    Upgrades the unique-owner rule: a unique owner match with a conflicting
    publication address is refused instead of minted, and an owner holding
    several parcels resolves when exactly one candidate parcel's site address
    matches the publication address.
    """
    import hashlib

    signals: list[dict[str, Any]] = []
    unresolved_report: list[dict[str, Any]] = []
    for row in unresolved:
        key = core.norm_owner(row.get("owner_name"))
        candidates = owner_matches.get(key, []) if key else []
        unique_by_parcel: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            parcel = str(candidate.get("parno") or candidate.get("altparno") or "").strip()
            if parcel:
                unique_by_parcel[core.norm_parcel(parcel)] = candidate
        pub_key = situs_key(row.get("property_address"))

        chosen: dict[str, Any] | None = None
        match_basis = ""
        confidence = 0.0
        if len(unique_by_parcel) == 1:
            one = next(iter(unique_by_parcel.values()))
            cand_key = situs_key(one.get("siteadd"))
            if pub_key and cand_key and pub_key != cand_key:
                unresolved_report.append({
                    "owner_name": row.get("owner_name"),
                    "tax_account_or_bill": row.get("tax_account_or_bill"),
                    "tax_balance": row.get("tax_balance"),
                    "candidate_parcel_count": 1,
                    "status": "ADDRESS_CONFLICT_ON_UNIQUE_OWNER_MATCH",
                    "source_url": row.get("source_url"),
                })
                continue
            chosen = one
            corroborated = bool(pub_key and cand_key and pub_key == cand_key)
            match_basis = (
                "unique_owner_match_address_corroborated"
                if corroborated
                else "unique_normalized_owner_to_onemap_parcel"
            )
            confidence = 0.88 if corroborated else 0.72
        elif len(unique_by_parcel) > 1 and pub_key:
            addr_hits = [
                c for c in unique_by_parcel.values()
                if situs_key(c.get("siteadd")) == pub_key
            ]
            if len(addr_hits) == 1:
                chosen = addr_hits[0]
                match_basis = "owner_plus_address_match"
                confidence = 0.80

        if chosen is None:
            unresolved_report.append({
                "owner_name": row.get("owner_name"),
                "tax_account_or_bill": row.get("tax_account_or_bill"),
                "tax_balance": row.get("tax_balance"),
                "candidate_parcel_count": len(unique_by_parcel),
                "status": "NO_UNIQUE_PROPERTY_MATCH",
                "source_url": row.get("source_url"),
            })
            continue

        parcel = str(chosen.get("parno") or chosen.get("altparno") or "").strip()
        sid_seed = f"meck-owner-match|{row.get('staging_id')}|{parcel}"
        signals.append({
            "signal_id": hashlib.sha256(sid_seed.encode()).hexdigest(),
            "source": "official_distress:mecklenburg:unique_owner_parcel_match",
            "source_type": "taxdelinquent",
            "geography": "37119",
            "event_date": row.get("source_as_of") or "2026-08-04",
            "raw_address": chosen.get("siteadd") or "",
            "parcel_apn": parcel,
            "owner_name": chosen.get("ownname") or row.get("owner_name") or "",
            "detail_json": {
                "observation_kind": "published_tax_distress_property_matched",
                "county": "Mecklenburg",
                "county_fips": "37119",
                "tax_balance": row.get("tax_balance"),
                "source_url": row.get("source_url"),
                "source_label": row.get("source_label"),
                "source_as_of": row.get("source_as_of"),
                "publication_property_address": row.get("property_address"),
                "match_basis": match_basis,
                "match_confidence": confidence,
                "contact_authorized": False,
            },
            "confidence": confidence,
            "fetched_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        })
    return signals, unresolved_report


def read_csv(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def run(
    signals_path: Path,
    output_dir: Path,
    reuse_mecklenburg_csv: Path | None = None,
    mecklenburg_taxpayers: Path | None = None,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)

    # Patch the existing v2 engine at its network and classification seams only.
    core.ONEMAP = ONEMAP
    if "saledate" not in core.OUT_FIELDS.split(","):
        core.OUT_FIELDS += ",saledate"
    core.request_json = request_json_post
    core.heir_flag = heir_flag_strict
    core.fragmented_flag = fragmented_flag_strict
    core.build_mecklenburg_signals = build_mecklenburg_signals_addr

    fresh_dir = output_dir / "fresh_non_mecklenburg"
    fresh_summary = v2.run(signals_path, mecklenburg_taxpayers, fresh_dir)
    fresh_rows = read_csv(fresh_dir / "LANDOS_ENRICHED_OPPORTUNITIES.csv")

    reused_meck: list[dict[str, Any]] = []
    if mecklenburg_taxpayers:
        # Fresh Mecklenburg owner-scan matching already ran inside v2 with the
        # strict classifiers patched in; keep those rows and skip any reuse CSV.
        combined = fresh_rows
    else:
        if reuse_mecklenburg_csv and reuse_mecklenburg_csv.exists():
            reused_meck = [
                row for row in read_csv(reuse_mecklenburg_csv)
                if str(row.get("county_fips") or "") == "37119"
            ]
            # Repair the two classification fields on reused rows as well.
            for row in reused_meck:
                row["heir_indicator"] = heir_flag_strict(str(row.get("owner_name") or ""), [])
                row["fragmented_ownership_indicator"] = fragmented_flag_strict(str(row.get("owner_name") or ""))

        combined = [
            row for row in fresh_rows
            if str(row.get("county_fips") or "") != "37119"
        ] + reused_meck
    combined.sort(key=lambda r: (str(r.get("county_fips") or ""), str(r.get("parcel_apn") or "")))
    core.write_csv(output_dir / "LANDOS_ENRICHED_OPPORTUNITIES.csv", combined)

    summary = {
        "source_signals": fresh_summary.get("source_signals"),
        "enriched_properties": len(combined),
        "onemap_matched_properties": sum(1 for r in combined if r.get("parcel_match_status") == "ONEMAP_MATCHED"),
        "out_of_state_properties": sum(1 for r in combined if _truthy(r.get("out_of_state"))),
        "twenty_plus_year_ownership": sum(
            1 for r in combined if (_int_or_none(r.get("years_owned")) or -1) >= 20
        ),
        "heir_signal_properties": sum(1 for r in combined if _truthy(r.get("heir_indicator"))),
        "tax_foreclosure_properties": sum(1 for r in combined if _truthy(r.get("tax_foreclosure_indicator"))),
        "reused_mecklenburg_properties": len(reused_meck),
        "mecklenburg_staging_rows": fresh_summary.get("mecklenburg_staging_rows", 0),
        "fresh_mecklenburg_property_matches": fresh_summary.get("mecklenburg_unique_property_matches", 0),
        "equity_verified_properties": 0,
        "equity_status": "UNKNOWN_UNTIL_DEBT_INSTRUMENT_SEARCH",
        "contact_authorized": False,
        "source_errors": fresh_summary.get("source_errors", []),
        "onemap_transport": "POST",
        "onemap_service": ONEMAP,
        "output": str(output_dir / "LANDOS_ENRICHED_OPPORTUNITIES.csv"),
    }
    (output_dir / "LANDOS_ENRICHMENT_SUMMARY.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--signals", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--reuse-mecklenburg-csv", type=Path)
    p.add_argument("--mecklenburg-taxpayers", type=Path)
    args = p.parse_args()
    print(json.dumps(
        run(args.signals, args.output, args.reuse_mecklenburg_csv, args.mecklenburg_taxpayers),
        indent=2,
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()

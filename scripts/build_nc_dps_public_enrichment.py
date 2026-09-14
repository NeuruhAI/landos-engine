#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

ONEMAP = "https://services.gis.nc.gov/secure/rest/services/NC1Map_Parcels/FeatureServer/0/query"
UA = "Neuruh-NC-DPS-Curative/1.0 (+official-public-record-research)"
PAGE_SIZE = 5000
COUNTY_NAMES = {
    "37057": "Davidson",
    "37119": "Mecklenburg",
    "37135": "Orange",
    "37179": "Union",
}
OUT_FIELDS = ",".join([
    "objectid", "parno", "altparno", "ownname", "ownname2", "owntype",
    "parval", "landval", "improvval", "mailadd", "mcity", "mstate", "mzip",
    "siteadd", "scity", "szip", "gisacres", "struct", "structno", "structyear",
    "saledatetx", "parusedesc", "sourceref", "sourcedatx", "stcntyfips",
])


def read_jsonl(path: Path | None) -> list[dict[str, Any]]:
    if not path or not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def norm_parcel(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("ACCOUNT:") or text.startswith("ADDR:"):
        return text
    text = text.replace('="', "").replace('"', "")
    return re.sub(r"[^A-Z0-9]", "", text)


def norm_owner(value: Any) -> str:
    text = str(value or "").upper()
    text = re.sub(r"\b(JR|SR|II|III|IV)\b", " ", text)
    return re.sub(r"[^A-Z0-9]", "", text)


def number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        text = re.sub(r"[^0-9.\-]", "", str(value))
        try:
            return float(text) if text not in ("", ".", "-") else None
        except ValueError:
            return None


def request_json(params: dict[str, Any], *, attempts: int = 4) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        ONEMAP + "?" + query,
        headers={"User-Agent": UA, "Accept": "application/json"},
    )
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            if "error" in payload:
                raise RuntimeError(str(payload["error"]))
            return payload
        except Exception as exc:  # network boundary
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.25 * (attempt + 1))
    raise RuntimeError(f"NC OneMap query failed after {attempts} attempts: {last}")


def query_ids(fips: str, ids: list[str]) -> list[dict[str, Any]]:
    """Query only the parcel ids already proven by distress sources."""
    results: list[dict[str, Any]] = []
    clean = [x for x in ids if x and not x.startswith(("ACCOUNT:", "ADDR:"))]
    for start in range(0, len(clean), 120):
        chunk = clean[start:start + 120]
        literals = ",".join("'" + x.replace("'", "''") + "'" for x in chunk)
        where = f"stcntyfips='{fips}' AND (parno IN ({literals}) OR altparno IN ({literals}))"
        payload = request_json({
            "f": "json",
            "where": where,
            "outFields": OUT_FIELDS,
            "returnGeometry": "false",
            "resultRecordCount": "5000",
        })
        results.extend(dict(x.get("attributes") or {}) for x in payload.get("features", []))
    return results


def scan_county_for_owners(fips: str, target_owners: set[str]) -> dict[str, list[dict[str, Any]]]:
    """Stream one county and retain only parcels whose normalized owner is wanted.

    This is used for Mecklenburg because the county delinquent-taxpayer publication
    can be taxpayer-level rather than parcel-level. A property is asserted only
    when that taxpayer name resolves to exactly one OneMap parcel.
    """
    matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not target_owners:
        return matches

    offset = 0
    while True:
        payload = request_json({
            "f": "json",
            "where": f"stcntyfips='{fips}'",
            "outFields": OUT_FIELDS,
            "returnGeometry": "false",
            "orderByFields": "objectid",
            "resultOffset": str(offset),
            "resultRecordCount": str(PAGE_SIZE),
        })
        features = payload.get("features", [])
        if not features:
            break
        for feature in features:
            row = dict(feature.get("attributes") or {})
            key = norm_owner(row.get("ownname"))
            if key in target_owners:
                matches[key].append(row)
        offset += len(features)
        print(f"MECK_ONEMAP_SCANNED={offset}")
        if len(features) < PAGE_SIZE:
            break
    return matches


# A county roll with no sale on record still fills its sale-date column:
# 12/30/1899 is OLE-automation day 0 (Durham), 1/1/1900 is spreadsheet day 1
# (Union), 1900-01-01 an ArcGIS date at that day (Mecklenburg). Read as dates,
# every such parcel was "owned 126 years" and earned ownership_20_plus_years in
# the ranker. On the 2026-09-13 factory run all 1,830 sale dates on or before
# 1900-01-01 were one of those three values; the next earliest was 1921. A
# placeholder is no sale date, so years_owned stays UNKNOWN.
NO_SALE_ON_RECORD_BEFORE = date(1900, 1, 2)


def parse_years_owned(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [
        "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d", "%m-%d-%Y",
    ]
    dt = None
    for fmt in candidates:
        try:
            dt = datetime.strptime(text[:10], fmt).date()
            break
        except ValueError:
            pass
    if dt is None:
        m = re.search(r"(19|20)\d{2}", text)
        if m:
            dt = date(int(m.group(0)), 1, 1)
    if dt is None or dt < NO_SALE_ON_RECORD_BEFORE:
        return None
    today = date(2026, 8, 7)
    years = today.year - dt.year - ((today.month, today.day) < (dt.month, dt.day))
    return max(0, years)


def detail(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("detail_json")
    return value if isinstance(value, dict) else {}


_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

FACTS_BASIS_OBSERVED = "observed_event_date"
FACTS_BASIS_UNKNOWN = "unknown"


def _iso_date(value: Any) -> date | None:
    match = _ISO_DATE.match(str(value or "").strip())
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def facts_as_of(signals: list[dict[str, Any]]) -> tuple[str, str]:
    """The property's FACTS date: the newest date a county payload carried about it.

    The same rule as landos-ingest source_factory/freshness.presentation_date, cut
    to its one trustworthy basis: detail_json.observed_event_date, which the
    factory records only when the payload dates itself (Wake's tape_creation_date).
    Nothing else may stand in for it -- not the adapter's declared as_of constant,
    not event_date, not retrieved_at/fetched_at, and not a build or export time.
    Without an observed date the answer is ("", "unknown"), never today.

    A payload cannot be dated after it was retrieved, so an observed date later
    than the signal's own retrieved_at is refused rather than trusted.
    """
    newest: date | None = None
    for signal in signals:
        d = detail(signal)
        observed = _iso_date(d.get("observed_event_date"))
        if observed is None:
            continue
        retrieved = _iso_date(d.get("retrieved_at"))
        if retrieved is not None and observed > retrieved:
            continue
        if newest is None or observed > newest:
            newest = observed
    if newest is None:
        return "", FACTS_BASIS_UNKNOWN
    return newest.isoformat(), FACTS_BASIS_OBSERVED


def heir_flag(owner: str, rows: list[dict[str, Any]]) -> bool:
    text = owner.upper()
    if any(token in text for token in (" HEIR", "HEIRS", " HRS", "ESTATE OF", " ESTATE")):
        return True
    return any(detail(r).get("heir_indicator") is True for r in rows)


def fragmented_flag(owner: str) -> bool:
    text = owner.upper()
    return any(token in text for token in (" ET AL", "ETALS", " ET ALS", " HEIRS", " HRS", "&"))


def combine_mailing(one: dict[str, Any]) -> str:
    parts = [one.get("mailadd"), one.get("mcity"), one.get("mstate"), one.get("mzip")]
    return ", ".join(str(x).strip() for x in parts if x not in (None, ""))


def build_mecklenburg_signals(
    unresolved: list[dict[str, Any]],
    owner_matches: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    signals: list[dict[str, Any]] = []
    unresolved_report: list[dict[str, Any]] = []
    for row in unresolved:
        key = norm_owner(row.get("owner_name"))
        candidates = owner_matches.get(key, []) if key else []
        unique_by_parcel: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            parcel = str(candidate.get("parno") or candidate.get("altparno") or "").strip()
            if parcel:
                unique_by_parcel[norm_parcel(parcel)] = candidate
        if len(unique_by_parcel) != 1:
            unresolved_report.append({
                "owner_name": row.get("owner_name"),
                "tax_account_or_bill": row.get("tax_account_or_bill"),
                "tax_balance": row.get("tax_balance"),
                "candidate_parcel_count": len(unique_by_parcel),
                "status": "NO_UNIQUE_PROPERTY_MATCH",
                "source_url": row.get("source_url"),
            })
            continue
        one = next(iter(unique_by_parcel.values()))
        parcel = str(one.get("parno") or one.get("altparno") or "").strip()
        sid_seed = f"meck-owner-match|{row.get('staging_id')}|{parcel}"
        import hashlib
        signals.append({
            "signal_id": hashlib.sha256(sid_seed.encode()).hexdigest(),
            "source": "official_distress:mecklenburg:unique_owner_parcel_match",
            "source_type": "taxdelinquent",
            "geography": "37119",
            # The publication's declared as_of, or nothing. Never a literal: the
            # old "2026-08-04" fallback dated every unlabelled row to one day.
            "event_date": row.get("source_as_of") or None,
            "raw_address": one.get("siteadd") or "",
            "parcel_apn": parcel,
            "owner_name": one.get("ownname") or row.get("owner_name") or "",
            "detail_json": {
                "observation_kind": "published_tax_distress_property_matched",
                "county": "Mecklenburg",
                "county_fips": "37119",
                "tax_balance": row.get("tax_balance"),
                "source_url": row.get("source_url"),
                "source_label": row.get("source_label"),
                "source_as_of": row.get("source_as_of"),
                "match_basis": "unique_normalized_owner_to_onemap_parcel",
                "match_confidence": 0.72,
                "contact_authorized": False,
            },
            "confidence": 0.72,
            "fetched_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
        })
    return signals, unresolved_report


def index_parcel_context(
    parcel_context: dict[tuple[str, str], dict[str, Any]],
    fips: str,
    records: Iterable[dict[str, Any]],
) -> int:
    """Key each OneMap record by its parno and its altparno.

    A number that is one record's parno and another record's altparno resolves
    to the parno record (the parcel filed under that number), whatever order
    OneMap answered in. Returns how many keys more than one record claims
    through the same field; the last claimant keeps the key, as before.
    """
    by_field: dict[str, dict[str, list[dict[str, Any]]]] = {"parno": defaultdict(list), "altparno": defaultdict(list)}
    for record in records:
        for field in ("parno", "altparno"):
            key = norm_parcel(record.get(field))
            if key:
                by_field[field][key].append(record)
    multi_claimed = 0
    for field in ("altparno", "parno"):  # parno last, so it wins a cross-field tie
        for key, claimants in by_field[field].items():
            multi_claimed += len(claimants) > 1
            parcel_context[(fips, key)] = claimants[-1]
    return multi_claimed


def parcel_identity(pkey: str, raw_parcel: str, one: dict[str, Any]) -> tuple[str, str]:
    """(parcel_apn, alternate_parcel) for the source group keyed pkey.

    The identity is the parcel number the SOURCE names. When OneMap files the
    parcel under that same number (parno), OneMap's spelling of it is kept.
    When the source number matched OneMap's altparno instead, the two numbers
    name different things: Durham bills each taxable unit by REID, and OneMap's
    parno there is the PIN every condo unit on one footprint shares. Re-keying
    to the PIN merged distinct units under one key (2026-09-13: 12 duplicate
    37063 keys, refused at export) and moved 1,951 Durham rows off the REID the
    DeedSonar estate keys them by. The source number stays the identity and
    OneMap's parno rides along as alternate_parcel. Either way the emitted
    parcel_apn normalizes to pkey, so one group is one key.
    """
    parno = str(one.get("parno") or "").strip()
    if parno and norm_parcel(parno) == pkey:
        return parno, str(one.get("altparno") or "")
    return raw_parcel, parno


def duplicate_parcel_keys(rows: Iterable[dict[str, Any]]) -> list[tuple[str, str]]:
    """(county_fips, normalized parcel_apn) keys carried by more than one row."""
    counts = Counter((str(r.get("county_fips") or ""), norm_parcel(r.get("parcel_apn"))) for r in rows)
    return sorted(k for k, n in counts.items() if n > 1)


def aggregate(
    signals: list[dict[str, Any]],
    parcel_context: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for signal in signals:
        fips = str(signal.get("geography") or "")
        parcel = str(signal.get("parcel_apn") or "")
        if fips and parcel:
            groups[(fips, norm_parcel(parcel))].append(signal)

    rows: list[dict[str, Any]] = []
    for (fips, pkey), sigs in groups.items():
        raw_parcel = str(sigs[0].get("parcel_apn") or "")
        one = parcel_context.get((fips, pkey), {})
        parcel_apn, alternate_parcel = parcel_identity(pkey, raw_parcel, one)
        owner = str(one.get("ownname") or next((s.get("owner_name") for s in sigs if s.get("owner_name")), "") or "")
        site = str(one.get("siteadd") or next((s.get("raw_address") for s in sigs if s.get("raw_address")), "") or "")
        mailing = combine_mailing(one)
        mstate = str(one.get("mstate") or "").strip().upper()
        out_state = True if mstate and mstate != "NC" else False if mstate == "NC" else None
        assessed = number(one.get("parval"))
        land = number(one.get("landval"))
        improvements = number(one.get("improvval"))
        years_owned = parse_years_owned(one.get("saledatetx"))
        tax_balances = [number(detail(s).get("tax_balance")) for s in sigs]
        tax_balances = [x for x in tax_balances if x is not None]
        tax_balance = sum(tax_balances) if tax_balances else None
        tax_years = sorted({
            int(detail(s)["tax_year"])
            for s in sigs
            if isinstance(detail(s).get("tax_year"), int) and not isinstance(detail(s).get("tax_year"), bool)
        })
        types = sorted({str(s.get("source_type") or "") for s in sigs if s.get("source_type")})
        urls = sorted({str(detail(s).get("source_url")) for s in sigs if detail(s).get("source_url")})
        cases: list[str] = []
        for s in sigs:
            value = detail(s).get("court_case_numbers")
            if isinstance(value, list):
                cases.extend(str(x) for x in value if x)
        cases = sorted(set(cases))
        # The owner column prefers OneMap's current ownname, but the heir and
        # co-ownership proxies read every owner text a county record gave us.
        # OneMap's ownname drops the co-owner "&" in Wake ("LEACH, SHELIA LEACH,
        # HILLERY O" for the bill's "LEACH, SHELIA & LEACH, HILLERY O") and the
        # HEIRS suffix in Durham; read alone it erased 686 Wake co-ownerships of
        # the same people on the 2026-09-13 run.
        source_owners = sorted({str(s.get("owner_name")) for s in sigs if s.get("owner_name")})
        heir = heir_flag(owner, sigs) or any(heir_flag(o, []) for o in source_owners)
        fragmented = fragmented_flag(owner) or any(fragmented_flag(o) for o in source_owners)
        assessed_less_tax = assessed - tax_balance if assessed is not None and tax_balance is not None else None
        tax_ratio = tax_balance / assessed if assessed and tax_balance is not None else None
        matched = bool(one)
        facts_date, facts_basis = facts_as_of(sigs)

        rows.append({
            "county": COUNTY_NAMES.get(fips, fips),
            "county_fips": fips,
            "parcel_apn": parcel_apn,
            "alternate_parcel": alternate_parcel,
            "owner_name": owner,
            "owner_name_2": str(one.get("ownname2") or ""),
            "owner_type": str(one.get("owntype") or ""),
            "property_address": site,
            "property_city": str(one.get("scity") or ""),
            "property_zip": str(one.get("szip") or ""),
            "owner_mailing_address": mailing,
            "owner_mailing_city": str(one.get("mcity") or ""),
            "owner_mailing_state": mstate,
            "owner_mailing_zip": str(one.get("mzip") or ""),
            "out_of_state": out_state,
            "assessed_value": assessed,
            "land_value": land,
            "improvement_value": improvements,
            "assessed_value_less_tax": assessed_less_tax,
            "tax_to_assessed_ratio": tax_ratio,
            "tax_balance": tax_balance,
            "tax_years": "|".join(str(x) for x in tax_years),
            "last_sale_date": str(one.get("saledatetx") or ""),
            "years_owned": years_owned,
            "acreage": number(one.get("gisacres")),
            "structure_count": one.get("structno"),
            "structure_year": one.get("structyear"),
            "property_use": str(one.get("parusedesc") or ""),
            "source_document_ref": str(one.get("sourceref") or ""),
            "source_document_date": str(one.get("sourcedatx") or ""),
            "source_types": "|".join(types),
            "signal_count": len(sigs),
            "court_case_numbers": "|".join(cases),
            "heir_indicator": heir,
            "fragmented_ownership_indicator": fragmented,
            "tax_foreclosure_indicator": "tax_foreclosure_case" in types,
            "parcel_match_status": "ONEMAP_MATCHED" if matched else "SOURCE_ONLY",
            "source_url": urls[0] if urls else "",
            "source_urls": "|".join(urls),
            "mortgage_balance": "",
            "judgment_lien_balance": "",
            "estimated_equity": "",
            "equity_status": "UNKNOWN_NEEDS_DEBT_SEARCH",
            "stage": "RAW_OPPORTUNITY_ENRICHED",
            "contact_authorized": False,
            # Appended last so every existing column keeps its position.
            "facts_as_of": facts_date,
            "facts_as_of_basis": facts_basis,
        })
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if fields:
            writer.writeheader()
            writer.writerows(rows)


def run(signals_path: Path, meck_path: Path | None, output_dir: Path) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    signals = read_jsonl(signals_path)
    meck = read_jsonl(meck_path)

    desired: dict[str, set[str]] = defaultdict(set)
    for signal in signals:
        fips = str(signal.get("geography") or "")
        parcel = norm_parcel(signal.get("parcel_apn"))
        if fips and parcel and not parcel.startswith(("ACCOUNT:", "ADDR:")):
            desired[fips].add(parcel)

    parcel_context: dict[tuple[str, str], dict[str, Any]] = {}
    source_errors: list[str] = []
    for fips, ids in sorted(desired.items()):
        try:
            records = query_ids(fips, sorted(ids))
            index_parcel_context(parcel_context, fips, records)
            print(f"ONEMAP_{fips}_MATCHED_RECORDS={len(records)}")
        except Exception as exc:
            source_errors.append(f"{fips}:{type(exc).__name__}:{exc}")
            print(f"ONEMAP_{fips}_ERROR={exc}")

    meck_unresolved_report: list[dict[str, Any]] = []
    meck_signals: list[dict[str, Any]] = []
    if meck:
        target_names = {norm_owner(r.get("owner_name")) for r in meck if norm_owner(r.get("owner_name"))}
        try:
            matches = scan_county_for_owners("37119", target_names)
            meck_signals, meck_unresolved_report = build_mecklenburg_signals(meck, matches)
            signals.extend(meck_signals)
            index_parcel_context(parcel_context, "37119",
                                 [record for candidates in matches.values() for record in candidates])
        except Exception as exc:
            source_errors.append(f"37119-owner-match:{type(exc).__name__}:{exc}")
            print(f"MECK_ONEMAP_ERROR={exc}")

    rows = aggregate(signals, parcel_context)
    rows.sort(
        key=lambda r: (
            r["county_fips"],
            str(r["parcel_apn"]),
        )
    )
    write_csv(output_dir / "LANDOS_ENRICHED_OPPORTUNITIES.csv", rows)
    write_jsonl(output_dir / "COMBINED_WITH_MECK_MATCHES.jsonl", signals)
    if meck_unresolved_report:
        write_csv(output_dir / "MECKLENBURG_UNRESOLVED_PROPERTY_MATCHES.csv", meck_unresolved_report)

    summary = {
        "source_signals": len(read_jsonl(signals_path)),
        "mecklenburg_staging_rows": len(meck),
        "mecklenburg_unique_property_matches": len(meck_signals),
        "enriched_properties": len(rows),
        "onemap_matched_properties": sum(1 for r in rows if r["parcel_match_status"] == "ONEMAP_MATCHED"),
        "out_of_state_properties": sum(1 for r in rows if r["out_of_state"] is True),
        "twenty_plus_year_ownership": sum(1 for r in rows if isinstance(r["years_owned"], int) and r["years_owned"] >= 20),
        "heir_signal_properties": sum(1 for r in rows if r["heir_indicator"] is True),
        "tax_foreclosure_properties": sum(1 for r in rows if r["tax_foreclosure_indicator"] is True),
        "equity_verified_properties": 0,
        "equity_status": "UNKNOWN_UNTIL_DEBT_INSTRUMENT_SEARCH",
        "contact_authorized": False,
        "source_errors": source_errors,
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

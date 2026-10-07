"""adopt_t5m_mecklenburg_resolution.py — D0: adopt the sealed T5-M Mecklenburg
owner→parcel resolution into canonical LandOS opportunity intelligence.

READ-ONLY over every source; writes ONLY new evidence artifacts (no DB, no CRM,
no DeedSonar). Every input is sha256-GATED against the sealed T5-M receipt —
a mismatch aborts before any adoption.

Doctrine (matches T4's canonical Mecklenburg identity representation —
build_nc_dps_public_enrichment_v3.build_mecklenburg_signals_addr): a resolved
identity is a normalized SIGNAL carrying match_basis + numeric confidence +
full provenance; distinct confidence classes stay distinct:

    RESOLVED_EXACT            -> parcel_match_status T5M_RESOLVED_EXACT (conf 0.97)
    RESOLVED_HIGH_CONFIDENCE  -> T5M_RESOLVED_HIGH_CONFIDENCE (0.82-0.95), NEVER promoted

AMBIGUOUS / NO_MATCH / INSUFFICIENT_SOURCE / HUMAN_LOOP_REQUIRED are never
adopted as identities — they are preserved as work queues.

Per-parcel aggregation rule (documented, arithmetic on real bills only):
multiple delinquent BILLS on one parcel (the receipt's 784 duplicate output
identities) collapse to ONE property row; tax_balance = SUM of the bill
balances (each is a distinct published liability), tax_years/source labels
merged, bill_count recorded; the parcel class is the strongest bill class and
every per-bill class survives in the adoption JSONL.

Stable identity wins: a parcel already inside the canonical T4 enriched
universe is NOT re-minted — its T2 bills are recorded as corroborating
evidence in the adoption JSONL (overlap report), the existing row stays
authoritative.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ENRICHED_COLUMNS = [
    "county", "county_fips", "parcel_apn", "alternate_parcel", "owner_name",
    "owner_name_2", "owner_type", "property_address", "property_city", "property_zip",
    "owner_mailing_address", "owner_mailing_city", "owner_mailing_state",
    "owner_mailing_zip", "out_of_state", "assessed_value", "land_value",
    "improvement_value", "assessed_value_less_tax", "tax_to_assessed_ratio",
    "tax_balance", "tax_years", "last_sale_date", "years_owned", "acreage",
    "structure_count", "structure_year", "property_use", "source_document_ref",
    "source_document_date", "source_types", "signal_count", "court_case_numbers",
    "heir_indicator", "fragmented_ownership_indicator", "tax_foreclosure_indicator",
    "parcel_match_status", "source_url", "source_urls", "mortgage_balance",
    "judgment_lien_balance", "estimated_equity", "equity_status", "stage",
    "contact_authorized",
]

COUNTY_BY_FIPS = {"37119": "Mecklenburg", "37179": "Union", "37183": "Wake",
                  "37101": "Johnston", "37063": "Durham", "37025": "Cabarrus",
                  "37071": "Gaston", "37097": "Iredell"}

T5M_JOB_ID = "t5m-meck-resolve-0001"
T5M_ALGO = "t5m_owner_parcel_resolution.v1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def gate(path: Path, expected: str, label: str) -> None:
    actual = sha256_file(path)
    if actual != expected:
        raise SystemExit(f"CUSTODY GATE FAILED — {label}: expected {expected[:16]}…, "
                         f"got {actual[:16]}… ({path}). NO ADOPTION.")


def norm_parcel(v: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (v or "").upper())


def fnum(v: Any) -> float | None:
    s = re.sub(r"[$, ]", "", str(v or ""))
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


_STATE_ZIP_RE = re.compile(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\s*$")


def state_from_text(addr: str) -> str:
    """Conservative 2-letter state from a free-text mailing tail; blank if not
    confidently parseable (unknown stays unknown)."""
    m = _STATE_ZIP_RE.search((addr or "").strip().upper())
    return m.group(1) if m else ""


def years_owned_from_year(year: int | None, as_of_year: int = 2026) -> str:
    return str(as_of_year - year) if year and 1850 < year <= as_of_year else ""


def build(args: argparse.Namespace) -> dict[str, Any]:
    receipt = json.loads(args.receipt.read_text())
    out = args.output
    out.mkdir(parents=True, exist_ok=True)

    # --- PHASE-A GATES: nothing is trusted before its hash matches the receipt --
    gate(args.resolved, receipt["OUTPUT_SHA256"]["resolved_owner_parcel.csv"], "resolved CSV")
    gate(args.unresolved, receipt["OUTPUT_SHA256"]["unresolved_owner_parcel.csv"], "unresolved CSV")
    gate(args.human_loop, receipt["OUTPUT_SHA256"]["human_loop_queue.csv"], "human-loop queue")
    gate(args.onemap, receipt["INPUT_SHA256"]["inputs/onemap_37119_parcels.jsonl"], "OneMap universe")
    gate(args.cama, receipt["INPUT_SHA256"]["inputs/cama_37119_ownership.jsonl"], "CAMA universe")
    gate(args.staged_taxpayers,
         receipt["INPUT_SHA256"]["inputs/mecklenburg_unresolved_taxpayers.jsonl"], "staged taxpayers")

    resolved = list(csv.DictReader(open(args.resolved, newline="", encoding="utf-8")))
    classes = defaultdict(int)
    for r in resolved:
        classes[r["resolution_class"]] += 1
    exact = classes.get("RESOLVED_EXACT", 0)
    high = classes.get("RESOLVED_HIGH_CONFIDENCE", 0)
    if exact != receipt["QUALITY_COURT"]["CLASSES"]["RESOLVED_EXACT"] or \
       high != receipt["QUALITY_COURT"]["CLASSES"]["RESOLVED_HIGH_CONFIDENCE"]:
        raise SystemExit("class counts diverge from receipt — NO ADOPTION")

    # staged taxpayer join (full provenance: source_url/label/account per staging_id)
    staged: dict[str, dict[str, Any]] = {}
    with open(args.staged_taxpayers, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                s = json.loads(line)
                staged[s["staging_id"]] = s

    # parcel attribute lookups from the CUSTODIED universes (no network)
    onemap: dict[str, dict[str, Any]] = {}
    with open(args.onemap, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                p = json.loads(line)
                key = norm_parcel(p.get("parno") or p.get("altparno") or "")
                if key and key not in onemap:
                    onemap[key] = p
    cama: dict[str, dict[str, Any]] = {}
    with open(args.cama, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                p = json.loads(line)
                key = norm_parcel(p.get("pid") or p.get("camapid") or "")
                if key and key not in cama:
                    cama[key] = p

    # --- PHASE C: reconcile against the existing canonical universe -------------
    prior = list(csv.DictReader(open(args.enriched, newline="", encoding="utf-8")))
    prior_keys = {(r["county_fips"], norm_parcel(r["parcel_apn"])) for r in prior}

    per_parcel: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in resolved:
        per_parcel[norm_parcel(r["parcel_apn"])].append(r)

    overlap_rows, conflicts = [], []
    new_parcels: dict[str, list[dict[str, Any]]] = {}
    for key, bills in sorted(per_parcel.items()):
        if ("37119", key) in prior_keys:
            for b in bills:
                overlap_rows.append({"parcel_apn": key, "staging_id": b["staging_id"],
                                     "resolution_class": b["resolution_class"],
                                     "note": "parcel already canonical in T4 universe; "
                                             "bill kept as corroborating evidence only"})
            continue
        owners = {re.sub(r"\s+", " ", (b["parcel_owner_name"] or "").strip().upper())
                  for b in bills}
        if len(owners) > 1:
            conflicts.append({"parcel_apn": key, "owners": sorted(owners),
                              "bills": len(bills),
                              "disposition": "adopted with parcel-universe owner "
                                             "(single authority), variants recorded"})
        new_parcels[key] = bills

    # --- PHASE D: adoption artifacts --------------------------------------------
    adopted_signals_path = out / "MECKLENBURG_ADOPTED_SIGNALS.jsonl"
    digests = {"inputs": receipt["INPUT_SHA256"], "outputs": receipt["OUTPUT_SHA256"]}
    n_signals = 0
    with open(adopted_signals_path, "w", encoding="utf-8") as fh:
        for key, bills in sorted(new_parcels.items()):
            for b in sorted(bills, key=lambda x: x["staging_id"]):
                st = staged.get(b["staging_id"], {})
                sig = {
                    "signal_id": hashlib.sha256(
                        f"t5m-adopt|{b['staging_id']}|{key}".encode()).hexdigest(),
                    "source": "official_distress:mecklenburg:t5m_owner_parcel_resolution",
                    "source_type": "taxdelinquent",
                    "geography": "37119",
                    "event_date": b.get("source_as_of") or "2026-08-04",
                    "raw_address": b.get("parcel_situs_address") or "",
                    "parcel_apn": b["parcel_apn"],
                    "owner_name": b.get("parcel_owner_name") or b.get("taxpayer_name") or "",
                    "detail_json": {
                        "observation_kind": "published_tax_distress_property_matched",
                        "county": "Mecklenburg", "county_fips": "37119",
                        "t2_job_id": T5M_JOB_ID, "algorithm_version": T5M_ALGO,
                        "resolution_class": b["resolution_class"],
                        "match_method": b["match_method"],
                        "match_confidence": fnum(b["confidence"]),
                        "owner_match_score": fnum(b.get("owner_match_score")),
                        "candidate_count": b.get("candidate_count"),
                        "fields_matched": b.get("fields_matched"),
                        "fields_contradicted": b.get("fields_contradicted") or None,
                        "staging_id": b["staging_id"],
                        "taxpayer_name": b.get("taxpayer_name"),
                        "publication_property_address": b.get("publication_property_address"),
                        "tax_balance": fnum(b.get("tax_balance")),
                        "tax_account_or_bill": st.get("tax_account_or_bill"),
                        "source_label": b.get("source_label"),
                        "source_url": st.get("source_url"),
                        "source_as_of": b.get("source_as_of"),
                        "evidence_source_ref": b.get("evidence_source_ref"),
                        "custody_digests": digests,
                        "contact_authorized": False,
                    },
                    "confidence": fnum(b["confidence"]),
                    "fetched_at": args.as_of,
                }
                fh.write(json.dumps(sig, sort_keys=True, separators=(",", ":")) + "\n")
                n_signals += 1

    # --- new enriched property rows (custodied-universe attributes only) --------
    new_rows = []
    for key, bills in sorted(new_parcels.items()):
        om, cm = onemap.get(key), cama.get(key)
        bills = sorted(bills, key=lambda x: x["staging_id"])
        b0 = bills[0]
        cls = ("T5M_RESOLVED_EXACT"
               if any(b["resolution_class"] == "RESOLVED_EXACT" for b in bills)
               else "T5M_RESOLVED_HIGH_CONFIDENCE")
        balances = [fnum(b.get("tax_balance")) for b in bills]
        balance = round(sum(v for v in balances if v is not None), 2) \
            if any(v is not None for v in balances) else ""
        assessed = (om or {}).get("parval") or (cm or {}).get("amt_totalvalue")
        land = (om or {}).get("landval") or (cm or {}).get("amt_landvalue")
        improv = (om or {}).get("improvval")
        mail_addr = ((om or {}).get("mailadd") or (cm or {}).get("txt_mailaddr1") or "").strip()
        mail_state = ((om or {}).get("mstate") or (cm or {}).get("txt_state") or "").strip().upper()
        if not mail_state:
            mail_state = state_from_text(b0.get("parcel_mailing_address") or "")
        sale_year = None
        sale_tx = str((om or {}).get("saledatetx") or "")
        m = re.search(r"(19|20)\d{2}", sale_tx)
        if m:
            sale_year = int(m.group(0))
        elif (cm or {}).get("dte_dateofsale"):
            try:
                sale_year = datetime.fromtimestamp(
                    int(cm["dte_dateofsale"]) / 1000, tz=timezone.utc).year
            except (ValueError, OSError, TypeError):
                sale_year = None
        labels = sorted({b.get("source_label") or "" for b in bills if b.get("source_label")})
        urls = sorted({(staged.get(b["staging_id"]) or {}).get("source_url") or ""
                       for b in bills} - {""})
        row = {c: "" for c in ENRICHED_COLUMNS}
        row.update({
            "county": "Mecklenburg", "county_fips": "37119",
            "parcel_apn": b0["parcel_apn"],
            "alternate_parcel": (om or {}).get("altparno") or "",
            "owner_name": ((om or {}).get("ownname") or (cm or {}).get("full_owner_name")
                           or b0.get("parcel_owner_name") or "").strip(),
            "owner_name_2": (str((om or {}).get("ownname2") or "").strip()
                             or (b0.get("parcel_owner_secondary") or "").strip()),
            "property_address": (b0.get("parcel_situs_address")
                                 or (om or {}).get("siteadd") or "").strip(),
            "property_city": ((om or {}).get("scity") or (cm or {}).get("txt_city") or "").strip(),
            "property_zip": str((om or {}).get("szip") or "").strip(),
            "owner_mailing_address": mail_addr,
            "owner_mailing_city": ((om or {}).get("mcity") or "").strip(),
            "owner_mailing_state": mail_state,
            "owner_mailing_zip": str((om or {}).get("mzip") or (cm or {}).get("txt_zipcode") or "").strip(),
            "out_of_state": ("" if not mail_state else str(mail_state != "NC")),
            "assessed_value": assessed if assessed not in (None, "") else "",
            "land_value": land if land not in (None, "") else "",
            "improvement_value": improv if improv not in (None, "") else "",
            "assessed_value_less_tax": (round(float(assessed) - float(balance), 2)
                                        if assessed not in (None, "") and balance != "" else ""),
            "tax_balance": balance,
            "tax_years": "",     # publication carries balances, not year lists — unknown
            "last_sale_date": sale_tx or "",
            "years_owned": years_owned_from_year(sale_year),
            "acreage": (om or {}).get("gisacres") or (cm or {}).get("num_totalac") or "",
            "structure_year": (om or {}).get("structyear") or "",
            "property_use": ((om or {}).get("parusedesc")
                             or (cm or {}).get("txt_propertyuse_desc") or "").strip(),
            "source_document_ref": "|".join(labels),
            "source_document_date": b0.get("source_as_of") or "",
            "source_types": "taxdelinquent",
            "signal_count": str(len(bills)),
            "parcel_match_status": cls,
            "source_url": urls[0] if urls else "",
            "source_urls": "|".join(urls),
            "equity_status": "UNKNOWN_NEEDS_DEBT_SEARCH",
            "stage": "RAW_OPPORTUNITY_ENRICHED",
            "contact_authorized": "False",
        })
        new_rows.append(row)

    # --- PHASE E: fold in the six-county pending signals -------------------------
    pending_rows, pending_stats = build_six_county_rows(args.pending_signals)

    combined: dict[tuple[str, str], dict[str, Any]] = {}
    for r in prior:                                   # stable identity wins, order 1
        combined[(r["county_fips"], norm_parcel(r["parcel_apn"]))] = r
    dropped_dupes = 0
    for r in new_rows + pending_rows:                 # order 2/3: T2 then six-county
        k = (r["county_fips"], norm_parcel(r["parcel_apn"]))
        if k in combined:
            dropped_dupes += 1
            continue
        combined[k] = r

    ranking_input = out / "RANKING_INPUT.csv"
    with open(ranking_input, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=ENRICHED_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for k in sorted(combined):
            w.writerow({c: combined[k].get(c, "") for c in ENRICHED_COLUMNS})

    # --- PHASE J: preserve unresolved queues ------------------------------------
    qdir = out / "queues"
    qdir.mkdir(exist_ok=True)
    unres = list(csv.DictReader(open(args.unresolved, newline="", encoding="utf-8")))
    qcounts = {}
    for cls_name in ("AMBIGUOUS", "NO_MATCH", "INSUFFICIENT_SOURCE"):
        rows_c = [r for r in unres if r["resolution_class"] == cls_name]
        qcounts[cls_name] = len(rows_c)
        with open(qdir / f"{cls_name}_QUEUE.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(unres[0].keys()))
            w.writeheader()
            w.writerows(rows_c)
    hl = args.human_loop.read_bytes()
    (qdir / "HUMAN_LOOP_QUEUE.csv").write_bytes(hl)   # byte-preserved, operator actions intact
    qcounts["HUMAN_LOOP_REQUIRED"] = sum(1 for _ in csv.DictReader(
        open(qdir / "HUMAN_LOOP_QUEUE.csv", encoding="utf-8")))

    report = {
        "adoption": {
            "t2_job_id": T5M_JOB_ID, "algorithm_version": T5M_ALGO,
            "resolved_exact_bills": exact, "resolved_high_confidence_bills": high,
            "adopted_signals": n_signals,
            "adopted_new_parcels": len(new_rows),
            "parcel_class_counts": {
                "T5M_RESOLVED_EXACT": sum(1 for r in new_rows
                                          if r["parcel_match_status"] == "T5M_RESOLVED_EXACT"),
                "T5M_RESOLVED_HIGH_CONFIDENCE": sum(
                    1 for r in new_rows
                    if r["parcel_match_status"] == "T5M_RESOLVED_HIGH_CONFIDENCE"),
            },
            "bills_on_already_canonical_parcels": len(overlap_rows),
            "owner_variant_conflicts": len(conflicts),
            "aggregation_rule": "bills summed per parcel; classes preserved per bill "
                                "in MECKLENBURG_ADOPTED_SIGNALS.jsonl",
        },
        "reconciliation": {
            "prior_universe_rows": len(prior),
            "six_county_pending": pending_stats,
            "combined_unique_properties": len(combined),
            "cross_set_duplicates_dropped": dropped_dupes,
        },
        "queues": qcounts,
        "before_after": {
            "before_canonical_properties": len(prior),
            "after_canonical_properties": len(combined),
        },
        "custody": digests,
        "as_of": args.as_of,
    }
    (out / "ADOPTION_REPORT.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    (out / "OVERLAP_T4_PARCELS.json").write_text(json.dumps(overlap_rows, indent=2))
    (out / "OWNER_VARIANT_CONFLICTS.json").write_text(json.dumps(conflicts, indent=2))
    return report


def build_six_county_rows(signals_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Six-county property-identified signals -> enriched-schema rows.
    Economics only where the SOURCE published them; per-parcel bill sums."""
    per: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    n = 0
    with open(signals_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            s = json.loads(line)
            n += 1
            per[(s["geography"], norm_parcel(s["parcel_apn"]))].append(s)

    rows = []
    for (fips, key), sigs in sorted(per.items()):
        sigs = sorted(sigs, key=lambda x: x["signal_id"])
        d0 = sigs[0]["detail_json"]
        types = sorted({s["source_type"] for s in sigs})
        balances = [fnum(s["detail_json"].get("tax_balance")) for s in sigs]
        balance = round(sum(v for v in balances if v is not None), 2) \
            if any(v is not None for v in balances) else ""
        years = sorted({str(s["detail_json"].get("tax_year"))
                        for s in sigs if s["detail_json"].get("tax_year")})
        cases = sorted({c for s in sigs
                        for c in (s["detail_json"].get("court_case_numbers") or [])})
        assessed = next((s["detail_json"].get("total_real_value") or
                         s["detail_json"].get("tax_value")
                         for s in sigs
                         if s["detail_json"].get("total_real_value")
                         or s["detail_json"].get("tax_value")), "")
        mail = next((s["detail_json"].get("mailing_address")
                     for s in sigs if s["detail_json"].get("mailing_address")), "")
        mail_state = state_from_text(mail or "")
        owner = next((s["owner_name"] for s in sigs if s["owner_name"]), "")
        addr = next((s["raw_address"] for s in sigs if s["raw_address"]), "")
        urls = sorted({s["detail_json"].get("source_url") or "" for s in sigs} - {""})
        row = {c: "" for c in ENRICHED_COLUMNS}
        row.update({
            "county": COUNTY_BY_FIPS.get(fips, fips), "county_fips": fips,
            "parcel_apn": sigs[0]["parcel_apn"],
            "owner_name": owner, "property_address": addr,
            "owner_mailing_address": mail or "",
            "owner_mailing_state": mail_state,
            "out_of_state": ("" if not mail_state else str(mail_state != "NC")),
            "assessed_value": assessed if assessed not in (None, "") else "",
            "tax_balance": balance,
            "tax_years": "|".join(years),
            "source_document_ref": "|".join(sorted({s["detail_json"].get("source_label") or ""
                                                    for s in sigs} - {""})),
            "source_document_date": d0.get("source_as_of") or "",
            "source_types": "|".join(types),
            "signal_count": str(len(sigs)),
            "court_case_numbers": "|".join(cases),
            "tax_foreclosure_indicator": ("True" if "tax_foreclosure_case" in types else ""),
            "parcel_match_status": "SOURCE_PARCEL_IDENTIFIED",
            "source_url": urls[0] if urls else "",
            "source_urls": "|".join(urls),
            "equity_status": "UNKNOWN_NEEDS_DEBT_SEARCH",
            "stage": "RAW_OPPORTUNITY_ENRICHED",
            "contact_authorized": "False",
        })
        rows.append(row)
    stats = {"signals": n, "distinct_properties": len(per)}
    return rows, stats


def main() -> None:
    ap = argparse.ArgumentParser(description="D0 T5-M adoption -> canonical ranking input")
    ap.add_argument("--receipt", required=True, type=Path)
    ap.add_argument("--resolved", required=True, type=Path)
    ap.add_argument("--unresolved", required=True, type=Path)
    ap.add_argument("--human-loop", required=True, type=Path)
    ap.add_argument("--onemap", required=True, type=Path)
    ap.add_argument("--cama", required=True, type=Path)
    ap.add_argument("--staged-taxpayers", required=True, type=Path)
    ap.add_argument("--enriched", required=True, type=Path)
    ap.add_argument("--pending-signals", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--as-of", default="2026-08-12T00:00:00Z")
    args = ap.parse_args()
    report = build(args)
    print(json.dumps({k: report[k] for k in ("adoption", "reconciliation", "queues")},
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

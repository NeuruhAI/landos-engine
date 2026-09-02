#!/usr/bin/env python3
"""Run the free routes (FREE_ROUTES_REGISTER) against the CURRENT estate, in yield order.

    python -m scripts.run_free_routes --run-name FREE_ROUTES_20260902 [--routes T8,I1] [--apply] [--counties Wake,Mecklenburg]

Dry-run by default: receipts, worklists and signal CSVs are written under the custody run dir
(outside Git, off every cloud sync); nothing touches the spine. --apply writes the per-row
attempts to public.free_route_attempts (DATABASE_URL, DB owner). Contact writes never happen
here: they go through project_contacts_to_deedsonar (party graph) after their own dry run.
Routes whose terms forbid scripts (T1, T2, T3, T4, T5, T7, T10) always produce an H-12 receipt
and a worklist — never a silent skip. Paid routes never run (ALLOW_SPEND=0)."""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.custody.locations import canonical_bundle, estate_export, run_dir  # noqa: E402
from app.custody.sync_guard import prepare_output_dir  # noqa: E402
from app.free_routes.attempts import write_attempts  # noqa: E402
from app.free_routes.estate_rows import load_estate_rows, is_entity_owner, is_probate  # noqa: E402
from app.free_routes.human_routes import (t2_notice_search, t3_special_proceedings_search, t4_obituary_search,  # noqa: E402
                                          t5_sos_search, t7_rod_search, t10_people_search, write_worklist)
from app.free_routes.invented import i1_out_of_state, i4_route_orders  # noqa: E402
from app.free_routes.receipts import halted_receipt, write_receipt  # noqa: E402
from app.free_routes.registry import ROUTES  # noqa: E402
from app.free_routes import t1_ecourts_pdf, t8_open_data, usps  # noqa: E402

DEFAULT_ROUTES = ["T1", "T2", "T3", "T4", "T5", "T7", "T8", "T9", "T10", "I1", "I4"]   # T6 / I2 / I3 / I5 have their own runners (spine)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _worklist(path, rows):
    n = write_worklist(path, rows); return path, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-name", default=f"FREE_ROUTES_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}")
    ap.add_argument("--estate-dir", default=str(estate_export()))
    ap.add_argument("--bundle-dir", default=str(canonical_bundle()), help="the canonical bundle holding CANDIDATE_CONTACT_RESOLUTION.csv (owner_class per candidate)")
    ap.add_argument("--routes", default=",".join(DEFAULT_ROUTES))
    ap.add_argument("--counties", default="")
    ap.add_argument("--cap", type=int, default=40, help="worklist cap per county for the human routes")
    ap.add_argument("--apply", action="store_true", help="write per-row attempts to public.free_route_attempts")
    ap.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    ap.add_argument("--wake-extract", default="", help="a local real_estate_delq853_*.zip (else downloaded)")
    ap.add_argument("--skip-charlotte", action="store_true")
    a = ap.parse_args()
    if (os.environ.get("ALLOW_SPEND") or "0") != "0":
        sys.exit("ALLOW_SPEND must be 0: the free routes never pay")
    out = prepare_output_dir(run_dir(a.run_name), what="free routes run")
    receipts_dir = str(out / "receipts"); worklists_dir = str(out / "worklists"); signals_dir = str(out / "signals")
    for d in (receipts_dir, worklists_dir, signals_dir):
        os.makedirs(d, exist_ok=True)
    run_id = str(uuid.uuid4()); ts = now_iso()
    counties = [c for c in a.counties.split(",") if c] or None
    estate = load_estate_rows(Path(a.estate_dir), counties, Path(a.bundle_dir) if a.bundle_dir else None)
    print(f"owner_class from the ledger: {sum(1 for r in estate if r.get('res_owner_class'))} of {len(estate)} rows; probate {sum(1 for r in estate if is_probate(r))}; entity {sum(1 for r in estate if is_entity_owner(r))}")
    routes = [r for r in a.routes.split(",") if r]
    print(f"run_id={run_id} estate_rows={len(estate)} routes={routes} apply={a.apply}")
    all_attempts = []; index = {}
    by_id = {r.route_id: r for r in ROUTES}

    def human_route(route_id, rows_fn, url_fn, instr_fn, paste):
        reg = by_id[route_id]; rows = []
        for r in rows_fn():
            rows.append({"route_id": route_id, "global_rank": r.get("global_rank", ""), "candidate_id": r["candidate_id"], "name": r.get("owner_name", ""), "role": "owner",
                         "county": r.get("county", ""), "situs_address": r.get("property_address_raw", ""), "apn": r.get("apn", ""), "url": url_fn(r), "instructions": instr_fn(r), "paste_back": paste})
        rows.sort(key=lambda x: int(x["global_rank"] or 10**9)); rows = rows[: a.cap * 7]
        path, n = _worklist(os.path.join(worklists_dir, f"{route_id}_WORKLIST_{ts[:10].replace('-', '')}.csv"), rows)
        rec = halted_receipt(route_id, reg.name, mode=reg.mode, source=reg.source, license=reg.license, provenance_url=reg.provenance_url,
                             halt_code=reg.halt or "H-12", human_step=f"{reg.human_step} ({n} rows in {path})", rows_tried=n, worklist_path=path)
        index[route_id] = write_receipt(rec, receipts_dir)
        from app.free_routes.attempts import Attempt
        for r in rows:
            all_attempts.append(Attempt(r["candidate_id"], route_id, run_id, "HALTED", reg.source, None, ts, index[route_id], {"human_step": reg.halt or "H-12"}))

    probate_rows = lambda: [r for r in estate if is_probate(r)]  # noqa: E731
    entity_rows = lambda: [r for r in estate if is_entity_owner(r)]  # noqa: E731
    person_rows = lambda: [r for r in estate if not is_entity_owner(r)]  # noqa: E731
    all_rows = lambda: estate  # noqa: E731

    if "T1" in routes:
        res = t1_ecourts_pdf.run(estate, counties or ["Wake", "Mecklenburg", "Durham"], run_id=run_id, now_iso=ts, worklists_dir=worklists_dir, receipt_path=None)
        for rec in res["receipts"]:
            index[f"T1:{rec.counties[0] if rec.counties else ''}"] = write_receipt(rec, receipts_dir)
        all_attempts += res["attempts"]
    if "T2" in routes:
        human_route("T2", probate_rows, lambda r: t2_notice_search(r.get("owner_name", ""), r.get("county", ""))["url"],
                    lambda r: json.dumps(t2_notice_search(r.get("owner_name", ""), r.get("county", ""))), "PR name, mailing address, publication date, newspaper")
    if "T3" in routes:
        human_route("T3", probate_rows, lambda r: t3_special_proceedings_search(r.get("owner_name", ""), r.get("county", ""))["url"],
                    lambda r: json.dumps(t3_special_proceedings_search(r.get("owner_name", ""), r.get("county", ""))), "the petition PDF")
    if "T4" in routes:
        human_route("T4", probate_rows, lambda r: t4_obituary_search(r.get("owner_name", ""), r.get("property_city", ""))["legacy_url"],
                    lambda r: t4_obituary_search(r.get("owner_name", ""), r.get("property_city", ""))["google_url"], "the 'survived by' paragraph")
    if "T5" in routes:
        human_route("T5", entity_rows, lambda r: t5_sos_search(r.get("owner_name", ""))["url"], lambda r: json.dumps(t5_sos_search(r.get("owner_name", ""))), "SOSID, status, registered agent, officials, offices")
    if "T7" in routes:
        human_route("T7", all_rows, lambda r: t7_rod_search(r.get("owner_name", ""), r.get("county", ""))["url"], lambda r: t7_rod_search(r.get("owner_name", ""), r.get("county", ""))["record"], "book/page, instrument date, grantor, mailing address, mortgage holder, liens")
    if "T8" in routes:
        extract = a.wake_extract or t8_open_data.download_wake_extract(str(out / "raw"), datetime.now(timezone.utc))
        w = t8_open_data.match_wake(estate, extract, run_id=run_id, now_iso=ts, receipt_path=None)
        index["T8:wake"] = write_receipt(w["receipt"], receipts_dir); all_attempts += w["attempts"]
        with open(os.path.join(signals_dir, "T8_wake_delinquent_signals.csv"), "w", newline="") as fh:
            if w["signals"]:
                wr = csv.DictWriter(fh, fieldnames=list(w["signals"][0].keys())); wr.writeheader(); wr.writerows(w["signals"])
        if not a.skip_charlotte:
            c = t8_open_data.match_charlotte_code(estate, run_id=run_id, now_iso=ts, receipt_path=None)
            index["T8:charlotte"] = write_receipt(c["receipt"], receipts_dir); all_attempts += c["attempts"]
            with open(os.path.join(signals_dir, "T8_charlotte_code_signals.csv"), "w", newline="") as fh:
                if c["signals"]:
                    wr = csv.DictWriter(fh, fieldnames=list(c["signals"][0].keys())); wr.writeheader(); wr.writerows(c["signals"])
    if "T9" in routes:
        reg = by_id["T9"]
        if usps.credentials_present():
            rows = [{"key": r["candidate_id"], "street": r.get("mailing_address", ""), "city": r.get("mailing_city", ""), "state": r.get("mailing_state", ""), "zip5": (r.get("mailing_zip") or "")[:5]} for r in estate if (r.get("mailing_address") or "").strip()]
            vals = usps.validate_many(rows, today=ts[:10], now_iso=ts)
            from app.free_routes.receipts import RouteReceipt
            from app.free_routes.attempts import Attempt
            hits = sum(1 for v in vals if v.deliverable is not None)
            rec = RouteReceipt("T9", reg.name, "AUTOMATED", reg.source, reg.license, reg.provenance_url, rows_tried=len(rows), hits=hits, new_contacts=0, new_heirs=0, source_as_of=ts[:10], observed_at=ts)
            index["T9"] = write_receipt(rec, receipts_dir)
            with open(os.path.join(signals_dir, "T9_usps_validated.csv"), "w", newline="") as fh:
                wr = csv.DictWriter(fh, fieldnames=list(vals[0].to_dict().keys()) if vals else ["input_key"]); wr.writeheader(); wr.writerows(v.to_dict() for v in vals)
            for v in vals:
                all_attempts.append(Attempt(v.input_key, "T9", run_id, "HIT" if v.deliverable is not None else "ERROR", usps.SOURCE, ts[:10], ts, index["T9"], {"dpv": v.dpv_confirmation, "error": v.error}))
        else:
            rec = halted_receipt("T9", reg.name, mode="AUTOMATED", source=reg.source, license=reg.license, provenance_url=reg.provenance_url, halt_code="H-12",
                                 human_step=usps.HUMAN_STEP, rows_tried=sum(1 for r in estate if (r.get("mailing_address") or "").strip()))
            index["T9"] = write_receipt(rec, receipts_dir)
    if "T10" in routes:
        human_route("T10", person_rows, lambda r: t10_people_search(r.get("owner_name", ""), r.get("property_city", ""))["truepeoplesearch"],
                    lambda r: t10_people_search(r.get("owner_name", ""), r.get("property_city", ""))["google"], "phones / relatives as read (confidence = operator_web)")
    if "I1" in routes:
        res = i1_out_of_state(estate, run_id=run_id, now_iso=ts, receipt_path=None)
        index["I1"] = write_receipt(res["receipt"], receipts_dir); all_attempts += res["attempts"]
        _worklist(os.path.join(worklists_dir, f"I1_OUT_OF_STATE_{ts[:10].replace('-', '')}.csv"), res["worklist"])
    if "I4" in routes:
        orders = i4_route_orders(estate)
        with open(os.path.join(signals_dir, "I4_route_order.csv"), "w", newline="") as fh:
            wr = csv.writer(fh); wr.writerow(["candidate_id", "route_order"]); wr.writerows((k, ";".join(v)) for k, v in orders.items())
        from app.free_routes.receipts import RouteReceipt
        index["I4"] = write_receipt(RouteReceipt("I4", by_id["I4"].name, "AUTOMATED", by_id["I4"].source, by_id["I4"].license, by_id["I4"].provenance_url, rows_tried=len(orders), hits=len(orders), new_contacts=0, new_heirs=0, source_as_of=None, observed_at=ts), receipts_dir)
    att = write_attempts(all_attempts, database_url=a.database_url, apply=a.apply)
    manifest = {"run_id": run_id, "run_name": a.run_name, "generated_at": ts, "estate_rows": len(estate), "routes": routes, "receipts": index, "attempts": att, "apply": a.apply}
    with open(out / "MANIFEST.json", "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(json.dumps({k: v for k, v in manifest.items() if k != "receipts"}, indent=2))
    print(f"receipts: {receipts_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

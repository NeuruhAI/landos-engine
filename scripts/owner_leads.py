"""
owner_leads.py — rank the absentee-owner queue for a county.

    python -m scripts.owner_leads --county berrien

Pulls landos.owner_leads for the county, JOINS the existing landos.signals stack
(tax_delinquent / brownfield / environmental_part201) by parcel_apn and tests each
parcel against the opportunity-zone overlays, runs the PURE absentee scorer
(app/score_absentee.py), writes the computed absentee fields back to owner_leads,
then emits a ranked CSV (data/owner_leads_<county>.csv) and prints the top 10.

MULTI-COUNTY: --county drives county_fips via the same COUNTY_CONFIG the normalizer
uses (alias derived from the region string). Nothing Berrien-specific is hardcoded.

Honesty: absentee != motivated. final_lead_score ranks REACHABILITY + disposition
likelihood, tightened by signal overlap. It is a prioritized list, not sellers.
"""
import argparse
import csv
import json
import os
import uuid
from collections import defaultdict
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.normalizers.reportall_owner import COUNTY_CONFIG
from app.overlays import overlay_hits
from app.score_absentee import (
    COUNTY_GEO, score_lead, tiebreak_key, weights_snapshot, __VERSION__,
)
from app.run_manifest import (
    sha256_file, git_commit_info, utc_now, build_manifest, write_manifest, insert_run_log,
)

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

OUT_FIELDS = [
    "rank", "owner_name", "owner_type", "lens", "absentee_tier", "situs_address",
    "mailing_address", "apn", "land_use", "acreage", "absentee_score",
    "signal_overlap", "final_lead_score", "skip_trace_status", "push_status",
]


def county_alias_to_fips(alias: str) -> Optional[str]:
    """'berrien' -> '26021', derived from COUNTY_CONFIG region strings (no hardcode)."""
    a = (alias or "").strip().lower()
    for fips, cfg in COUNTY_CONFIG.items():
        name = cfg["region"].split(" County")[0].strip().lower()
        if a in (fips, name):
            return fips
    return None


def _f(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_leads(cur, county_fips: str) -> List[Dict[str, Any]]:
    cur.execute(
        "select * from landos.owner_leads where tenant_id=%s and county_fips=%s",
        (TENANT, county_fips),
    )
    return cur.fetchall()


def load_signal_types_by_apn(cur) -> Dict[str, set]:
    """parcel_apn -> set(signal_type) for this tenant. Exact APN match: owner_leads
    stores the same CSV-form APN as landos.signals, so the join is direct."""
    cur.execute(
        "select parcel_apn, signal_type from landos.signals "
        "where tenant_id=%s and parcel_apn is not null",
        (TENANT,),
    )
    by_apn: Dict[str, set] = defaultdict(set)
    for r in cur.fetchall():
        by_apn[r["parcel_apn"]].add(r["signal_type"])
    return by_apn


def load_oz_overlays(cur) -> List[Dict[str, Any]]:
    cur.execute(
        """
        select overlay_type, name, geometry, source, provenance_status,
               effective_date, multiplier_weight, buffer_m, minx, miny, maxx, maxy
        from landos.overlay_polygons
        where (tenant_id = %s or tenant_id is null) and overlay_type = 'opportunity_zone'
        """,
        (TENANT,),
    )
    return cur.fetchall()


def oz_hit_for(lat, lng, oz_overlays) -> Optional[Dict[str, Any]]:
    if lat is None or lng is None:
        return None
    hits = overlay_hits(float(lat), float(lng), oz_overlays)
    # prefer an authoritative OZ hit if present
    auth = [h for h in hits if h.get("provenance_status") == "authoritative"]
    return (auth or hits or [None])[0]


def _mailing_str(lead: Dict[str, Any]) -> str:
    bits = [lead.get("owner_mailing_address")]
    csz = " ".join(p for p in [lead.get("owner_mailing_city"),
                               lead.get("owner_mailing_state"),
                               lead.get("owner_mailing_zip")] if p)
    if csz:
        bits.append(csz)
    return ", ".join(b for b in bits if b)


def _overlap_str(ov: Dict[str, Any]) -> str:
    parts = list(ov.get("distress_stack") or [])
    if ov.get("in_opportunity_zone"):
        parts.append(f"OZ:{ov.get('oz_tract') or 'yes'}")
    return "|".join(parts) or "none"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", required=True, help="county alias or FIPS, e.g. berrien / 26021")
    ap.add_argument("--out", default=None)
    ap.add_argument("--include-discovery", action="store_true",
                    help="include discovery_seed leads in the ranked queue (default: excluded — "
                         "they are a bootstrap surname-seed kept only as the validator test set)")
    args = ap.parse_args()

    fips = county_alias_to_fips(args.county)
    if not fips:
        raise SystemExit(
            f"Unknown county {args.county!r}. Known: "
            + ", ".join(f"{c['region']} ({f})" for f, c in COUNTY_CONFIG.items())
        )
    county_geo = COUNTY_GEO.get(fips)

    scored: List[Dict[str, Any]] = []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            leads = load_leads(cur, fips)
            sig_by_apn = load_signal_types_by_apn(cur)
            oz_overlays = load_oz_overlays(cur)
        print(f"county={args.county} fips={fips}  leads={len(leads)}  "
              f"oz_overlays={len(oz_overlays)}  parcels_with_signals={len(sig_by_apn)}")

        # Pass 1 — score every lead (pure), collect the UNCLAMPED raw fused score.
        for lead in leads:
            rm = lead.get("raw_meta") or {}
            if isinstance(rm, str):
                rm = json.loads(rm)
            sig_types = sig_by_apn.get(lead["parcel_apn"], set())
            oz = oz_hit_for(lead.get("situs_lat"), lead.get("situs_lng"), oz_overlays)
            merged = dict(lead)  # merge raw_meta situs locality for the pure scorer
            merged.update({
                "situs_city": rm.get("situs_city"), "situs_county": rm.get("situs_county"),
                "situs_state": rm.get("situs_state"), "situs_zip": rm.get("situs_zip"),
            })
            res = score_lead(merged, sig_types, oz, county_geo)
            # Persist the cold-call lens INTO signal_overlap jsonb (additive, no
            # migration) so downstream consumers (export_callable / sync_to_crm)
            # can filter the human-seller queue without re-deriving it.
            overlap_obj = dict(res["signal_overlap"])
            overlap_obj["lens"] = res["lens"]
            scored.append({
                "lead_id": lead["lead_id"],
                "owner_name": lead.get("owner_name") or "",
                "owner_type": res["owner_type"],
                "lens": res["lens"],
                "cold_call_eligible": res["cold_call_eligible"],
                "absentee_tier": res["absentee_tier"],
                "absentee_flag": res["absentee_flag"],
                "situs_address": lead.get("situs_address") or "",
                "mailing_address": _mailing_str(lead),
                "apn": lead.get("parcel_apn") or "",
                "land_use": lead.get("land_use_class") or "",
                "acreage": _f(lead.get("acreage")) or "",
                "absentee_score": res["absentee_score"],
                "signal_overlap": _overlap_str(res["signal_overlap"]),
                "_overlap_obj": overlap_obj,
                "_raw": res["raw_fused_score"],
                "skip_trace_status": lead.get("skip_trace_status") or "not_traced",
                "push_status": lead.get("push_status") or "not_pushed",
                "discovery_seed": bool(lead.get("discovery_seed")),
            })

        # De-compress: normalize the batch's raw scores to 0–100 against the actual
        # max, so the ranking spreads across the range instead of piling at the 100
        # ceiling. The top lead is 100; everything else scales by its true raw value.
        max_raw = max((r["_raw"] for r in scored), default=0.0) or 1.0
        for r in scored:
            r["final_lead_score"] = round(r["_raw"] / max_raw * 100.0, 2)

        # Total order: raw score first, then explicit tiebreakers (see tiebreak_key).
        scored.sort(
            key=lambda r: tiebreak_key(r["_raw"], r["_overlap_obj"], r["absentee_tier"],
                                       r["acreage"], r["owner_type"]),
            reverse=True,
        )
        for i, r in enumerate(scored, 1):
            r["rank"] = i

        # Pass 2 — write the normalized score (+ absentee fields) back to the DB.
        with conn.cursor() as cur:
            for r in scored:
                cur.execute(
                    """
                    update landos.owner_leads set
                      owner_type=%s, absentee_tier=%s, absentee_flag=%s,
                      absentee_score=%s, signal_overlap=%s, final_lead_score=%s,
                      updated_at=now()
                    where lead_id=%s
                    """,
                    (r["owner_type"], r["absentee_tier"], r["absentee_flag"],
                     r["absentee_score"], Jsonb(r["_overlap_obj"]),
                     r["final_lead_score"], r["lead_id"]),
                )
        conn.commit()

    # LIVE COLD-CALL QUEUE = human SELLERS only, minus discovery_seed bootstrap.
    # DOCTRINE (v1.2.0): absentee is a dimension, not a thesis. An institution
    # (bank/LLC/inc/corp/facilities/trust/authority) is a commercial holder, not a
    # motivated cold-call seller — it drops out of THIS queue into the entity/
    # commercial lens. Govt is a disposition channel (land bank / surplus). All
    # rows stay scored in the DB (each lens keeps its score); only the ranked
    # cold-call OUTPUT is filtered. --include-discovery re-admits the seed set.
    excluded = [r for r in scored if r["discovery_seed"]]
    institution_lens = [r for r in scored if r["lens"] == "institution" and not r["discovery_seed"]]
    govt_lens = [r for r in scored if r["lens"] == "govt" and not r["discovery_seed"]]
    live = [r for r in scored
            if (args.include_discovery or not r["discovery_seed"]) and r["cold_call_eligible"]]
    for i, r in enumerate(live, 1):
        r["rank"] = i

    out = args.out or os.path.join(
        os.path.expanduser("~/Documents/landos-engine/data"),
        f"owner_leads_{args.county.lower()}.csv",
    )
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(live)

    # ---- tamper-evident run manifest + append-only run_log -------------------
    generated_at = utc_now()
    csv_sha256 = sha256_file(out)  # hash the bytes AFTER writing
    manifest = build_manifest(
        run_id=str(uuid.uuid4()),
        generated_at=generated_at,
        county_fips=fips,
        county_alias=args.county.lower(),
        git_info=git_commit_info(REPO_DIR),
        scorer_version=__VERSION__,
        weights_snapshot=weights_snapshot(),
        row_count=len(live),
        input_apn_count=len(leads),
        reportall_calls=None,  # this run makes no ReportAll calls (see owner_leads_import_log)
        csv_path=out,
        csv_sha256=csv_sha256,
        top_5=[{"rank": r["rank"], "owner_name": r["owner_name"], "apn": r["apn"],
                "final_lead_score": r["final_lead_score"]} for r in live[:5]],
    )
    manifest_path = write_manifest(os.path.dirname(out), args.county.lower(), manifest, generated_at)
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            insert_run_log(cur, TENANT, manifest, manifest_path)
        conn.commit()

    # tier breakdown (live queue only)
    tiers: Dict[str, int] = defaultdict(int)
    overlap_n = 0
    for r in live:
        tiers[r["absentee_tier"]] += 1
        if r["signal_overlap"] != "none":
            overlap_n += 1
    if excluded and not args.include_discovery:
        print(f"\nexcluded {len(excluded)} discovery_seed leads from live queue "
              f"(kept scored in DB as validator test set; --include-discovery to show)")
    # Lens diversion (v1.2.0): institutions + govt are NOT cold-call sellers. Show
    # WHAT was diverted (and the top few) so the operator sees they were routed, not
    # lost — an absentee LLC is still a real entity/commercial lead in its own lens.
    print(f"\nLENS SPLIT: cold-call SELLERS (human) = {len(live)}  |  "
          f"entity/commercial (institution) = {len(institution_lens)}  |  "
          f"govt/disposition = {len(govt_lens)}")
    if institution_lens:
        institution_lens.sort(key=lambda r: r["_raw"], reverse=True)
        print("  diverted institutions (top 5 by raw — entity/commercial lens, NOT cold-call):")
        for r in institution_lens[:5]:
            print(f"    - {r['owner_name'][:38]:<38} raw={r['_raw']:>7.2f} "
                  f"[{r['signal_overlap'][:24]}]")
    print("tier breakdown (live sellers): " + ", ".join(f"{k}={v}" for k, v in sorted(tiers.items())))
    print(f"sellers with signal overlap: {overlap_n}/{len(live)}")
    print(f"\n========  TOP 20 LIVE — county={args.county}  (score=normalized 0-100, raw=unclamped)  ========")
    print(f"{'#':>2} {'score':>6} {'raw':>7} {'abss':>4} {'tier':<13}{'type':<11}{'#sg':>3} {'acre':>6}  owner / situs [overlap]")
    for r in live[:20]:
        nd = len(r["_overlap_obj"].get("distress_stack") or [])
        ac = r["acreage"] if r["acreage"] != "" else 0.0
        print(f'{r["rank"]:>2} {r["final_lead_score"]:>6.2f} {r["_raw"]:>7.2f} {r["absentee_score"]:>4.0f} '
              f'{r["absentee_tier"]:<13}{r["owner_type"]:<11}{nd:>3} {float(ac):>6.2f}  '
              f'{r["owner_name"][:22]:<22} | {r["situs_address"][:18]:<18} [{r["signal_overlap"][:26]}]')
    print(f"\nRanked CSV:    {out}")
    print(f"Run manifest:  {manifest_path}")
    print(f"CSV sha256:    {csv_sha256}")
    print(f"scorer_version={__VERSION__}  run_id={manifest['run_id']}  git={manifest['git_commit']}")


if __name__ == "__main__":
    main()

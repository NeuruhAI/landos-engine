"""
ingest_discovery.py — DISCOVERY-1 pipeline: signals -> parcels -> motivated leads.

    python -m scripts.ingest_discovery --adapter probate_in [--csv path] [--dry-run]

Stage-1 feed INTO owner_leads:
  adapter.fetch() -> upsert landos.discovery_signals (idempotent on the
  source's natural ref) -> attempt parcel match -> on match, CREATE a new
  owner_lead (or FLAG the existing one) with the discovery provenance and the
  recipe the signal implies (probate -> R4 deceased->heirs), then record
  matched_lead_id + status on the signal. Heirs named by the signal land in
  landos.contact_relations (is_heir=true).

PARCEL MATCH (DISCOVERY-R): all lookups go through the ParcelProviderChain
(app/providers) — config-ordered cost gradient: local_cache -> county_rest ->
reportall -> regrid. The chain stamps which_provider on every match
(audit-grade) and degrades through quota/key failures instead of hard-stopping.
A full-chain miss with an unavailable tier keeps the signal RETRYABLE ('new');
only a miss where every live tier answered empty becomes 'no_match'. The
canonical-APN rule (IN 71-form, never 018) lives in the providers/normalizer.

PROVENANCE RULE (migration-004 trap): leads created here carry
source='discovery', provenance='discovery:<source_type>:<source_ref>' — NEVER
'reportall:%', because migration 004 tags 18141 'reportall:%' rows as
discovery_seed bootstrap noise and the live queue would silently bury them.

FIREWALL: writes landos.* only (owner_leads, discovery_signals,
contact_relations). Zero public.* writes — the CRM sees discovery leads
through the P4 sync like every other lead.
"""
import argparse
import sys
import uuid
from typing import Any, Dict, List

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.discovery.probate_in import ProbateINStJoeAdapter
from app.providers.base import name_tokens
from app.providers.chain import ParcelProviderChain

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

ADAPTERS = {
    "probate_in": ProbateINStJoeAdapter,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", required=True, choices=sorted(ADAPTERS))
    ap.add_argument("--csv", default=None, help="va_fed input file override")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    adapter = (ADAPTERS[args.adapter](TENANT, csv_path=args.csv) if args.csv
               else ADAPTERS[args.adapter](TENANT))
    signals = adapter.fetch()
    print(f"adapter={adapter.source_type} county={adapter.county_fips} "
          f"mode={adapter.access_mode}: {len(signals)} signal(s) fetched")

    created = flagged = no_match = retryable = 0

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        chain = ParcelProviderChain(TENANT, conn=conn)
        adapter.upsert_signals(cur, signals)

        for s in signals:
            cur.execute(
                "select signal_id, status from landos.discovery_signals "
                "where source_type=%s and source_ref=%s",
                (adapter.source_type, s["source_ref"]),
            )
            sig = cur.fetchone()
            payload = s.get("raw_payload") or {}
            heirs = payload.get("heirs_list") or []

            # ---- the provider chain (config-ordered, degrades, never stops) ----
            if s["parcel_apn"]:
                leads = chain.lookup_by_apn(adapter.county_fips, s["parcel_apn"])
            else:
                leads = chain.search_by_owner(adapter.county_fips, s["owner_name"] or "")
            trail = chain.trail_str()

            if not leads:
                if chain.miss_is_retryable():
                    retryable += 1
                    print(f"  {s['source_ref']}  {s['owner_name']!r}: chain miss with "
                          f"unavailable tier(s) [{trail}] — signal stays 'new' for retry")
                else:
                    no_match += 1
                    cur.execute(
                        "update landos.discovery_signals set status='no_match' "
                        "where signal_id=%s", (sig["signal_id"],),
                    )
                    print(f"  {s['source_ref']}  {s['owner_name']!r}: NO parcel match "
                          f"[{trail}] -> status=no_match")
                continue

            for lead in leads:
                provenance = f"discovery:{adapter.source_type}:{s['source_ref']}"
                if lead.get("_existing_lead_id"):
                    cur.execute(
                        "select lead_id, skip_trace_meta from landos.owner_leads "
                        "where lead_id=%s", (lead["_existing_lead_id"],),
                    )
                    existing = cur.fetchone()
                else:
                    cur.execute(
                        "select lead_id, skip_trace_meta from landos.owner_leads "
                        "where tenant_id=%s and county_fips=%s and parcel_apn=%s",
                        (TENANT, lead["county_fips"], lead["parcel_apn"]),
                    )
                    existing = cur.fetchone()

                # deceased=True ONLY when the TITLED owner is the decedent.
                # An estate-LINKED household (widow/heir still on title) is not
                # marked deceased — liveness of the titled owner is the VA's
                # SOP-1 step; the estate linkage is recorded for context.
                dec_toks = (name_tokens(s["owner_name"] or "") or "").split()
                owner_toks = (lead.get("owner_name") or "").upper() \
                    .replace(",", " ").replace(".", " ").split()
                owner_is_decedent = bool(dec_toks) and all(t in owner_toks for t in dec_toks)
                meta_patch = {
                    "outreach_recipe": adapter.recipe_hint,           # R4
                    "primary_target": "heirs",
                    "deceased": owner_is_decedent,
                    "deceased_source": (f"probate docket {s['source_ref']} "
                                        f"({payload.get('court', '?')}, filed "
                                        f"{payload.get('filed_date', '?')})")
                                       if owner_is_decedent else None,
                    "estate_linked": None if owner_is_decedent else
                        (f"household linked to estate {s['source_ref']} "
                         f"(decedent {s['owner_name']}); titled owner is NOT the "
                         f"decedent — VA verifies linkage + owner liveness"),
                    "relatives": [{"name": h, "phone": None} for h in heirs],
                    "verified_via": ["mycase_in_docket"],
                    "discovery": {
                        "source_type": adapter.source_type,
                        "source_ref": s["source_ref"],
                        "signal_confidence": s.get("confidence"),
                        "personal_rep": payload.get("personal_rep"),
                        "which_provider": lead.get("which_provider"),
                        "parcel_provenance": lead.get("provenance", "?"),
                    },
                }
                if existing:
                    merged = {**(existing["skip_trace_meta"] or {}), **meta_patch}
                    if not args.dry_run:
                        cur.execute(
                            """update landos.owner_leads
                               set skip_trace_meta=%s, updated_at=now()
                               where lead_id=%s""",
                            (Jsonb(merged), existing["lead_id"]),
                        )
                    lead_id, action = existing["lead_id"], "FLAGGED existing"
                    flagged += 1
                else:
                    lead_id = uuid.uuid4()
                    if not args.dry_run:
                        cur.execute(
                            """insert into landos.owner_leads
                               (lead_id, tenant_id, county_fips, parcel_apn,
                                reportall_parcel_id, situs_address, situs_lat, situs_lng,
                                owner_name, owner_mailing_address, owner_mailing_city,
                                owner_mailing_state, owner_mailing_zip, land_use_class,
                                acreage, skip_trace_status, skip_trace_meta, source,
                                provenance, raw_meta, discovery_seed)
                               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                                       'not_traced',%s,'discovery',%s,%s,false)""",
                            (lead_id, TENANT, lead["county_fips"], lead["parcel_apn"],
                             lead.get("reportall_parcel_id"), lead.get("situs_address"),
                             lead.get("situs_lat"), lead.get("situs_lng"),
                             lead.get("owner_name"), lead.get("owner_mailing_address"),
                             lead.get("owner_mailing_city"), lead.get("owner_mailing_state"),
                             lead.get("owner_mailing_zip"), lead.get("land_use_class"),
                             lead.get("acreage"), Jsonb(meta_patch),
                             provenance, Jsonb(lead.get("raw_meta") or {})),
                        )
                    action = "CREATED"
                    created += 1

                if not args.dry_run:
                    for h in heirs:
                        cur.execute(
                            """select id from landos.contact_relations
                               where owner_lead_id=%s and lower(relative_name)=lower(%s)""",
                            (lead_id, h),
                        )
                        if not cur.fetchone():
                            cur.execute(
                                """insert into landos.contact_relations
                                   (owner_lead_id, relative_name, relationship,
                                    is_heir, confidence, source)
                                   values (%s,%s,'heir',true,%s,%s)""",
                                (lead_id, h, s.get("confidence", 0.5),
                                 f"{adapter.source_type}:{s['source_ref']}"),
                            )
                    cur.execute(
                        """update landos.discovery_signals
                           set status='matched', parcel_apn=%s, matched_lead_id=%s,
                               confidence=%s
                           where signal_id=%s""",
                        (lead["parcel_apn"], lead_id, s.get("confidence"),
                         sig["signal_id"]),
                    )
                print(f"  {s['source_ref']}  {s['owner_name']!r}: {action} lead "
                      f"{str(lead_id)[:8]}… apn={lead['parcel_apn']} "
                      f"owner_on_roll={lead.get('owner_name')!r} R4-tagged, "
                      f"{len(heirs)} heir(s), which_provider={lead.get('which_provider')} "
                      f"[{trail}]")

        if args.dry_run:
            conn.rollback()
            print("DRY RUN — rolled back")
        else:
            conn.commit()

    print(f"\ndiscovery summary: signals={len(signals)} created={created} "
          f"flagged={flagged} no_match={no_match} retryable={retryable}")


if __name__ == "__main__":
    main()

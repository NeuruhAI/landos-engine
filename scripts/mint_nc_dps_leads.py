#!/usr/bin/env python3
"""NC DPS -> LandOS owner_leads bridge.

This is deliberately NOT a second CRM writer. It consumes only qualified NC DPS
property packets whose score receipts say crm_ready=true and writes them into
landos.owner_leads. The existing scripts.sync_to_crm remains the sole writer to
public.acquisition_leads.

Default mode is dry-run. --apply is required to mutate landos.*. No seller
contact, skip trace, offer, filing, funds, recording, or transaction execution
is performed here.
"""
from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.nc_dps_writer import build_candidates, read_jsonl
from app.score_absentee import classify_owner_type

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def merge_meta(current, patch):
    return {**(current or {}), **patch}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--qualified-signals", required=True, type=Path)
    ap.add_argument("--packets", required=True, type=Path)
    ap.add_argument("--receipts", required=True, type=Path)
    ap.add_argument("--apply", action="store_true", help="mutate landos.*; default is dry-run")
    args = ap.parse_args()

    signals = read_jsonl(args.qualified_signals)
    packets = read_jsonl(args.packets)
    receipts = read_jsonl(args.receipts)
    candidates, summary = build_candidates(signals, packets, receipts)

    print(json.dumps(summary, indent=2, sort_keys=True))
    if not candidates:
        print("No crm_ready NC DPS properties. Nothing to mint.")
        return

    if not args.apply:
        print("\nDRY RUN — no database writes. crm_ready candidates:")
        for row in candidates[:20]:
            print(
                f"  {row['property_id']} priority={row['emerging_score']} "
                f"owner={row['owner_name']!r} tags={row['raw_meta']['signal_tags']}"
            )
        if len(candidates) > 20:
            print(f"  ... +{len(candidates) - 20} more")
        print("\nUse --apply only after reviewing this exact candidate set.")
        return

    inserted = updated = signals_upserted = signals_matched = 0
    lead_ids: list[str] = []
    signal_by_id = {str(s["signal_id"]): s for s in signals}

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        for row in candidates:
            cur.execute(
                """select lead_id, raw_meta from landos.owner_leads
                   where tenant_id=%s and county_fips=%s and parcel_apn=%s""",
                (TENANT, row["county_fips"], row["parcel_apn"]),
            )
            existing = cur.fetchone()

            if existing:
                lead_id = existing["lead_id"]
                merged_meta = merge_meta(existing["raw_meta"], row["raw_meta"])
                cur.execute(
                    """update landos.owner_leads set
                         owner_name=coalesce(nullif(owner_name,''),%s),
                         situs_address=coalesce(situs_address,%s),
                         owner_mailing_address=coalesce(owner_mailing_address,%s),
                         absentee_flag=coalesce(absentee_flag,%s),
                         confidence_score=greatest(coalesce(confidence_score,0),%s),
                         emerging_score=%s,
                         emerging_signals=%s,
                         raw_meta=%s,
                         updated_at=now()
                       where lead_id=%s""",
                    (
                        row["owner_name"], row["situs_address"], row["owner_mailing_address"],
                        row["absentee_flag"], row["confidence_score"], row["emerging_score"],
                        Jsonb(row["emerging_signals"]), Jsonb(merged_meta), lead_id,
                    ),
                )
                updated += 1
            else:
                lead_id = uuid.uuid4()
                cur.execute(
                    """insert into landos.owner_leads
                       (lead_id, tenant_id, county_fips, parcel_apn, owner_name,
                        owner_type, situs_address, owner_mailing_address,
                        absentee_flag, confidence_score, emerging_score,
                        emerging_signals, discovery_seed, source, provenance,
                        raw_meta)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                               false,%s,%s,%s)""",
                    (
                        lead_id, TENANT, row["county_fips"], row["parcel_apn"],
                        row["owner_name"], classify_owner_type(row["owner_name"]),
                        row["situs_address"], row["owner_mailing_address"],
                        row["absentee_flag"], row["confidence_score"],
                        row["emerging_score"], Jsonb(row["emerging_signals"]),
                        row["source"], row["provenance"], Jsonb(row["raw_meta"]),
                    ),
                )
                inserted += 1

            lead_ids.append(str(lead_id))

            for signal_id in row["signal_ids"]:
                signal = signal_by_id[signal_id]
                detail = signal.get("detail_json") or {}
                source_type = str(signal["source_type"])
                source_ref = f"nc_dps:{signal_id}"
                cur.execute(
                    """insert into landos.discovery_signals
                       (tenant_id, source_type, source_ref, source_url, county_fips,
                        raw_payload, parcel_apn, owner_name, signal_date, confidence,
                        status, matched_lead_id)
                       values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'matched',%s)
                       on conflict (source_type, source_ref) do update set
                         source_url=excluded.source_url,
                         raw_payload=excluded.raw_payload,
                         parcel_apn=excluded.parcel_apn,
                         owner_name=excluded.owner_name,
                         signal_date=excluded.signal_date,
                         confidence=excluded.confidence,
                         status='matched',
                         matched_lead_id=excluded.matched_lead_id""",
                    (
                        TENANT, source_type, source_ref, detail.get("source_url"),
                        signal["geography"], Jsonb(detail), signal.get("parcel_apn"),
                        signal.get("owner_name"), signal.get("event_date") or None,
                        signal.get("confidence", 0.5), lead_id,
                    ),
                )
                signals_upserted += 1
                signals_matched += 1

        conn.commit()

    print(
        f"NC DPS LandOS mint complete: inserted={inserted} updated={updated} "
        f"signals_upserted={signals_upserted} signals_matched={signals_matched}"
    )
    print("Next existing writer: python -m scripts.sync_to_crm " + " ".join(f"--lead-id {lid}" for lid in lead_ids))


if __name__ == "__main__":
    main()

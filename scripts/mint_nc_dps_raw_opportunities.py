#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

# Direct-file execution puts ``scripts/`` rather than the repository root on
# sys.path. Repair only that invocation mode; ``python -m`` remains unchanged.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.nc_dps_raw_writer import build_raw_candidates, read_jsonl
from app.score_absentee import classify_owner_type

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def merge_meta(current, patch):
    return {**(current or {}), **patch}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--signals", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="mutate landos.owner_leads; default is dry-run")
    args = parser.parse_args()

    signals = read_jsonl(args.signals)
    candidates, summary = build_raw_candidates(signals)
    print(json.dumps(summary, indent=2, sort_keys=True))

    if not candidates:
        print("No property-identifiable raw NC DPS opportunities found.")
        return

    if not args.apply:
        print("\nDRY RUN — no database writes. Raw opportunities:")
        for row in candidates[:50]:
            nc = row["emerging_signals"]["nc_dps"]
            print(
                f"  {row['property_id']} owner={row['owner_name']!r} "
                f"signals={','.join(nc['signal_types'])} out_of_state={nc['out_of_state_observed']} "
                f"tax_balance={nc['tax_balance_observed']}"
            )
        if len(candidates) > 50:
            print(f"  ... +{len(candidates) - 50} more")
        print("\nNo score threshold or qualification is required for raw intake.")
        print("Use --apply only after reviewing the exact raw opportunity set.")
        return

    # Database/config dependencies are intentionally imported only for --apply.
    # The default dry-run must work in a clean repo checkout without psycopg or
    # a DATABASE_URL environment variable.
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb

    from app.config import DATABASE_URL

    inserted = updated = 0
    lead_ids: list[str] = []

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
                         emerging_signals=%s,
                         raw_meta=%s,
                         source=coalesce(nullif(source,''),%s),
                         provenance=coalesce(nullif(provenance,''),%s),
                         updated_at=now()
                       where lead_id=%s""",
                    (
                        row["owner_name"], row["situs_address"], row["owner_mailing_address"],
                        row["absentee_flag"], row["confidence_score"],
                        Jsonb(row["emerging_signals"]), Jsonb(merged_meta), row["source"],
                        row["provenance"], lead_id,
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
                        row["absentee_flag"], row["confidence_score"], row["emerging_score"],
                        Jsonb(row["emerging_signals"]), row["source"], row["provenance"],
                        Jsonb(row["raw_meta"]),
                    ),
                )
                inserted += 1

            lead_ids.append(str(lead_id))

        conn.commit()

    print(f"NC DPS raw opportunity mint complete: inserted={inserted} updated={updated}")
    print("Raw opportunities remain research-stage; contact_authorized=false.")
    print("Existing CRM projection path: python -m scripts.sync_to_crm " + " ".join(f"--lead-id {lead_id}" for lead_id in lead_ids))


if __name__ == "__main__":
    main()

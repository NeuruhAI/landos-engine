#!/usr/bin/env python3
"""Give the NC projection back its own property ZIP and its own street line.

TWO FIELDS, TWO FAULTS, ONE ROOT CAUSE -- the export that built the batch put a
mailing-side value into a property-side column and then put the ZIP into the
street text as well.

  situs_zip        On 889 of 889 rows where both are populated, situs_zip equals
                   mailing_zip. Zero disagreements. 789 NC parcels carry a ZIP
                   that is not an NC ZIP, and the product renders
                   "3464 N DAVIDSON ST CHARLOTTE NC, NC 29662" -- 29662 is
                   Mauldin, SOUTH CAROLINA, the owner's mailing ZIP, on a
                   Charlotte parcel. APN 05512302 carries the proof on one row:
                   its address line reads "6601 SULLINS RD CHARLOTTE NC -28214"
                   and its situs_zip column reads 28130.

  mailing_address  "1780 BENNETT RD 30017, GRAYSON, GA, 30017". The ZIP is
                   inside the street segment AND at the end. A delivery line
                   built from this is broken.

WHAT IS AUTHORITATIVE. The property line itself -- normalized->>'propertyAddress'
for the property side, the pre-comma segment of mailing_address for the mailing
side. Both come straight from the county source. A ZIP is written only when the
authoritative text proves it, or when the owner demonstrably lives at the
property so the one ZIP describes both places. Otherwise the field is blanked:
UNKNOWN is a fact and a borrowed ZIP is a lie.

    property_zip_for() in app/contact/estate_fields.py already implements that
    rule and is already under test. This script does not restate the rule; it
    applies it to the rows in production.

WHAT IT DOES NOT DO. No regex sweep over production. Every row is decided from
its own authoritative source text, the pre-image of every field it rewrites is
recorded in public.address_semantics_repair_log before the write, and re-running
converges: a row already correct produces no update.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contact_authority import contact_dsn_or_exit  # noqa: E402

from app.contact.address_normalize import extract_zip5, strip_leading_house
from app.contact.estate_fields import owner_occupies_property, property_zip_for
from app.custody.sync_guard import prepare_output_dir

CANONICAL_BATCH_PREDICATE = """
  b.import_kind = 'data_estate'
  and b.is_test is false and b.is_synthetic is false
  and (b.source_context ->> 'dataset') = 'CURRENT_CANONICAL'
  and coalesce((b.source_context ->> 'authoritative')::boolean, false)
  and coalesce((b.source_context ->> 'current')::boolean, false)
"""

SELECT_ROWS = f"""
select r.row_id,
       r.tenant_id,
       r.normalized ->> 'apn'             as apn,
       r.normalized ->> 'propertyAddress' as property_address_raw,
       r.normalized ->> 'propertyCity'    as property_city,
       r.normalized ->> 'propertyZip'     as property_zip,
       r.normalized ->> 'state'           as property_state,
       l.lead_id,
       l.mailing_address,
       l.mailing_city,
       l.mailing_state,
       l.mailing_zip
  from lead_import_rows r
  join lead_import_batches b
    on b.batch_id = r.batch_id and b.tenant_id = r.tenant_id
  left join acquisition_leads l
    on l.lead_id = r.linked_lead_id and l.tenant_id = r.tenant_id
 where {CANONICAL_BATCH_PREDICATE}
   and r.candidate_class in ('QUALIFIED_CANDIDATE', 'RESEARCH_CANDIDATE')
   and coalesce(r.normalized ->> 'state', '') = 'NC'
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def street_segment(mailing_address: str) -> str:
    """The part of a composed mailing line that is meant to be the street."""
    return (mailing_address or "").split(",")[0].strip()


def repaired_street(mailing_address: str, mailing_zip: str) -> str:
    """Remove a trailing ZIP token from the street segment, and only there.

    The ZIP is removed when it is the LAST token of the street segment and equal
    to the row's own mailing_zip. A street legitimately containing five digits --
    a house number, a highway number, a unit -- is untouched because it is not
    the trailing token and does not match the ZIP.
    """
    street = street_segment(mailing_address)
    if not street or not mailing_zip:
        return mailing_address or ""
    cleaned = re.sub(rf"\s+{re.escape(mailing_zip)}(?:-\d{{4}})?$", "", street).strip()
    if cleaned == street or not cleaned:
        return mailing_address or ""
    rest = (mailing_address or "")[len(street_segment(mailing_address)):]
    return cleaned + rest


def plan(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(SELECT_ROWS)
        rows = cur.fetchall()

    zip_changes, street_changes = [], []
    reasons = Counter()
    for row in rows:
        estate_row = {
            "property_address_raw": row["property_address_raw"] or "",
            "property_address_normalized": "",
            "property_city": row["property_city"] or "",
            "property_zip": row["property_zip"] or "",
            "property_state": row["property_state"] or "",
            "mailing_address": street_segment(row["mailing_address"] or ""),
            "mailing_city": row["mailing_city"] or "",
            "mailing_zip": row["mailing_zip"] or "",
        }
        declared = (row["property_zip"] or "").strip()
        corrected = property_zip_for(estate_row)
        if corrected != declared:
            line_zip = extract_zip5(strip_leading_house(row["property_address_raw"]))
            from app.contact.zip_state import zip_plausible_for_state
            if line_zip and corrected == line_zip:
                reason = "PROPERTY_LINE_CARRIES_ITS_OWN_ZIP"
            elif not corrected and declared:
                if declared == (row["mailing_zip"] or "").strip():
                    reason = "MAILING_ZIP_COPY_OWNER_NOT_AT_PROPERTY"
                elif not zip_plausible_for_state(extract_zip5(declared),
                                                 row["property_state"] or ""):
                    reason = "ZIP_IMPOSSIBLE_FOR_PROPERTY_STATE"
                else:
                    reason = "DECLARED_ZIP_NOT_CORROBORATED"
            else:
                reason = "RECOMPUTED"
            reasons[reason] += 1
            zip_changes.append({
                "row_id": str(row["row_id"]), "apn": row["apn"],
                "before": declared, "after": corrected, "reason": reason,
                "property_line": row["property_address_raw"], "mailing_zip": row["mailing_zip"],
                "owner_occupies": owner_occupies_property(estate_row),
            })

        mailing = row["mailing_address"] or ""
        if mailing and row["lead_id"]:
            fixed = repaired_street(mailing, (row["mailing_zip"] or "").strip())
            if fixed != mailing:
                street_changes.append({
                    "lead_id": str(row["lead_id"]), "apn": row["apn"],
                    "before": mailing, "after": fixed,
                    "reason": "ZIP_SPLICED_INTO_STREET_SEGMENT",
                })

    return {
        "scanned": len(rows),
        "zip_changes": zip_changes,
        "street_changes": street_changes,
        "zip_reasons": dict(reasons),
    }


def snapshot_counts(conn) -> dict:
    q = """
    select count(*) as nc_rows,
           count(*) filter (where coalesce(situs_zip,'') <> '') as zip_populated,
           count(*) filter (where coalesce(situs_zip,'') <> ''
                              and left(situs_zip,2) not in ('27','28')) as non_nc_prefix,
           count(*) filter (where coalesce(situs_zip,'') <> '' and coalesce(mailing_zip,'') <> ''
                              and situs_zip = mailing_zip) as zip_equals_mailing,
           count(*) filter (where coalesce(mailing_address,'') <> '' and coalesce(mailing_zip,'') <> ''
                              and position(mailing_zip in mailing_address) > 0
                              and position(mailing_zip in mailing_address)
                                  < position(',' in mailing_address)) as street_carries_zip
      from public.deedsonar_current_candidates_v1
     where source_state = 'NC'
    """
    with conn.cursor() as cur:
        cur.execute(q)
        return cur.fetchone()


def apply(conn, result: dict, run_id: str) -> dict:
    """One transaction, set-based, pre-images recorded before the write."""
    written = {"zip": 0, "street": 0}
    with conn.cursor() as cur:
        cur.execute("""
            create temporary table t24a_zip_repair (
              row_id uuid primary key, before_value text, after_value text, reason text
            ) on commit drop
        """)
        cur.execute("""
            create temporary table t24a_street_repair (
              lead_id uuid primary key, before_value text, after_value text, reason text
            ) on commit drop
        """)
        with cur.copy("copy t24a_zip_repair (row_id, before_value, after_value, reason) from stdin") as cp:
            for c in result["zip_changes"]:
                cp.write_row((c["row_id"], c["before"], c["after"], c["reason"]))
        with cur.copy("copy t24a_street_repair (lead_id, before_value, after_value, reason) from stdin") as cp:
            for c in result["street_changes"]:
                cp.write_row((c["lead_id"], c["before"], c["after"], c["reason"]))

        cur.execute("""
            insert into public.address_semantics_repair_log
              (run_id, scope, target_id, field, before_value, after_value, rule, applied_at)
            select %s, 'lead_import_rows.normalized', row_id::text, 'propertyZip',
                   before_value, after_value, reason, now()
              from t24a_zip_repair
            on conflict (run_id, scope, target_id, field) do nothing
        """, (run_id,))
        cur.execute("""
            insert into public.address_semantics_repair_log
              (run_id, scope, target_id, field, before_value, after_value, rule, applied_at)
            select %s, 'acquisition_leads', lead_id::text, 'mailing_address',
                   before_value, after_value, reason, now()
              from t24a_street_repair
            on conflict (run_id, scope, target_id, field) do nothing
        """, (run_id,))

        # jsonb_set with a '' value still writes a key, which is what we want:
        # the key stays present and empty rather than disappearing, so the
        # contract shape of `normalized` does not change under repair.
        cur.execute("""
            update lead_import_rows r
               set normalized = jsonb_set(coalesce(r.normalized, '{}'::jsonb),
                                          '{propertyZip}', to_jsonb(z.after_value))
              from t24a_zip_repair z
             where r.row_id = z.row_id
               and coalesce(r.normalized ->> 'propertyZip', '') is distinct from z.after_value
        """)
        written["zip"] = cur.rowcount
        cur.execute("""
            update acquisition_leads l
               set mailing_address = s.after_value
              from t24a_street_repair s
             where l.lead_id = s.lead_id
               and l.mailing_address is distinct from s.after_value
        """)
        written["street"] = cur.rowcount
    return written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out-dir", default=str(Path.home() / "neuruh-private" / "contact-intelligence"
                                            / "runs" / "T24A_ADDRESS_SEMANTICS"))
    args = ap.parse_args()

    # DEEDSONAR_DATABASE_URL, then DATABASE_URL; the retired legacy DB is refused (exit 2).
    dsn = contact_dsn_or_exit()

    out = prepare_output_dir(args.out_dir, what="address semantics repair evidence")
    run_id = f"t24a-{utcnow()}"

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        pre = snapshot_counts(conn)
        result = plan(conn)
        expected = {
            "PROPERTY_ZIP_ROWS_TO_CHANGE": len(result["zip_changes"]),
            "PROPERTY_ZIP_REASONS": result["zip_reasons"],
            "MAILING_STREET_ROWS_TO_CHANGE": len(result["street_changes"]),
            "EXPECTED_NON_NC_PREFIX_AFTER": 0,
            "EXPECTED_STREET_CARRIES_ZIP_AFTER": 0,
        }
        (out / "PRE_REPAIR_SNAPSHOT.json").write_text(json.dumps(pre, indent=1, default=str))
        (out / "REPAIR_PLAN.json").write_text(json.dumps(
            {"run_id": run_id, "scanned": result["scanned"],
             "zip_changes": result["zip_changes"],
             "street_changes": result["street_changes"]}, indent=1, default=str))
        (out / "EXPECTED_DIFF.json").write_text(json.dumps(expected, indent=1, default=str))
        for name in ("PRE_REPAIR_SNAPSHOT.json", "REPAIR_PLAN.json", "EXPECTED_DIFF.json"):
            os.chmod(out / name, 0o600)

        print(json.dumps({"mode": "APPLY" if args.apply else "DRY_RUN",
                          "run_id": run_id, "pre": pre, "expected": expected,
                          "evidence_dir": str(out)}, indent=1, default=str))
        if not args.apply:
            return 0

        written = apply(conn, result, run_id)
        conn.commit()
        post = snapshot_counts(conn)
        receipt = {"run_id": run_id, "applied_at": utcnow(), "written": written,
                   "pre": pre, "post": post, "expected": expected}
        (out / "APPLY_RECEIPT.json").write_text(json.dumps(receipt, indent=1, default=str))
        os.chmod(out / "APPLY_RECEIPT.json", 0o600)
        print(json.dumps(receipt, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

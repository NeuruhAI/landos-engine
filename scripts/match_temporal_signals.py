"""
match_temporal_signals.py — exact parcel resolver for already-staged temporal signals.

    python -m scripts.match_temporal_signals --source code_violation --dry-run

This is the no-spend resolver for signal rows that already carry a canonical
county APN. It does NOT create owner_leads, geocode, call providers, or write
public.*. It only links an unmatched landos.discovery_signals row to an existing
landos.owner_leads row when the exact (county_fips, parcel_apn) join is
unambiguous.

Taxsale stays on its dedicated minter path because that source creates new
inventory. This resolver is for enrichment/distress overlays like
code_violation and lis_pendens.
"""
import argparse
from typing import Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

DEFAULT_SOURCES = ("code_violation", "lis_pendens", "rental_license")

CANDIDATES = """
    with candidate_matches as (
        select ds.signal_id, ds.source_type, ds.source_ref, ds.county_fips,
               ds.parcel_apn, ol.lead_id, ol.owner_name
        from landos.discovery_signals ds
        join landos.owner_leads ol
          on ol.county_fips = ds.county_fips
         and ol.parcel_apn = ds.parcel_apn
        where ds.signal_class = 'temporal'
          and ds.status = 'new'
          and ds.matched_lead_id is null
          and ds.parcel_apn is not null
          and ds.source_type = any(%(sources)s)
    )
    select signal_id, source_type, source_ref, county_fips, parcel_apn,
           array_agg(lead_id order by lead_id) as lead_ids,
           array_agg(owner_name order by lead_id) as owner_names,
           count(distinct lead_id) as lead_count
    from candidate_matches
    group by signal_id, source_type, source_ref, county_fips, parcel_apn
    order by source_type, county_fips, parcel_apn, source_ref
"""

TOTALS = """
    select source_type,
           count(*) as total_new_unmatched,
           count(*) filter (where parcel_apn is null) as missing_apn
    from landos.discovery_signals
    where signal_class = 'temporal'
      and status = 'new'
      and matched_lead_id is null
      and source_type = any(%(sources)s)
    group by source_type
    order by source_type
"""

MATCH = """
    update landos.discovery_signals
       set matched_lead_id = %(lead_id)s,
           status = 'matched'
     where signal_id = %(signal_id)s
       and matched_lead_id is null
       and status = 'new'
"""


def _sources(values: Sequence[str] | None) -> list[str]:
    selected = list(values or DEFAULT_SOURCES)
    return sorted(set(s.strip() for s in selected if s.strip()))


def main(sources: Iterable[str] | None = None, dry_run: bool = False) -> dict:
    selected = _sources(list(sources) if sources is not None else None)
    matched = ambiguous = 0
    preview = []

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(TOTALS, {"sources": selected})
        totals = [dict(r) for r in cur.fetchall()]

        cur.execute(CANDIDATES, {"sources": selected})
        candidates = cur.fetchall()

        for row in candidates:
            lead_ids = row["lead_ids"] or []
            owner_names = row["owner_names"] or []
            if row["lead_count"] != 1:
                ambiguous += 1
                preview.append(("AMBIG", row, lead_ids, owner_names))
                continue

            lead_id = lead_ids[0]
            matched += 1
            preview.append(("MATCH", row, [lead_id], owner_names[:1]))
            if not dry_run:
                cur.execute(MATCH, {"signal_id": row["signal_id"], "lead_id": lead_id})
                if cur.rowcount != 1:
                    raise RuntimeError(f"expected to match 1 row for signal {row['signal_id']}, got {cur.rowcount}")

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"\n=== TEMPORAL MATCH {mode} — sources={','.join(selected)} ===")
    for row in totals:
        print(f"  [{row['source_type']}] new_unmatched={row['total_new_unmatched']} missing_apn={row['missing_apn']}")
    print(f"  would_match={matched}" if dry_run else f"  matched={matched}")
    print(f"  ambiguous={ambiguous} (left untouched)")

    for kind, row, lead_ids, owner_names in preview[:20]:
        lead = str(lead_ids[0]) if lead_ids else "-"
        owner = owner_names[0] if owner_names else None
        print(f"  {kind} [{row['source_type']}] {row['parcel_apn']} -> {lead} owner={owner!r}")
    if len(preview) > 20:
        print(f"  ... +{len(preview) - 20} more")

    if dry_run:
        print("\nNO writes. Re-run without --dry-run to apply exact unambiguous matches.")

    return {"dry_run": dry_run, "sources": selected, "matched": matched,
            "ambiguous": ambiguous, "totals": totals}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Exact resolver for staged temporal signals")
    ap.add_argument("--source", action="append", choices=DEFAULT_SOURCES,
                    help="source_type to match; repeatable. Defaults to code_violation + lis_pendens")
    ap.add_argument("--dry-run", action="store_true", help="preview matches; write nothing")
    args = ap.parse_args()
    main(args.source, dry_run=args.dry_run)

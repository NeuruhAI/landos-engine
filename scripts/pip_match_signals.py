"""
pip_match_signals.py — point-in-polygon resolver for spatial_deferred signals
(geometry_infra_spec Part 3).

    python -m scripts.pip_match_signals --source ground_lease --dry-run

The no-spend spatial sibling of match_temporal_signals: for signal rows that
carry a lat/lng point but NO parcel APN (FCC towers etc., staged with
match_method=spatial_deferred), resolve the point to the parcel whose polygon
contains it (ST_Contains over landos.parcel_geom), stamp the resolved APN +
provenance on the signal, and — exactly like the temporal resolver — link to an
owner_leads row ONLY when the (county_fips, parcel_apn) join is unambiguous.

Honesty rules (all from the spec / house doctrine):
  * ambiguous PIP (point inside >=2 polygons)   -> untouched, logged, never guess
  * point outside every polygon                 -> untouched, logged ('outside')
  * county with zero parcel_geom coverage       -> named gate ('geometry_dark'),
                                                   e.g. Berrien 26021 until MI
                                                   polygons exist
  * parcel resolved but no/ambiguous book lead  -> APN + provenance stamped,
                                                   status stays 'new' (a future
                                                   book pickup), matched only on
                                                   an unambiguous lead
It does NOT create owner_leads, geocode, call providers, or write public.*.
"""
import argparse
import json
from typing import Any, Dict, Sequence

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

DEFAULT_SOURCES = ("ground_lease",)

GEOM_COVERAGE = """
    select county_fips, count(*) as polys
    from landos.parcel_geom
    group by county_fips
"""

# One pass: every eligible point classified against parcel polygons in its own
# county. count(distinct parcel_apn) drives the unambiguous-only rule.
PIP_CLASSIFY = """
    with pts as (
        select ds.signal_id, ds.source_type, ds.source_ref, ds.county_fips,
               (ds.raw_payload->>'lat')::float8 as lat,
               (ds.raw_payload->>'lng')::float8 as lng
        from landos.discovery_signals ds
        where ds.source_type = any(%(sources)s)
          and ds.status = 'new'
          and ds.matched_lead_id is null
          and coalesce(ds.parcel_apn, '') = ''
          and (ds.raw_payload->>'lat') ~ '^-?[0-9]+(\\.[0-9]+)?$'
          and (ds.raw_payload->>'lng') ~ '^-?[0-9]+(\\.[0-9]+)?$'
    )
    select p.signal_id, p.source_type, p.source_ref, p.county_fips,
           count(distinct h.parcel_apn) as n_parcels,
           min(h.parcel_apn) as parcel_apn
    from pts p
    left join lateral (
        select g.parcel_apn
        from landos.parcel_geom g
        where g.county_fips = p.county_fips
          and ST_Contains(g.geom, ST_SetSRID(ST_MakePoint(p.lng, p.lat), 4326))
    ) h on true
    group by p.signal_id, p.source_type, p.source_ref, p.county_fips
    order by p.county_fips, p.source_ref
"""

LEAD_CANDIDATES = """
    select ol.county_fips, ol.parcel_apn,
           array_agg(ol.lead_id order by ol.lead_id) as lead_ids,
           array_agg(ol.owner_name order by ol.lead_id) as owner_names
    from landos.owner_leads ol
    join unnest(%(fips)s::text[], %(apns)s::text[]) as want(county_fips, parcel_apn)
      on ol.county_fips = want.county_fips
     and ol.parcel_apn = want.parcel_apn
    group by ol.county_fips, ol.parcel_apn
"""

# APN + provenance stamp — additive jsonb merge, guarded to the untouched state.
STAMP = """
    update landos.discovery_signals
       set parcel_apn = %(parcel_apn)s,
           raw_payload = coalesce(raw_payload, '{}'::jsonb)
                         || jsonb_build_object(
                                'match_method', 'pip_parcel',
                                'pip_resolve', %(prov)s::jsonb)
     where signal_id = %(signal_id)s
       and matched_lead_id is null
       and status = 'new'
       and coalesce(parcel_apn, '') = ''
"""

# Same MATCH contract as match_temporal_signals: unambiguous lead only.
MATCH = """
    update landos.discovery_signals
       set matched_lead_id = %(lead_id)s,
           status = 'matched'
     where signal_id = %(signal_id)s
       and matched_lead_id is null
       and status = 'new'
"""


def classify(row: Dict[str, Any], geom_counties: Dict[str, int]) -> str:
    """PIP outcome for one classified point row.
    geometry_dark — its county has NO parcel polygons at all (named gate);
    outside      — polygons exist but none contains the point;
    ambiguous    — point inside >=2 distinct parcels (never guess);
    resolved     — exactly one containing parcel."""
    if not geom_counties.get(row["county_fips"]):
        return "geometry_dark"
    n = int(row["n_parcels"] or 0)
    if n == 0:
        return "outside"
    if n > 1:
        return "ambiguous"
    return "resolved"


def _sources(values: Sequence[str] | None) -> list[str]:
    selected = list(values or DEFAULT_SOURCES)
    return sorted(set(s.strip() for s in selected if s.strip()))


def main(sources: Sequence[str] | None = None, dry_run: bool = False) -> dict:
    selected = _sources(list(sources) if sources is not None else None)
    counts = {"resolved": 0, "matched": 0, "lead_none": 0, "lead_ambiguous": 0,
              "ambiguous": 0, "outside": 0, "geometry_dark": 0}
    dark_counties: Dict[str, int] = {}
    preview = []

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(GEOM_COVERAGE)
        geom_counties = {r["county_fips"]: r["polys"] for r in cur.fetchall()}

        cur.execute(PIP_CLASSIFY, {"sources": selected})
        points = cur.fetchall()

        resolved = []
        for row in points:
            kind = classify(row, geom_counties)
            if kind == "geometry_dark":
                counts["geometry_dark"] += 1
                dark_counties[row["county_fips"]] = dark_counties.get(row["county_fips"], 0) + 1
            elif kind == "outside":
                counts["outside"] += 1
                preview.append(("OUTSIDE", row, None))
            elif kind == "ambiguous":
                counts["ambiguous"] += 1
                preview.append(("AMBIG-PIP", row, None))
            else:
                counts["resolved"] += 1
                resolved.append(row)

        leads = {}
        if resolved:
            cur.execute(LEAD_CANDIDATES, {
                "fips": [r["county_fips"] for r in resolved],
                "apns": [r["parcel_apn"] for r in resolved]})
            leads = {(r["county_fips"], r["parcel_apn"]): r for r in cur.fetchall()}

        for row in resolved:
            prov = json.dumps({"via": "landos.parcel_geom ST_Contains",
                               "spec": "geometry_infra_spec Part 3",
                               "was": "spatial_deferred"})
            cand = leads.get((row["county_fips"], row["parcel_apn"]))
            lead_ids = (cand or {}).get("lead_ids") or []
            if len(lead_ids) == 1:
                counts["matched"] += 1
                verdict, lead_id = "MATCH", lead_ids[0]
            elif len(lead_ids) > 1:
                counts["lead_ambiguous"] += 1
                verdict, lead_id = "AMBIG-LEAD", None
            else:
                counts["lead_none"] += 1
                verdict, lead_id = "APN-ONLY", None
            preview.append((verdict, row, lead_id))
            if dry_run:
                continue
            cur.execute(STAMP, {"signal_id": row["signal_id"],
                                "parcel_apn": row["parcel_apn"], "prov": prov})
            if cur.rowcount != 1:
                raise RuntimeError(f"expected to stamp 1 row for signal {row['signal_id']}, got {cur.rowcount}")
            if lead_id is not None:
                cur.execute(MATCH, {"signal_id": row["signal_id"], "lead_id": lead_id})
                if cur.rowcount != 1:
                    raise RuntimeError(f"expected to match 1 row for signal {row['signal_id']}, got {cur.rowcount}")

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

    mode = "DRY-RUN" if dry_run else "APPLY"
    print(f"\n=== PIP MATCH {mode} — sources={','.join(selected)} ===")
    print(f"  eligible_points={len(points)}")
    print(f"  resolved_parcel={counts['resolved']} "
          f"(matched_lead={counts['matched']} apn_only={counts['lead_none']} "
          f"lead_ambiguous={counts['lead_ambiguous']})")
    print(f"  ambiguous_pip={counts['ambiguous']} outside={counts['outside']} (untouched)")
    for fips, n in sorted(dark_counties.items()):
        print(f"  geometry_dark [{fips}]: {n} held — named gate, no parcel polygons loaded")

    for verdict, row, lead_id in preview[:25]:
        print(f"  {verdict} [{row['source_type']}] {row['source_ref']} "
              f"{row['county_fips']}/{row.get('parcel_apn') or '-'}"
              + (f" -> {lead_id}" if lead_id else ""))
    if len(preview) > 25:
        print(f"  ... +{len(preview) - 25} more")

    if dry_run:
        print("\nNO writes. Re-run without --dry-run to apply.")

    return {"dry_run": dry_run, "sources": selected, "eligible": len(points),
            **counts, "dark_counties": dark_counties}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="PIP resolver for spatial_deferred signals")
    ap.add_argument("--source", action="append",
                    help="source_type to resolve; repeatable (default: ground_lease)")
    ap.add_argument("--dry-run", action="store_true", help="preview; write nothing")
    args = ap.parse_args()
    main(args.source, dry_run=args.dry_run)

"""
enrich_owner_chain.py — A2: owner enrichment for the discovered-taxsale cohort via the
ParcelProviderChain (county_rest tier, $0).

    python -m scripts.enrich_owner_chain [--samples N] [--batch] [--county FIPS]

The Jun-30 MI tax-sale mint (Berrien/Allegan/Van Buren/Cass) landed property-keyed:
parcel_apn + county only, owner/mailing/situs honest-NULL (tax-sale.info carries no owner).
This points the EXISTING provider chain at those rows and fills, from the county's own
parcel layer: owner_name, owner_mailing_address/_city/_state/_zip, situs_address (where the
layer carries it) — the exact fields skip_trace_tracerfy.py's TARGET_QUERY needs.
owner_type is re-stamped from the REAL name via app.score_absentee.classify_owner_type
(the mint stamped 'individual' blind on NULL names).

Modes:
  (default)    dry-run over the whole cohort — lookups happen, nothing is written
  --samples N  first N per county only, with cross-verification columns (layer SEV/acres
               vs the tax-sale.info sev/property_type we already hold) — the STEP-2b gate
  --batch      write. Idempotent: only rows with owner_name IS NULL are selected and the
               UPDATE re-guards on owner_name IS NULL.

Counties come from COUNTY_REST_ENDPOINTS (verified=True rows only — the provider refuses
unverified mappings). Van Buren 26159 has no free source (STEP-2d stop) and is absent.
Polite: >=1.2s between chain calls, provider sends the honest engine UA. Match discipline:
exact canonical-APN equality; 0 hits = miss (stays NULL), >1 = ambiguous (skipped, counted).
"""
import argparse
import time

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from app.providers.chain import ParcelProviderChain
from app.providers.config import COUNTY_REST_ENDPOINTS
from app.score_absentee import classify_owner_type

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
PROVENANCE = "discovered_taxsale"
SPACING_S = 1.2

COHORT = """
    select lead_id, county_fips, parcel_apn,
           raw_meta->>'sev' as ts_sev, raw_meta->>'property_type' as ts_prop
    from landos.owner_leads
    where tenant_id = %s and provenance = %s
      and owner_name is null
      and county_fips = any(%s)
    order by county_fips, parcel_apn
"""

WRITE = """
    update landos.owner_leads
    set owner_name = %(owner_name)s,
        owner_type = %(owner_type)s,
        owner_mailing_address = %(mail_street)s,
        owner_mailing_city = %(mail_city)s,
        owner_mailing_state = %(mail_state)s,
        owner_mailing_zip = %(mail_zip)s,
        situs_address = coalesce(situs_address, %(situs)s),
        updated_at = now()
    where lead_id = %(lead_id)s and owner_name is null
"""


def _layer_evidence(parcel):
    """SEV/acres the county layer holds, for side-by-side audit vs tax-sale.info."""
    a = (parcel.get("raw_meta") or {}).get("county_rest_attributes") or {}
    for k in ("sev24", "SEV", "Acres", "acres", "AC"):
        if a.get(k) not in (None, "", " "):
            return f"{k}={a[k]}"
    return ""


def _pick(results, apn):
    """Exact canonical-APN match only. Returns (parcel|None, 'hit'|'miss'|'ambiguous')."""
    exact = [r for r in results if (r.get("parcel_apn") or "").strip() == apn]
    if not exact:
        return None, "miss"
    if len(exact) > 1:
        owners = {(r.get("owner_name") or "").strip() for r in exact}
        if len(owners) > 1:
            return None, "ambiguous"
    return exact[0], "hit"


def main(samples=None, batch=False, county=None):
    counties = sorted(f for f, c in COUNTY_REST_ENDPOINTS.items() if c.get("verified"))
    if county:
        counties = [f for f in counties if f == county]
    if not counties:
        raise SystemExit(f"no verified county_rest config for {county!r}")

    chain = ParcelProviderChain(TENANT)
    stats = {f: {"attempted": 0, "hit": 0, "miss": 0, "ambiguous": 0, "unavailable": 0,
                 "mail": 0, "situs": 0, "entity": 0, "govt": 0} for f in counties}

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(COHORT, (TENANT, PROVENANCE, counties))
        rows = cur.fetchall()
        per_county_seen = {f: 0 for f in counties}
        mode = f"samples={samples}" if samples else ("BATCH" if batch else "dry-run")
        print(f"enrich_owner_chain [{mode}] — cohort rows selected: {len(rows)} across {counties}")

        for r in rows:
            f = r["county_fips"]
            if samples and per_county_seen[f] >= samples:
                continue
            per_county_seen[f] += 1
            st = stats[f]
            st["attempted"] += 1

            results = chain.lookup_by_apn(f, r["parcel_apn"])
            if not results and chain.miss_is_retryable():
                st["unavailable"] += 1
                print(f"  UNAVAILABLE {f} {r['parcel_apn']}  trail={chain.trail_str()}")
                time.sleep(SPACING_S)
                continue
            parcel, verdict = _pick(results, r["parcel_apn"])
            st[verdict if verdict != "hit" else "hit"] += 1

            if verdict != "hit":
                print(f"  {verdict.upper()} {f} {r['parcel_apn']} ({len(results)} rows) trail={chain.trail_str()}")
                time.sleep(SPACING_S)
                continue

            owner = parcel.get("owner_name")
            otype = classify_owner_type(owner)
            if otype in ("entity", "govt"):
                st[otype] += 1
            if parcel.get("owner_mailing_address"):
                st["mail"] += 1
            if parcel.get("situs_address"):
                st["situs"] += 1

            if samples:
                print(f"  [{f}] {r['parcel_apn']}")
                print(f"      layer: owner={owner!r} [{otype}] mail={parcel.get('owner_mailing_address')!r}, "
                      f"{parcel.get('owner_mailing_city')!r} {parcel.get('owner_mailing_state')!r} "
                      f"{parcel.get('owner_mailing_zip')!r} situs={parcel.get('situs_address')!r} "
                      f"| {_layer_evidence(parcel)}")
                print(f"      ours : sev={r['ts_sev']!r} prop={(r['ts_prop'] or '')[:70]!r}")
            elif batch:
                cur.execute(WRITE, {
                    "owner_name": owner,
                    "owner_type": otype,
                    "mail_street": parcel.get("owner_mailing_address"),
                    "mail_city": parcel.get("owner_mailing_city"),
                    "mail_state": parcel.get("owner_mailing_state"),
                    "mail_zip": parcel.get("owner_mailing_zip"),
                    "situs": parcel.get("situs_address"),
                    "lead_id": r["lead_id"],
                })
            time.sleep(SPACING_S)

        if batch:
            conn.commit()
        else:
            conn.rollback()

    print("\n=== enrich_owner_chain summary ===")
    for f in counties:
        s = stats[f]
        if not s["attempted"]:
            continue
        print(f"  {f}: attempted={s['attempted']} hit={s['hit']} miss={s['miss']} "
              f"ambiguous={s['ambiguous']} unavailable={s['unavailable']} | "
              f"mail={s['mail']} situs={s['situs']} entity={s['entity']} govt={s['govt']}")
    if not batch:
        print("dry-run/samples — NO writes. Re-run with --batch to write.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A2 — cohort owner enrichment via ParcelProviderChain")
    ap.add_argument("--samples", type=int, help="verify N per county (read-only, evidence columns)")
    ap.add_argument("--batch", action="store_true", help="write owner fields to landos.owner_leads")
    ap.add_argument("--county", help="scope to one county_fips")
    args = ap.parse_args()
    main(samples=args.samples, batch=args.batch, county=args.county)

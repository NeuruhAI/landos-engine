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
CANONICAL-APN equality (separators/case stripped — the statewide layers disagree with our
stored spelling on dashes/spaces); 0 hits = miss (stays NULL), >1 = ambiguous (skipped).

ENRICH-1 (2026-07-05) additions:
  * THE VINTAGE-CAPTURE GUARD (doctrine as code): a hit whose owner matches
    TREASURER|COUNTY OF|CITY OF|STATE OF|LAND BANK|FORFEIT is a VESTED/government roll
    entry, not the delinquent owner we want — it is NEVER written to owner_name. The row
    gets vintage_needed=true (mig 016) and the vested name + roll mailing stowed under
    raw_meta->'vintage' for the later paid vintage capture. Human/entity names write
    normally.
  * dirty-APN cleaning: 'Part of 12-410-004-00 (B)' queries the parent parcel (the
    county roll bills that APN); multi-APN strings query the FIRST parcel; 'TBD'
    placeholders are counted no_apn and skipped.
  * separator VARIANTS on miss (as-is, dashes<->spaces, stripped) — the query form that
    hit is stowed with the row's cache entry.
  * raw responses cached to ~/models/provenance/enrich_owner_cache/<fips>.json
    (re-inspectable, re-runs reproducible).
"""
import argparse
import json
import os
import re
import time
from datetime import date

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.providers.chain import ParcelProviderChain
from app.providers.config import (COLLIGO_ENDPOINTS, COUNTY_REST_ENDPOINTS,
                                  FETCHGIS_ENDPOINTS)
from app.score_absentee import classify_owner_type

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
PROVENANCE = "discovered_taxsale"
SPACING_S = 1.2

# ENRICH-1 doctrine pattern — the directive's six, PLUS government spellings the
# census fan-out OBSERVED on real county rolls (each line cites its evidence;
# the intent — never write a vested/government name to owner_name — is the
# directive's, the extra spellings are reported to the operator):
#   TREASURY     'MICHIGAN DEPARTMENT OF TREASURY' (Mecosta 3/3)
#   TREASRUER    'IONIA COUNTY TREASRUER' — the county's own misspelling (Ionia)
#   DEPT/DEPARTMENT OF NATURAL RESOURCE  MI DNR vested (Kalkaska 3/3, Charlevoix)
#   trailing ' COUNTY'   'GRATIOT COUNTY' (Gratiot sample)
#   bare 'STATE'         Lake County's shorthand for State of Michigan (3/3)
VINTAGE_RE = re.compile(
    r"TREASURER|TREASRUER|TREASURY|COUNTY OF|CITY OF|STATE OF|LAND BANK|FORFEIT"
    r"|DEP(ARTMEN)?T OF NATURAL RESOURCE|\bCOUNTY$|^STATE$", re.I)
CACHE_DIR = os.path.expanduser("~/models/provenance/enrich_owner_cache")


def canon_apn(s):
    """Canonical APN key — alphanumerics only, uppercase (separator/case-immune)."""
    return re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()


def clean_apn(raw):
    """Query-able parcel number from a stored parcel_apn that may be decorated.
    'Part of 12-410-004-00 (B)' -> '12-410-004-00' (the county roll bills the parent
    parcel; its owner-of-record IS the delinquent taxpayer). Two APNs in one string ->
    the FIRST. 'Parcel ID is TBD ...' -> None (no parcel-shaped token; never guessed)."""
    s = (raw or "").strip()
    if not s:
        return None
    dashed = [t for t in re.findall(r"[0-9A-Za-z]+(?:-[0-9A-Za-z]+)+", s)
              if sum(c.isdigit() for c in t) >= 6]
    if re.fullmatch(r"[0-9A-Za-z .\-]+", s) and any(c.isdigit() for c in s):
        # undecorated: either one APN verbatim (spaces may BE its separator style,
        # e.g. Mecosta '11 180 122 000') or several dash-form APNs side by side
        parts = s.split()
        if len(dashed) >= 2 and all("-" in p for p in parts):
            return dashed[0]
        return s
    if dashed:
        return dashed[0]
    return None


def apn_variants(apn):
    """Ordered query spellings: as-is first, then separator swaps, then stripped."""
    a = (apn or "").strip()
    if not a:
        return []
    out, seen = [], set()
    for v in (a, a.replace("-", " "), a.replace(" ", "-"),
              re.sub(r"[^0-9A-Za-z]", "", a)):
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out

COHORT = """
    select lead_id, county_fips, parcel_apn,
           raw_meta->>'sev' as ts_sev, raw_meta->>'property_type' as ts_prop
    from landos.owner_leads
    where tenant_id = %s and provenance = %s
      and owner_name is null
      and not vintage_needed
      and county_fips = any(%s)
    order by county_fips, parcel_apn
"""

# --coords-only: rows ALREADY owner-enriched but still coordless (the pre-centroid
# A2 harvest) — re-lookup for geometry only; owner fields are never touched.
COHORT_COORDS = """
    select lead_id, county_fips, parcel_apn,
           raw_meta->>'sev' as ts_sev, raw_meta->>'property_type' as ts_prop
    from landos.owner_leads
    where tenant_id = %s and provenance = %s
      and owner_name is not null and situs_lat is null
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
        situs_lat = coalesce(situs_lat, %(lat)s),
        situs_lng = coalesce(situs_lng, %(lng)s),
        updated_at = now()
    where lead_id = %(lead_id)s and owner_name is null
"""

WRITE_COORDS = """
    update landos.owner_leads
    set situs_lat = %(lat)s, situs_lng = %(lng)s, updated_at = now()
    where lead_id = %(lead_id)s and situs_lat is null
"""

# VINTAGE GUARD write: owner_name deliberately NOT touched. The vested name +
# roll mailing go under raw_meta->'vintage' (audit + input to the later paid
# capture); parcel facts the roll DOES hold honestly (coords/situs) still land.
WRITE_VINTAGE = """
    update landos.owner_leads
    set vintage_needed = true,
        raw_meta = coalesce(raw_meta, '{}'::jsonb) || jsonb_build_object('vintage', %(vintage)s),
        situs_address = coalesce(situs_address, %(situs)s),
        situs_lat = coalesce(situs_lat, %(lat)s),
        situs_lng = coalesce(situs_lng, %(lng)s),
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
    """CANONICAL-APN match only (separator/case-immune; still exact parcel identity).
    Returns (parcel|None, 'hit'|'miss'|'ambiguous')."""
    key = canon_apn(apn)
    exact = [r for r in results if canon_apn(r.get("parcel_apn")) == key]
    if not exact:
        return None, "miss"
    if len(exact) > 1:
        owners = {(r.get("owner_name") or "").strip() for r in exact}
        if len(owners) > 1:
            return None, "ambiguous"
    return exact[0], "hit"


def main(samples=None, batch=False, county=None, coords_only=False):
    # ENRICH-1: a county is workable through ANY verified $0 tier (ArcGIS
    # county_rest, FetchGIS PSsearch2, Colligo public link) — the chain picks
    # the right provider per county from PARCEL_PROVIDER_CHAIN.
    counties = sorted({f for reg in (COUNTY_REST_ENDPOINTS, FETCHGIS_ENDPOINTS,
                                     COLLIGO_ENDPOINTS)
                       for f, c in reg.items() if c.get("verified")})
    if county:
        counties = [f for f in counties if f == county]
    if not counties:
        raise SystemExit(f"no verified county_rest config for {county!r}")

    chain = ParcelProviderChain(TENANT)
    stats = {f: {"attempted": 0, "hit": 0, "miss": 0, "ambiguous": 0, "unavailable": 0,
                 "no_apn": 0, "vintage": 0,
                 "mail": 0, "situs": 0, "coords": 0, "entity": 0, "govt": 0} for f in counties}
    cache = {f: {} for f in counties}   # stored_apn -> raw evidence, flushed per county

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(COHORT_COORDS if coords_only else COHORT, (TENANT, PROVENANCE, counties))
        rows = cur.fetchall()
        per_county_seen = {f: 0 for f in counties}
        mode = f"samples={samples}" if samples else ("BATCH" if batch else "dry-run")
        if coords_only:
            mode += " coords-only"
        print(f"enrich_owner_chain [{mode}] — cohort rows selected: {len(rows)} across {counties}")

        for r in rows:
            f = r["county_fips"]
            if samples and per_county_seen[f] >= samples:
                continue
            per_county_seen[f] += 1
            st = stats[f]
            st["attempted"] += 1

            q_apn = clean_apn(r["parcel_apn"])
            if not q_apn:
                st["no_apn"] += 1
                cache[f][r["parcel_apn"] or ""] = {"verdict": "no_apn"}
                print(f"  NO-APN {f} {r['parcel_apn']!r} — nothing parcel-shaped to query")
                continue

            parcel, verdict, hit_form, results, unavailable = None, "miss", None, [], False
            tried = apn_variants(q_apn)
            for i, v in enumerate(tried):
                if i:
                    time.sleep(0.4)   # brief spacing between variant retries
                results = chain.lookup_by_apn(f, v)
                if not results and chain.miss_is_retryable():
                    unavailable = True
                    break
                parcel, verdict = _pick(results, v)
                if verdict != "miss":
                    hit_form = v
                    break
            if unavailable:
                st["unavailable"] += 1
                print(f"  UNAVAILABLE {f} {r['parcel_apn']}  trail={chain.trail_str()}")
                time.sleep(SPACING_S)
                continue
            st[verdict if verdict != "hit" else "hit"] += 1

            if verdict != "hit":
                cache[f][r["parcel_apn"]] = {"verdict": verdict, "tried": tried}
                print(f"  {verdict.upper()} {f} {r['parcel_apn']} ({len(results)} rows) trail={chain.trail_str()}")
                time.sleep(SPACING_S)
                continue

            owner = parcel.get("owner_name")
            otype = classify_owner_type(owner)
            cache[f][r["parcel_apn"]] = {
                "verdict": "hit", "queried": hit_form,
                "attributes": (parcel.get("raw_meta") or {}).get("county_rest_attributes")}

            # ---- THE VINTAGE-CAPTURE GUARD (ENRICH-1 doctrine as code) ----------
            # A vested/government roll name NEVER lands in owner_name. Flag the row
            # for paid vintage capture; stow the vested name + roll mailing for audit;
            # parcel facts the roll holds honestly (coords/situs) still land.
            if not coords_only and owner and VINTAGE_RE.search(owner.strip()):
                st["vintage"] += 1
                if parcel.get("situs_lat") is not None:
                    st["coords"] += 1
                vint = {
                    "vested_owner": owner,
                    "roll_mailing": {k: parcel.get(k) for k in
                                     ("owner_mailing_address", "owner_mailing_city",
                                      "owner_mailing_state", "owner_mailing_zip")
                                     if parcel.get(k)},
                    "source": parcel.get("provenance"),
                    "queried_apn": hit_form,
                    "captured_at": date.today().isoformat(),
                }
                if samples:
                    print(f"  [{f}] {r['parcel_apn']}  VINTAGE-FLAGGED "
                          f"(vested: {owner!r}) — owner_name NOT written")
                elif batch:
                    cur.execute(WRITE_VINTAGE, {
                        "vintage": Jsonb(vint),
                        "situs": parcel.get("situs_address"),
                        "lat": parcel.get("situs_lat"),
                        "lng": parcel.get("situs_lng"),
                        "lead_id": r["lead_id"],
                    })
                time.sleep(SPACING_S)
                continue
            if otype in ("entity", "govt"):
                st[otype] += 1
            if parcel.get("owner_mailing_address"):
                st["mail"] += 1
            if parcel.get("situs_address"):
                st["situs"] += 1
            if parcel.get("situs_lat") is not None:
                st["coords"] += 1

            if samples:
                print(f"  [{f}] {r['parcel_apn']}")
                print(f"      layer: owner={owner!r} [{otype}] mail={parcel.get('owner_mailing_address')!r}, "
                      f"{parcel.get('owner_mailing_city')!r} {parcel.get('owner_mailing_state')!r} "
                      f"{parcel.get('owner_mailing_zip')!r} situs={parcel.get('situs_address')!r} "
                      f"latlng=({parcel.get('situs_lat')},{parcel.get('situs_lng')}) "
                      f"| {_layer_evidence(parcel)}")
                print(f"      ours : sev={r['ts_sev']!r} prop={(r['ts_prop'] or '')[:70]!r}")
            elif batch and coords_only:
                if parcel.get("situs_lat") is not None:
                    cur.execute(WRITE_COORDS, {
                        "lat": parcel.get("situs_lat"),
                        "lng": parcel.get("situs_lng"),
                        "lead_id": r["lead_id"],
                    })
            elif batch:
                cur.execute(WRITE, {
                    "owner_name": owner,
                    "owner_type": otype,
                    "mail_street": parcel.get("owner_mailing_address"),
                    "mail_city": parcel.get("owner_mailing_city"),
                    "mail_state": parcel.get("owner_mailing_state"),
                    "mail_zip": parcel.get("owner_mailing_zip"),
                    "situs": parcel.get("situs_address"),
                    "lat": parcel.get("situs_lat"),
                    "lng": parcel.get("situs_lng"),
                    "lead_id": r["lead_id"],
                })
            time.sleep(SPACING_S)

        if batch:
            conn.commit()
        else:
            conn.rollback()

    # raw-response cache: best-effort, merge-on-write (re-runs accumulate), never fatal
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        for f, entries in cache.items():
            if not entries:
                continue
            path = os.path.join(CACHE_DIR, f"{f}.json")
            merged = {}
            if os.path.exists(path):
                with open(path) as fh:
                    merged = (json.load(fh) or {}).get("entries", {})
            merged.update(entries)
            with open(path, "w") as fh:
                json.dump({"county_fips": f, "updated": date.today().isoformat(),
                           "entries": merged}, fh)
    except (OSError, ValueError) as e:
        print(f"  cache write skipped (never fatal): {e}")

    print("\n=== enrich_owner_chain summary (found / government-flagged / miss) ===")
    for f in counties:
        s = stats[f]
        if not s["attempted"]:
            continue
        print(f"  {f}: attempted={s['attempted']} found={s['hit'] - s['vintage']} "
              f"government_flagged={s['vintage']} miss={s['miss']} "
              f"ambiguous={s['ambiguous']} unavailable={s['unavailable']} no_apn={s['no_apn']} | "
              f"mail={s['mail']} situs={s['situs']} coords={s['coords']} "
              f"entity={s['entity']} govt_named={s['govt']}")
    if not batch:
        print("dry-run/samples — NO writes. Re-run with --batch to write.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="A2 — cohort owner enrichment via ParcelProviderChain")
    ap.add_argument("--samples", type=int, help="verify N per county (read-only, evidence columns)")
    ap.add_argument("--batch", action="store_true", help="write owner fields to landos.owner_leads")
    ap.add_argument("--county", help="scope to one county_fips")
    ap.add_argument("--coords-only", action="store_true", dest="coords_only",
                    help="backfill situs_lat/lng for already-enriched coordless rows (owner fields untouched)")
    args = ap.parse_args()
    main(samples=args.samples, batch=args.batch, county=args.county, coords_only=args.coords_only)

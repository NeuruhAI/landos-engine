"""
mint_taxsale_leads.py — CC2 step 4: the NO-SPEND MINTER (AXON Emergence Loop).

    python -m scripts.mint_taxsale_leads [--dry-run]

Mints county tax-sale parcels (loaded by scripts/load_signals.py / load_taxsale_county.py) into
landos.owner_leads straight from the staged county data already in
discovery_signals.raw_payload, then matches each tax-sale signal to its new lead so the
emergence scorer can light it.

$0. Pure DB read -> compute -> DB write. NO network, NO API, NO ParcelProviderChain.

These are NEW inventory (certificate-sale parcels NOT in the curated 136). DISCOVERED, not
underwritten: the county layer gives no situs street / lat-lng / mailing, so those land as
honest NULL (never fabricated). final_lead_score stays NULL (no composite run); emerging_score
is the ONLY score they get, written next by score_emergence.py.

Idempotent: ON CONFLICT (tenant_id, county_fips, parcel_apn) DO NOTHING — re-run mints 0 dups;
only signals still status='new' are (re)matched, so re-run re-matches 0. The whole batch is one
DELETE (provenance='discovered_taxsale', source='county_taxsale_<county_fips>') if it must be removed.

🔴 PROMOTE GATE (mig 036 / tax_sale_promote_contract): the mint fires ONLY for signals a HUMAN
has promoted (promoted=true). tax-sale.info's ToU forbids AUTOMATED aggregation for outreach +
contacting former owners — the ToU line runs through a human keystroke, so an un-promoted
taxsale signal NEVER mints and NEVER enters any queue. Gate-in-DATA (the WHERE below), not app
logic that a caller could skip. The freshness gate stays as defense-in-depth on top.

🔴 DILIGENCE PREDICATE (G1 / mig 039): the full mint predicate is fresh + promoted +
needs_diligence=false + diligence_flag is null. A flagged record NEVER mints. Gate-in-DATA in
the WHERE below, re-checked row-by-row in Python (app/taxsale_predicate.py) as defense-in-depth.
The dry-run reconciles EVERY taxsale signal against the predicate — county-by-county eligible +
excluded-by-reason counts with a books-balance invariant and a canonical receipt hash.
"""
import argparse
from datetime import date

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.score_absentee import classify_owner_type   # the EXISTING entity/individual classifier
from app.taxsale_predicate import (                   # G1: the complete mint predicate, pure
    evaluate_mint_predicate, reconcile, render_reconciliation)

APG_TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"   # St. Joseph 18141 is APG territory
SOURCE_PREFIX = "county_taxsale_"   # per-row source = prefix + the signal's county_fips
PROVENANCE = "discovered_taxsale"

SIGNALS = """
    select signal_id, parcel_apn, owner_name, confidence, county_fips,
           raw_payload, status, matched_lead_id, signal_date, promoted,
           needs_diligence, diligence_flag
    from landos.discovery_signals
    where source_type = 'taxsale' and signal_class = 'temporal'
      and promoted = true                       -- 🔴 THE GATE (mig 036): human-promoted only
      and needs_diligence = false               -- 🔴 G1 (mig 039): diligence review complete
      and diligence_flag is null                -- 🔴 G1 (mig 039): flagged records NEVER mint
      and (%(only_fips)s::text[] is null or county_fips = any(%(only_fips)s))
    order by parcel_apn
"""

# The dry-run reconciler's population: EVERY taxsale temporal signal in scope, ungated —
# so the excluded (flagged / stale / needs_diligence / not_promoted) are counted per county,
# never silently invisible. Read-only; the classification happens in app/taxsale_predicate.
RECON_SIGNALS = """
    select signal_id, parcel_apn, owner_name, county_fips, source_ref,
           raw_payload, signal_date, promoted, needs_diligence, diligence_flag
    from landos.discovery_signals
    where source_type = 'taxsale' and signal_class = 'temporal'
      and (%(only_fips)s::text[] is null or county_fips = any(%(only_fips)s))
    order by parcel_apn
"""

# Honest NULLs in the VALUES: situs_address / situs_lat / situs_lng / owner_mailing_address /
# absentee_flag / absentee_score / final_lead_score / emerging_score. discovery_seed=false (real
# parcels). emerging_score is written by the scorer, not the minter.
MINT = """
    insert into landos.owner_leads
        (tenant_id, county_fips, parcel_apn, owner_name, land_use_class, acreage,
         owner_type, situs_address, situs_lat, situs_lng, owner_mailing_address,
         absentee_flag, absentee_score, final_lead_score, emerging_score,
         discovery_seed, source, provenance, confidence_score, raw_meta)
    values (%(tenant_id)s, %(county_fips)s, %(parcel_apn)s, %(owner_name)s,
            %(land_use_class)s, %(acreage)s, %(owner_type)s,
            null, null, null, null,
            null, null, null, null,
            false, %(source)s, %(provenance)s, %(confidence_score)s, %(raw_meta)s)
    on conflict (tenant_id, county_fips, parcel_apn) do nothing
    returning lead_id
"""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main(dry_run=False, only_fips=None):
    """only_fips: optional list of county_fips to scope the mint (HUNT-V2 jobs mint
    per-territory). Returns a stats dict either way — the CLI ignores it; the hunt
    worker/mint consume it. CLI behavior with no args is byte-identical."""
    minted = skipped_conflict = signals_matched = rejected_stale = 0
    rejected_by_reason = {}
    today = date.today()
    preview = []
    per_county = {}
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(SIGNALS, {"only_fips": list(only_fips) if only_fips else None})
        signals = cur.fetchall()
        for s in signals:
            # G1 PREDICATE (defense-in-depth): the SQL already gates promoted / needs_diligence /
            # diligence_flag; re-checking the FULL predicate here (incl. freshness) means a
            # consumed, flagged, or un-reviewed parcel NEVER becomes a candidate even if the
            # fetch path changes. Reject loudly, whatever the source/county.
            ok, code, why = evaluate_mint_predicate(s, today)
            if not ok:
                rejected_stale += 1  # back-compat total (hunt_worker consumes this key)
                rejected_by_reason[code] = rejected_by_reason.get(code, 0) + 1
                print(f"  REJECT {s['parcel_apn']!r}: [{code}] {why}")
                continue
            rp = s["raw_payload"] or {}
            owner_name = s["owner_name"]
            row = {
                "tenant_id": APG_TENANT,
                "county_fips": s["county_fips"],
                "parcel_apn": s["parcel_apn"],
                "owner_name": owner_name,
                "land_use_class": (rp.get("raw") or {}).get("PROPTYPE") or rp.get("prop_type"),
                "acreage": _num(rp.get("acreage")),
                "owner_type": classify_owner_type(owner_name),
                "source": SOURCE_PREFIX + s["county_fips"],
                "provenance": PROVENANCE,
                "confidence_score": s["confidence"],
                "raw_meta": Jsonb(rp),
            }
            if dry_run:
                preview.append((s["county_fips"], row["parcel_apn"], owner_name,
                                row["owner_type"], row["land_use_class"], row["acreage"]))
                continue
            cur.execute(MINT, row)
            got = cur.fetchone()
            pc = per_county.setdefault(s["county_fips"],
                                       {"minted": 0, "already_present": 0})
            if got:
                lead_id = got["lead_id"]
                minted += 1
                pc["minted"] += 1
            else:
                # parcel already an owner_lead — find it so the signal still matches (idempotent)
                cur.execute("select lead_id from landos.owner_leads "
                            "where tenant_id=%s and county_fips=%s and parcel_apn=%s",
                            (APG_TENANT, s["county_fips"], s["parcel_apn"]))
                lead_id = cur.fetchone()["lead_id"]
                skipped_conflict += 1
                pc["already_present"] += 1
            # match the SOURCE signal to its lead — only if not already matched (re-run matches 0)
            if s["status"] != "matched" or s["matched_lead_id"] is None:
                cur.execute("update landos.discovery_signals "
                            "set matched_lead_id=%s, status='matched' where signal_id=%s",
                            (lead_id, s["signal_id"]))
                signals_matched += cur.rowcount

        if dry_run:
            # honest would_mint: probe existing (tenant, fips, apn) so a county
            # that already minted reports already_present, never a phantom mint
            for fips, apn, own, ot, luc, ac in preview:
                cur.execute("select 1 from landos.owner_leads "
                            "where tenant_id=%s and county_fips=%s and parcel_apn=%s",
                            (APG_TENANT, fips, apn))
                pc = per_county.setdefault(fips, {"would_mint": 0, "already_present": 0})
                pc["already_present" if cur.fetchone() else "would_mint"] += 1
            # G1 RECONCILIATION: classify EVERY taxsale signal in scope (ungated fetch) so the
            # excluded — flagged / stale / needs_diligence / not_promoted — are counted county
            # by county, and the books provably balance. Read-only; deterministic (injected
            # clock, canonical ordering, receipt hash byte-stable across runs on unchanged data).
            cur.execute(RECON_SIGNALS, {"only_fips": list(only_fips) if only_fips else None})
            recon = reconcile(cur.fetchall(), today)
            conn.rollback()
            would = sum(c.get("would_mint", 0) for c in per_county.values())
            present = sum(c.get("already_present", 0) for c in per_county.values())
            print(f"\n=== MINT DRY-RUN — {would} would mint, {present} already present, "
                  f"{rejected_stale} rejected (gate), NO writes ===")
            for fips, apn, own, ot, luc, ac in preview[:10]:
                print(f"  [{fips}] {apn}  {own!r}  [{ot}]  {luc}  {ac} ac")
            if len(preview) > 10:
                print(f"  … +{len(preview) - 10} more")
            print()
            print(render_reconciliation(recon))
            print("\nHARD STOP — dry-run only. Re-run without --dry-run to mint.")
            return {"dry_run": True, "would_mint": would, "already_present": present,
                    "rejected_stale": rejected_stale, "rejected_by_reason": rejected_by_reason,
                    "per_county": per_county, "reconciliation": recon,
                    "preview": [{"county_fips": p[0], "parcel_apn": p[1], "owner_name": p[2],
                                 "owner_type": p[3], "land_use_class": p[4], "acreage": p[5]}
                                for p in preview]}
        conn.commit()

    print(f"\nmint summary: minted={minted} skipped_conflict={skipped_conflict} "
          f"signals_matched={signals_matched} rejected_stale={rejected_stale} "
          f"(tenant=APG, source={SOURCE_PREFIX!r}+county_fips, provenance={PROVENANCE!r}; G1 predicate: only "
          f"fresh + promoted + diligence-clear mint; situs / lat-lng / mailing / absentee / final_lead_score = honest NULL).")
    print("Next: `python -m scripts.score_emergence` then `python -m scripts.sync_to_crm`.")
    return {"dry_run": False, "minted": minted, "skipped_conflict": skipped_conflict,
            "signals_matched": signals_matched, "rejected_stale": rejected_stale,
            "rejected_by_reason": rejected_by_reason, "per_county": per_county}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="CC2 step 4 — no-spend minter (taxsale -> owner_leads)")
    ap.add_argument("--dry-run", action="store_true", help="count + preview; write nothing")
    main(dry_run=ap.parse_args().dry_run)

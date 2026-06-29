"""
axon_cycle.py — the full AXON discovery cycle in ONE process (additive orchestrator).

    python -m scripts.axon_cycle [--dry-run]

Runs the whole chain end-to-end in a single Python process so the (expensive) imports are paid
ONCE — the host this runs on can take minutes to import psycopg under memory pressure, so 9
separate `python -m` invocations would be 9x that cost. daily_run.sh calls THIS.

Order (critical steps abort the run on failure = fail-loud; enrichment steps are best-effort and
degrade to honest NULL so a transient ArcGIS/Nominatim outage never blocks minting/scoring/sync):

  CRITICAL  load_signals            stjoseph + estate (shipped)
  CRITICAL  load_taxsale_county     delaware + vanderburgh + future (additive)
  CRITICAL  mint_taxsale_inmarket   taxsale signals -> owner_leads, ALL counties (allowlist gutted
                                     2026-06-29; national expansion — delegates to mint_taxsale_leads)
  CRITICAL  score_emergence         emerging_score (shipped, moat IP)
  ENRICH    geocode_taxsale_multi   per-layer parcel centroids (additive; supersedes the
                                     shipped single-layer geocode, which crashes on >1 county)
  ENRICH    backfill_discovered_situs  situs_address from staged raw_meta (additive, $0)
  ENRICH    reverse_geocode_taxsale    Nominatim situs for any coord'd lead still missing one (shipped)
  ENRICH    enrich_owner_mailing       owner mailing from county parcel layer (shipped)
  CRITICAL  sync_to_crm             project landos -> public.acquisition_leads (shipped, SOLE writer)

Idempotent throughout (every step re-runs to 0 new). NB: it does NOT call the shipped
scripts/geocode_taxsale_leads.py (single-layer; left untouched per additive-only).
"""
import argparse
import sys
import time
import traceback

from scripts import load_signals, mint_taxsale_inmarket, score_emergence
from scripts import reverse_geocode_taxsale, enrich_owner_mailing, sync_to_crm
from scripts import load_taxsale_county, geocode_taxsale_multi, backfill_discovered_situs


def _run(label, fn, critical, dry_run, results):
    print(f"\n{'='*72}\n>>> {label}  ({'CRITICAL' if critical else 'enrich'})\n{'='*72}", flush=True)
    t = time.time()
    try:
        fn()
        results.append((label, "OK", round(time.time() - t, 1), critical))
        print(f"<<< {label} OK ({time.time()-t:.1f}s)", flush=True)
        return True
    except BaseException as e:                       # SystemExit (drop-not-fight) included
        results.append((label, f"FAIL: {type(e).__name__}: {e}", round(time.time() - t, 1), critical))
        print(f"<<< {label} FAILED: {type(e).__name__}: {e}", flush=True)
        traceback.print_exc()
        return False


def main(dry_run=False):
    t0 = time.time()
    results = []

    # ---- CRITICAL ingest/score chain — abort on first failure (don't push partial state) ----
    critical_chain = [
        ("load_signals",        lambda: load_signals.main(list(load_signals.SOURCES), dry_run)),
        ("load_taxsale_county", lambda: load_taxsale_county.main(dry_run=dry_run)),
        ("mint_taxsale_inmarket", lambda: mint_taxsale_inmarket.main(dry_run=dry_run)),
        ("score_emergence",     lambda: score_emergence.main(dry_run=dry_run)),
    ]
    for label, fn in critical_chain:
        if not _run(label, fn, True, dry_run, results):
            _summary(results, t0)
            print("\nABORT: critical ingest/score step failed — not running enrichment or sync.")
            sys.exit(1)

    # ---- ENRICH — best-effort; honest NULL on failure, never blocks the sync ----
    for label, fn in [
        ("geocode_taxsale_multi",    lambda: geocode_taxsale_multi.main(dry_run=dry_run)),
        ("backfill_discovered_situs",lambda: backfill_discovered_situs.main(dry_run=dry_run)),
        ("reverse_geocode_taxsale",  lambda: reverse_geocode_taxsale.main(dry_run=dry_run)),
        ("enrich_owner_mailing",     lambda: enrich_owner_mailing.main(dry_run=dry_run)),
    ]:
        _run(label, fn, False, dry_run, results)

    # ---- CRITICAL projection (sole writer of public.acquisition_leads) ----
    sync_ok = _run("sync_to_crm", lambda: sync_to_crm.main(dry_run=dry_run), True, dry_run, results)

    _summary(results, t0)
    if not sync_ok:
        sys.exit(1)


def _summary(results, t0):
    print(f"\n{'#'*72}\n# AXON CYCLE SUMMARY ({time.time()-t0:.0f}s total)\n{'#'*72}")
    for label, status, secs, critical in results:
        tag = "C" if critical else " "
        print(f"  [{tag}] {label:<26} {secs:>6.1f}s  {status}")
    crit_fail = [r for r in results if r[1].startswith('FAIL') and r[3]]
    enr_fail = [r for r in results if r[1].startswith('FAIL') and not r[3]]
    print(f"\n  critical_failures={len(crit_fail)} enrichment_failures={len(enr_fail)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AXON full discovery cycle (one process, additive)")
    ap.add_argument("--dry-run", action="store_true", help="run every step in dry-run; write nothing")
    main(dry_run=ap.parse_args().dry_run)

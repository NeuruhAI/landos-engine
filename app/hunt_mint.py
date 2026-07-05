"""
hunt_mint.py — HUNT-V2 §4: THE OPERATOR'S TRIGGER. The only path from a hunt
job to landos.owner_leads.

    python -m app.hunt_mint <job_id> [--yes-i-typed-go GO]

Autonomy doctrine holds absolutely: the system proposed (worker parked the job
at awaiting_operator_mint with the dry-run table); THIS command is the human
firing it. It prints the dry-run table fresh (never trusts a stale count),
requires the operator to TYPE GO (exactly), then:

    mint (scoped to the job's covered counties, mint_taxsale_leads)
    -> geocode attempt (county adapter if known: MI registry counties via
       scripts.geocode_taxsale_mi, Williamson TX via its dedicated script;
       everything else stays honest-NULL with a note)
    -> score_emergence (the REAL run — signals are now matched, leads emerge)
    -> sync_to_crm (THE single writer of public.acquisition_leads)
    -> status 'synced'

--yes-i-typed-go GO exists for the operator's own scripting (e.g. a runbook
line); it still demands the literal token GO. Anything else aborts unchanged.
"""
import argparse
import sys

import psycopg
from psycopg.rows import dict_row

from scripts import mint_taxsale_leads, score_emergence, sync_to_crm

from .config import DATABASE_URL
from . import hunt_core

# county_fips -> geocode adapter. DATA: add a county here when its adapter exists.
GEOCODE_ADAPTERS = {
    "48491": "scripts.geocode_taxsale_williamson",
}
MI_REGISTRY_GEOCODER = "scripts.geocode_taxsale_mi"   # registry-driven (mi_parcel_endpoints.json)


def print_dry_run_table(stats):
    print("\n=== DRY-RUN MINT TABLE (fresh — computed now, not the stored counts) ===")
    print(f"  would_mint={stats['would_mint']}  already_present={stats['already_present']}  "
          f"rejected_stale={stats['rejected_stale']}")
    for fips, c in sorted(stats["per_county"].items()):
        print(f"    [{fips}] would_mint={c.get('would_mint', 0)} "
              f"already_present={c.get('already_present', 0)}")
    for p in stats["preview"][:15]:
        print(f"    {p['county_fips']} {p['parcel_apn']}  {p['owner_name']!r} [{p['owner_type']}]")
    if len(stats["preview"]) > 15:
        print(f"    … +{len(stats['preview']) - 15} more")


def geocode_attempt(fips_list):
    """Best-effort county-adapter geocode for freshly minted rows. Honest about
    coverage: counties with no adapter are reported and stay NULL."""
    notes = {}
    mi = [f for f in fips_list if f.startswith("26")]
    if mi:
        try:
            from scripts import geocode_taxsale_mi
            geocode_taxsale_mi.main(dry_run=False, only=set(mi))
            notes["mi"] = f"registry geocoder ran for {sorted(mi)}"
        except Exception as e:  # noqa: BLE001 — geocode is best-effort, never blocks the mint
            notes["mi"] = f"registry geocoder failed (rows stay NULL): {e}"
    for f in fips_list:
        if f in GEOCODE_ADAPTERS and not f.startswith("26"):
            notes[f] = f"adapter available: python -m {GEOCODE_ADAPTERS[f]} (county-specific; run per its runbook)"
        elif not f.startswith("26"):
            notes[f] = "no geocode adapter — coords stay honest-NULL"
    return notes


def main(job_id, typed=None):
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        job = hunt_core.get_job(cur, job_id)
        if job is None:
            raise SystemExit(f"no hunt job {job_id}")
        if job["status"] != "awaiting_operator_mint":
            raise SystemExit(f"job {job_id} is {job['status']!r} — only "
                             f"'awaiting_operator_mint' jobs can be fired")

        census = (job["counts"] or {}).get("census") or {}
        fips_list = [f for f, c in census.items() if c.get("covered")]
        if not fips_list:
            raise SystemExit("job has no covered counties (should be 'uncovered') — refusing")

        # fresh dry-run table — the operator fires on CURRENT truth
        stats = mint_taxsale_leads.main(dry_run=True, only_fips=fips_list)
        print_dry_run_table(stats)

        answer = typed if typed is not None else input(
            f"\nType GO to mint {stats['would_mint']} lead(s) into landos.owner_leads "
            f"(anything else aborts): ").strip()
        if answer != "GO":
            print("ABORTED — nothing written (the typed token was not GO).")
            return 1

        counts = dict(job["counts"] or {})
        hunt_core.update_job(cur, job_id, status="minting")
        conn.commit()

        mint_stats = mint_taxsale_leads.main(dry_run=False, only_fips=fips_list)
        counts["minted"] = {k: mint_stats[k] for k in
                            ("minted", "skipped_conflict", "signals_matched",
                             "rejected_stale", "per_county")}
        hunt_core.append_receipt(job, "mint", counts["minted"])

        geo_notes = geocode_attempt(fips_list)
        counts["geocode"] = geo_notes
        hunt_core.append_receipt(job, "geocode", geo_notes)

        score_emergence.main(dry_run=False)          # the REAL emergence run
        hunt_core.append_receipt(job, "score_emergence", {"ran": True})

        hunt_core.update_job(cur, job_id, counts=counts, receipts=job["receipts"])
        conn.commit()

    sync_to_crm.main()                               # THE single writer projects
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        job = hunt_core.get_job(cur, job_id)
        hunt_core.append_receipt(job, "sync", {"single_writer": "scripts.sync_to_crm", "ran": True})
        hunt_core.update_job(cur, job_id, status="synced", receipts=job["receipts"])
        conn.commit()
    print(f"\nhunt job {job_id}: SYNCED. minted={counts['minted']['minted']} "
          f"signals_matched={counts['minted']['signals_matched']}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HUNT-V2 operator fire (typed GO)")
    ap.add_argument("job_id")
    ap.add_argument("--yes-i-typed-go", dest="typed", default=None,
                    help="non-interactive runbooks pass the literal token GO")
    a = ap.parse_args()
    sys.exit(main(a.job_id, typed=a.typed))

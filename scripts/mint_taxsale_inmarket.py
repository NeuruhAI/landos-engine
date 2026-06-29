"""
mint_taxsale_inmarket.py — NATIONAL tax-sale minter (the in-market allowlist was GUTTED 2026-06-29).

    python -m scripts.mint_taxsale_inmarket [--dry-run]

History: this file briefly carried a county allowlist ({18141, 26021}) built off a stale
"out-of-market = do not mint" directive. That was REVERSED — national expansion is the thesis:
source the best emerging opportunities ANYWHERE in the US. The allowlist is removed entirely (not
widened — a widened list just breaks again on county #5), so this mints EVERY discovered tax-sale
signal in landos.discovery_signals, every county present and future.

It delegates to the shipped, county-agnostic scripts/mint_taxsale_leads.py — there is no longer
any county logic here to drift. The filename is retained ON PURPOSE so scripts/axon_cycle.py and
daily_run.sh keep importing the same symbol; only the behavior (national) changed.

Idempotent (ON CONFLICT DO NOTHING; signals re-match 0). New leads land discovered-tier; the CRM
spine forces them to the candidate stage on projection (you cannot mint straight into pipeline).
"""
import argparse

from scripts.mint_taxsale_leads import main   # national minter (no county filter) — re-exported


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="national tax-sale minter (delegates to mint_taxsale_leads; allowlist gutted)")
    ap.add_argument("--dry-run", action="store_true", help="count + preview; write nothing")
    main(dry_run=ap.parse_args().dry_run)

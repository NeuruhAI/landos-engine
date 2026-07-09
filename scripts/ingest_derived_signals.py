"""
ingest_derived_signals.py — SIGNALS-ONLY runner for DERIVED DiscoveryAdapters.

    python -m scripts.ingest_derived_signals --adapter estate_sale_ext [--dry-run]
    python -m scripts.ingest_derived_signals --adapter heirs_of_tangled_title [--dry-run]

WHY A SEPARATE RUNNER (not scripts/ingest_discovery.py, the minter):
  ingest_discovery.py is the probate -> owner_leads MINTER. Its per-signal loop runs
  the cost-gradient ParcelProviderChain (local_cache -> county_rest -> reportall ->
  regrid; the last two are PAID) and stamps probate provenance (deceased / heirs /
  mycase) onto every matched lead. DERIVED adapters (access_mode='derived') are
  spine -> spine OVERLAYS: they read rows already on the spine, classify them, and
  upsert graded discovery_signals back. They must NEVER touch a paid tier or wear
  probate stamps. This runner keeps that path single-purpose: it imports NO provider
  chain, so a paid tier is physically unreachable from here.

WRITE SHAPE: adapter.fetch() -> DiscoveryAdapter.upsert_signals (shared, idempotent
  ON CONFLICT (source_type, source_ref) DO UPDATE). Every row is INSERT-new within
  the adapter's OWN source_type namespace; it never mutates the base rows it derives
  from (estate_sale / taxsale). No owner_leads, no contact_relations, no public.* write.

TENANT DIVERGENCE (intentional, per canon): the base pool it reads is tenant-agnostic
  -- estate_sale/taxsale rows carry tenant_id NULL, and the acquisition_leads owner
  projection is tenant-scoped. The overlay rows this runner writes carry
  tenant_id = TENANT (upsert_signals stamps self.tenant_id). Base NULL, overlays TENANT
  -- by design; tenantization is explicit at the overlay layer.

FIREWALL: reads public.acquisition_leads (owner projection, SELECT only) +
  landos.discovery_signals (estate_sale text, SELECT only); writes landos.discovery_signals
  only. Zero public.* writes, zero owner_leads writes.
"""
import argparse
from collections import Counter

import psycopg

from app.config import DATABASE_URL
from app.discovery.estate_sale_ext import EstateSaleExtAdapter, load_estate_sale_records
from app.discovery.tangled_title import TangledTitleAdapter, load_owner_records

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

# adapter key -> (AdapterClass, records constructor-kwarg, spine loader)
# The loader reads the base pool with a DEFAULT (tuple) cursor -- both loaders index
# positionally (row[0] / dict(zip(cols,row))), so this runner must NOT use dict_row.
DERIVED = {
    "estate_sale_ext":        (EstateSaleExtAdapter, "records",       load_estate_sale_records),
    "heirs_of_tangled_title": (TangledTitleAdapter,  "owner_records", load_owner_records),
}


def _skip_report(adapter):
    """Honest per-adapter skip accounting (attributes are adapter-specific)."""
    skips = {}
    if hasattr(adapter, "non_matches"):
        skips["non_match_no_motivation"] = adapter.non_matches
    if hasattr(adapter, "skipped_no_ref"):
        skips["skipped_no_stable_ref"] = adapter.skipped_no_ref
    return skips


def main() -> None:
    ap = argparse.ArgumentParser(description="signals-only runner for derived overlay adapters")
    ap.add_argument("--adapter", required=True, choices=sorted(DERIVED))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    Cls, kwarg, loader = DERIVED[args.adapter]

    # DEFAULT (tuple) cursor on purpose -- the load_*_records read positionally.
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        records = loader(cur, TENANT)
        adapter = Cls(TENANT, **{kwarg: records})
        signals = adapter.fetch()

        # --- expected-new vs collision, computed BEFORE any write (read-only) ------
        refs = [s["source_ref"] for s in signals]
        ref_counts = Counter(refs)
        intra_batch_dups = sum(c - 1 for c in ref_counts.values() if c > 1)
        distinct = list(ref_counts)

        existing = set()
        if distinct:
            cur.execute(
                "select source_ref from landos.discovery_signals "
                "where source_type = %s and source_ref = any(%s)",
                (adapter.source_type, distinct),
            )
            existing = {row[0] for row in cur.fetchall()}
        new_refs = [r for r in distinct if r not in existing]
        collisions = [r for r in distinct if r in existing]

        mode = "DRY-RUN" if args.dry_run else "APPLY"
        print(f"\n=== DERIVED SIGNALS {mode} — {adapter.source_type} "
              f"(county={adapter.county_fips} access={adapter.access_mode}) ===")
        print(f"  base_records_loaded  = {len(records)}")
        print(f"  signals_emitted      = {len(signals)}")
        print(f"  intra_batch_dups     = {intra_batch_dups} (collapse to 1 row each via ON CONFLICT)")
        print(f"  distinct_rows        = {len(distinct)}")
        print(f"  expected_new         = {len(new_refs)}")
        print(f"  collisions_update    = {len(collisions)} (update-in-place, no dup)")
        for k, v in _skip_report(adapter).items():
            print(f"  skip[{k}] = {v}")

        if args.dry_run:
            conn.rollback()
            print("  NO writes (dry-run rolled back).")
        else:
            n = adapter.upsert_signals(cur, signals)
            conn.commit()
            print(f"  upserted = {n}  (inserted {len(new_refs)} + updated {len(collisions)})")


if __name__ == "__main__":
    main()

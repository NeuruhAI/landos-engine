"""
load_taxsale_county.py — load ADDITIONAL county tax-sale staged files (AXON cycle, additive).

    python -m scripts.load_taxsale_county [--dry-run]

The shipped scripts/load_signals.py only reads the two hardcoded St. Joseph / estate files. As
CC3 stages more counties (Delaware 18035, Vanderburgh 18163, future TaxSaleResources drops),
their `*_taxsale_signals.jsonl` files need loading too. This globs the staged tax_sale dir and
loads every county EXCEPT the one load_signals already owns (stjoseph), reusing load_signals'
own map_row()/INSERT so the mapping never diverges.

Pure: reads local JSONL + writes landos.discovery_signals only. NO network, NO spend.
Idempotent on (source_type, source_ref): re-running inserts 0 dups and never clobbers a row
that has since been resolved.
"""
import argparse
import glob
import json
import os

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from scripts.load_signals import map_row, INSERT, INGEST_ROOT

STAGED_DIR = os.path.join(INGEST_ROOT, "tax_sale", "staged")
# files load_signals.py already owns — skip so we don't double-handle (ON CONFLICT makes it
# harmless anyway, but explicit is clearer).
OWNED_BY_LOAD_SIGNALS = {"stjoseph_taxsale_signals.jsonl"}


def discover_files():
    return sorted(p for p in glob.glob(os.path.join(STAGED_DIR, "*_taxsale_signals.jsonl"))
                  if os.path.basename(p) not in OWNED_BY_LOAD_SIGNALS)


def load_file(cur, path, dry_run):
    read = inserted = skipped = 0
    with open(path) as fh:                                   # READ-ONLY on CC3's tree
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            row = map_row(rec)
            assert row["signal_class"] == "temporal", f"non-temporal {rec.get('signal_id')!r}"
            read += 1
            if dry_run:
                cur.execute("select 1 from landos.discovery_signals where source_type=%s and source_ref=%s",
                            (row["source_type"], row["source_ref"]))
                if cur.fetchone():
                    skipped += 1
                else:
                    inserted += 1
                continue
            cur.execute(INSERT, row)
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1
    return read, inserted, skipped


def main(dry_run=False):
    files = discover_files()
    tot_read = tot_ins = tot_skip = 0
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        for path in files:
            r, i, s = load_file(cur, path, dry_run)
            verb = "would_insert" if dry_run else "inserted"
            print(f"  [{os.path.basename(path)}] read={r} {verb}={i} skipped_dup={s}")
            tot_read += r; tot_ins += i; tot_skip += s
        if dry_run:
            conn.rollback()
            print(f"\n=== LOAD-COUNTY DRY-RUN — files={len(files)} read={tot_read} "
                  f"would_insert={tot_ins} skipped_dup={tot_skip}. NO writes. ===")
            return
        conn.commit()
    print(f"\nload-county summary: files={len(files)} read={tot_read} inserted={tot_ins} "
          f"skipped_dup={tot_skip} (signal_class=temporal, status=new). Next: mint_taxsale_leads")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="load additional county taxsale staged files (additive)")
    ap.add_argument("--dry-run", action="store_true", help="count + preview; write nothing")
    main(dry_run=ap.parse_args().dry_run)

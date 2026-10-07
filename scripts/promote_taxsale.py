"""
promote_taxsale.py — THE HUMAN KEYSTROKE (tax_sale_promote_contract §3).

A tax-sale signal mints ONLY after a person promotes it (mig 036). This is the sanctioned
engine-side promote surface: a reviewed diligence worklist of specific parcels a human chose
to work. It sets promoted=true / promoted_by / promoted_at for NAMED rows only.

    python -m scripts.promote_taxsale --by <user_uuid> --ref <source_ref> [--ref ...]   # dry-run
    python -m scripts.promote_taxsale --by <user_uuid> --csv diligence_sheet.csv --apply

INVARIANT (contract §3): "Nothing else may set promoted=true. No cron, no adapter, no bulk
update." So this tool REFUSES to run without explicit --ref/--csv targets — there is no
promote-all. Only taxsale rows are eligible; a ref that isn't a needs-diligence taxsale signal
is reported and skipped, never guessed.

G1 (mig 040): the promote keystroke IS the completed diligence review — it sets
needs_diligence=false in the same statement. A FLAGGED row (diligence_flag set) cannot be
promoted: the WHERE refuses it and it reports as not-eligible. Flagging (--flag, the safe
direction: can only prevent a mint) and clearing a flag (--clear-flag) are ALSO human
keystrokes here; clearing a flag re-opens diligence (needs_diligence=true, fail-closed) —
it never promotes.
"""
import argparse
import csv
import sys

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

PROMOTE = """
    update landos.discovery_signals
       set promoted = true, promoted_by = %(by)s, promoted_at = now(),
           needs_diligence = false
     where source_type = 'taxsale' and source_ref = %(ref)s and promoted = false
       and diligence_flag is null
     returning signal_id, source_ref, county_fips, parcel_apn
"""

FLAG = """
    update landos.discovery_signals
       set diligence_flag = %(reason)s, flagged_by = %(by)s, flagged_at = now()
     where source_type = 'taxsale' and source_ref = %(ref)s and diligence_flag is null
     returning signal_id, source_ref, county_fips, parcel_apn
"""

CLEAR_FLAG = """
    update landos.discovery_signals
       set diligence_flag = null, flagged_by = null, flagged_at = null,
           needs_diligence = true
     where source_type = 'taxsale' and source_ref = %(ref)s and diligence_flag is not null
     returning signal_id, source_ref, county_fips, parcel_apn
"""


def _refs_from_csv(path):
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    key = None
    for cand in ("source_ref", "signal_ref", "ref", "signal_id"):
        if rows and cand in rows[0]:
            key = cand
            break
    if not key:
        raise SystemExit("csv needs a 'source_ref' (or signal_id) column")
    return [r[key].strip() for r in rows if r.get(key, "").strip()]


def promote(user_uuid, refs, apply):
    if not refs:
        raise SystemExit("refuse: no --ref/--csv targets. There is no promote-all (contract §3).")
    done, missing = [], []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        for ref in refs:
            cur.execute(PROMOTE, {"by": user_uuid, "ref": ref})
            got = cur.fetchone()
            (done if got else missing).append(got or ref)
        if apply:
            conn.commit()
        else:
            conn.rollback()
    print(f"{'PROMOTED' if apply else 'DRY-RUN (rolled back)'}: {len(done)} taxsale signal(s) "
          f"by {user_uuid}; {len(missing)} ref(s) not an un-promoted, un-flagged taxsale signal.")
    for d in done[:20]:
        print(f"  + {d['source_ref']}  ({d['county_fips']} {d['parcel_apn']})")
    for m in missing[:20]:
        print(f"  ? not-eligible / already-promoted / flagged: {m}")
    return {"promoted": len(done), "skipped": len(missing)}


def flag(user_uuid, refs, reason, apply, clear=False):
    """Human flag / clear-flag keystroke. Flagging records a diligence problem (the row can
    never mint or be promoted while flagged); clearing re-opens diligence fail-closed."""
    if not refs:
        raise SystemExit("refuse: no --ref targets. There is no flag-all / clear-all.")
    if not clear and not (reason or "").strip():
        raise SystemExit("refuse: --flag requires --reason (the flag IS the reason text).")
    sql = CLEAR_FLAG if clear else FLAG
    verb = "FLAG-CLEARED (diligence re-opened)" if clear else "FLAGGED (never mints)"
    done, missing = [], []
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        for ref in refs:
            cur.execute(sql, {"by": user_uuid, "ref": ref, "reason": (reason or "").strip()})
            got = cur.fetchone()
            (done if got else missing).append(got or ref)
        if apply:
            conn.commit()
        else:
            conn.rollback()
    print(f"{verb if apply else 'DRY-RUN (rolled back)'}: {len(done)} taxsale signal(s) "
          f"by {user_uuid}; {len(missing)} ref(s) not applicable.")
    for d in done[:20]:
        print(f"  * {d['source_ref']}  ({d['county_fips']} {d['parcel_apn']})")
    for m in missing[:20]:
        print(f"  ? not-applicable: {m}")
    return {"changed": len(done), "skipped": len(missing)}


def main():
    ap = argparse.ArgumentParser(description="human promote of tax-sale signals (the diligence gate)")
    ap.add_argument("--by", required=True, help="promoting user's uuid (audit: promoted_by)")
    ap.add_argument("--ref", action="append", default=[], help="a source_ref to promote (repeatable)")
    ap.add_argument("--csv", help="a reviewed diligence sheet with a source_ref/signal_id column")
    ap.add_argument("--apply", action="store_true", help="commit (default: dry-run, rolled back)")
    ap.add_argument("--flag", action="store_true",
                    help="flag the named refs instead of promoting (requires --reason)")
    ap.add_argument("--reason", help="the diligence problem found (stored as diligence_flag)")
    ap.add_argument("--clear-flag", action="store_true",
                    help="clear the flag on the named refs (re-opens diligence, never promotes)")
    a = ap.parse_args()
    refs = list(a.ref) + (_refs_from_csv(a.csv) if a.csv else [])
    if a.flag or a.clear_flag:
        flag(a.by, refs, a.reason, a.apply, clear=a.clear_flag)
    else:
        promote(a.by, refs, a.apply)


if __name__ == "__main__":
    sys.exit(main())

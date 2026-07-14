"""
load_parcel_owner.py — ingest county owner-of-record sets into landos.parcel_owner
(JOB 5.6). $0 / no network — a manifest-sealed local CSV -> DB upsert.

    python -m scripts.load_parcel_owner --county laporte [--dry-run]

The owner set currently lives ONLY in the assemblage CSV, outside the database —
that's the bug. This lands it in landos.parcel_owner (PK county_fips, parcel_apn),
ON CONFLICT DO NOTHING (idempotent, additive). Census before/after.

VINTAGE-CAPTURE LAW: government / treasurer / public-entity owner names are
flagged vintage_flag='govt' here at load — surfaced downstream, NEVER asserted
as a seller. The pattern lives in exactly one place (GOVT_RE below); fn_parcel_plat
reads the stored flag, it does not re-derive it.

When St. Joseph / Berrien / future counties land parcel geometry, add a COUNTIES
entry pointing at their owner pull — same idiom, no new code.
"""
import argparse
import csv
import os
import re

import psycopg

from app.config import DATABASE_URL
from app.run_manifest import sha256_file

# County registry — one entry per manifest-sealed owner pull. LaPorte = the
# assemblage Track-A scored parcels (sha 48b3b0b2… = the sealed recipe input).
COUNTIES = {
    "laporte": {
        "county_fips": "18091",
        "csv": os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "recipes/senior_assemblage/data/in/laporte_scored.csv"),
        "source": "assemblage:laporte_scored.csv (LaPorte County GIS AGOL owner roll)",
    },
}

PAGE = 2000

# Government / treasurer / public-entity owner-name pattern — VERBATIM the token
# set from the operator's fn_parcel_plat draft, ported here so the flag is stamped
# once at load. Word-boundaried so surnames (TOWNSEND, PARKER, BURNS) don't trip.
# Errs toward flagging: over-flagging a private LP named after a county is the
# SAFE direction (flag, never assert as seller); the reverse would be a liability.
GOVT_RE = re.compile(
    r"\b(?:"
    r"treasurer|tax[ -]?sale|tax deed|county|state of|usa|united states|"
    r"city of|town of|town|village of|township|school|building corporation|"
    r"department|dept|board of|commissioner|municipal|sanitary|drain|"
    r"utilit|water district|conservancy|redevelopment|housing authority|"
    r"land bank|library|fire (?:district|dept|department|protection)|park|"
    r"public|forest|cemetery"
    r")\b", re.IGNORECASE)

INSERT_SQL = """
    insert into landos.parcel_owner
      (county_fips, parcel_apn, owner_name, owner_mailing, source, source_ref, vintage_flag)
    values (%(county_fips)s, %(parcel_apn)s, %(owner_name)s, %(owner_mailing)s,
            %(source)s, %(source_ref)s, %(vintage_flag)s)
    on conflict (county_fips, parcel_apn) do nothing
"""


def assemble_mailing(row: dict) -> str | None:
    line = (row.get("owner_mailing_address") or "").strip()
    csz = " ".join(p for p in [(row.get("owner_mailing_city") or "").strip(),
                               (row.get("owner_mailing_state") or "").strip(),
                               (row.get("owner_mailing_zip") or "").strip()] if p)
    bits = [b for b in (line, csz) if b]
    return ", ".join(bits) or None


def vintage_flag_for(owner_name: str) -> str | None:
    return "govt" if GOVT_RE.search(owner_name or "") else None


def rows_from_csv(cfg: dict, source_ref: str):
    fips = cfg["county_fips"]
    with open(cfg["csv"], newline="") as f:
        for r in csv.DictReader(f):
            apn = (r.get("parcel_apn") or "").strip()
            owner = (r.get("owner_name") or "").strip()
            if not apn:
                yield None, "no_apn"; continue
            if not owner:
                yield None, "no_owner"; continue
            yield {
                "county_fips": fips,
                "parcel_apn": apn,
                "owner_name": owner,
                "owner_mailing": assemble_mailing(r),
                "source": cfg["source"],
                "source_ref": source_ref,
                "vintage_flag": vintage_flag_for(owner),
            }, None


def main(county: str = "laporte", dry_run: bool = False) -> dict:
    cfg = COUNTIES[county]
    if not os.path.exists(cfg["csv"]):
        raise SystemExit(f"owner CSV not found: {cfg['csv']}")
    source_ref = sha256_file(cfg["csv"])[:16]

    seen = loadable = govt = 0
    skips = {"no_apn": 0, "no_owner": 0}
    with psycopg.connect(DATABASE_URL) as conn, conn.cursor() as cur:
        cur.execute("select count(*) from landos.parcel_owner where county_fips=%s",
                    (cfg["county_fips"],))
        before = cur.fetchone()[0]
        print(f"[census] before: parcel_owner {cfg['county_fips']} = {before}")

        page = []
        for row, skip in rows_from_csv(cfg, source_ref):
            seen += 1
            if row is None:
                skips[skip] += 1
                continue
            loadable += 1
            if row["vintage_flag"] == "govt":
                govt += 1
            page.append(row)
            if not dry_run and len(page) >= PAGE:
                cur.executemany(INSERT_SQL, page)
                conn.commit()
                page = []
        if not dry_run and page:
            cur.executemany(INSERT_SQL, page)
            conn.commit()

        cur.execute("select count(*) from landos.parcel_owner where county_fips=%s",
                    (cfg["county_fips"],))
        after = cur.fetchone()[0]

    print(f"[census] rows={seen} loadable={loadable} "
          f"govt_flagged={govt} skipped_no_apn={skips['no_apn']} "
          f"skipped_no_owner={skips['no_owner']}"
          + ("  [dry-run: no writes]" if dry_run else ""))
    print(f"[census] after: parcel_owner {cfg['county_fips']} = {after} "
          f"(+{after - before})")
    return {"before": before, "after": after, "loadable": loadable,
            "govt_flagged": govt, "skips": skips, "source_ref": source_ref}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--county", default="laporte", choices=sorted(COUNTIES))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    main(county=a.county, dry_run=a.dry_run)

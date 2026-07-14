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
from app.score_absentee import classify_owner_type

# County registry — one entry per manifest-sealed owner pull. LaPorte = the
# assemblage Track-A scored parcels (sha 48b3b0b2… = the sealed recipe input).
COUNTIES = {
    "laporte": {
        "county_fips": "18091",
        "csv": os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "recipes/senior_assemblage/data/in/laporte_scored.csv"),
        "source": "assemblage:laporte_scored.csv (LaPorte County GIS AGOL owner roll)",
    },
    # --- W0 multi-county owner loads (../landos-ingest manifest-sealed CSVs, gitignored) ---
    # colmap folds each county's native header into the canonical shape. Vanderburgh is the
    # rich roll — YearBuilt + land/imp/tot assessed + LastSale* — that unblocks teardown +
    # class-code. Delaware carries class + acreage only (no year_built/assessed -> honest NULL).
    "vanderburgh": {
        "county_fips": "18163",
        "csv": "../landos-ingest/data/strategy/parcel_owner/staged/vanderburgh_parcel_owner.csv",
        "source": "arcgis:vanderburgh_evansvillegis_parcels (owner/assessor)",
        "derive_owner_kind": True,
        "colmap": {
            "parcel_apn": "StatePIN", "owner_name": "OwnerName",
            "owner_mailing_address": "own_street", "owner_mailing_city": "own_city",
            "owner_mailing_state": "own_state", "owner_mailing_zip": "own_zip",
            "situs_address": "prop_street", "class_code": "property_class",
            "acres": "acreage", "last_sale_date": "LastSaleDate", "year_built": "YearBuilt",
            "land_assessed": "land_assess", "improvement_assessed": "imp_assess",
            "total_assessed": "tot_assess", "last_sale_price": "LastSalePrice",
        },
    },
    "delaware": {
        "county_fips": "18035",
        "csv": "../landos-ingest/data/strategy/parcel_owner/staged/delaware_parcel_owner.csv",
        "source": "arcgis:delaware_parcels (owner/assessor)",
        "derive_owner_kind": True,
        "colmap": {
            "parcel_apn": "PIN", "owner_name": "OwnerName",
            "owner_mailing_address": "OwnerAddress", "owner_mailing_city": "OwnerCity",
            "owner_mailing_state": "OwnerState", "owner_mailing_zip": "OwnerZip",
            "situs_address": "StreetAddress", "class_code": "PropertyClassNum",
            "acres": "TotalAcreage",
        },
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

# W0 widen (mig 026): the mine attributes ride ON CONFLICT DO UPDATE so a reload
# backfills existing rows. owner_name/vintage/source are coalesced (never clobbered
# by a blank); the attribute columns are refreshed from the sealed CSV.
INSERT_SQL = """
    insert into landos.parcel_owner
      (county_fips, parcel_apn, owner_name, owner_mailing, source, source_ref, vintage_flag,
       situs_address, owner_mailing_city, owner_mailing_state, owner_mailing_zip,
       property_class, acres, owner_kind, owner_occupied_proxy, last_sale_date, tenure_floor,
       year_built, land_assessed, improvement_assessed, total_assessed, last_sale_price)
    values (%(county_fips)s, %(parcel_apn)s, %(owner_name)s, %(owner_mailing)s,
            %(source)s, %(source_ref)s, %(vintage_flag)s,
            %(situs_address)s, %(owner_mailing_city)s, %(owner_mailing_state)s, %(owner_mailing_zip)s,
            %(property_class)s, %(acres)s, %(owner_kind)s, %(owner_occupied_proxy)s,
            %(last_sale_date)s, %(tenure_floor)s,
            %(year_built)s, %(land_assessed)s, %(improvement_assessed)s, %(total_assessed)s,
            %(last_sale_price)s)
    on conflict (county_fips, parcel_apn) do update set
      owner_name           = coalesce(excluded.owner_name, landos.parcel_owner.owner_name),
      owner_mailing        = coalesce(excluded.owner_mailing, landos.parcel_owner.owner_mailing),
      vintage_flag         = excluded.vintage_flag,
      situs_address        = excluded.situs_address,
      owner_mailing_city   = excluded.owner_mailing_city,
      owner_mailing_state  = excluded.owner_mailing_state,
      owner_mailing_zip    = excluded.owner_mailing_zip,
      property_class       = excluded.property_class,
      acres                = excluded.acres,
      owner_kind           = excluded.owner_kind,
      owner_occupied_proxy = excluded.owner_occupied_proxy,
      last_sale_date       = excluded.last_sale_date,
      tenure_floor         = excluded.tenure_floor,
      year_built           = excluded.year_built,
      land_assessed        = excluded.land_assessed,
      improvement_assessed = excluded.improvement_assessed,
      total_assessed       = excluded.total_assessed,
      last_sale_price      = excluded.last_sale_price
"""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _date(v):
    s = (v or "").strip()
    return s or None          # ISO 'YYYY-MM-DD' or '' -> NULL; psycopg casts to date


def _sale_date(v):
    """Robust county sale-date -> ISO 'YYYY-MM-DD' or NULL. Accepts ISO and US
    M/D/YYYY (Vanderburgh's LastSaleDate); anything else -> None (never crash the cast)."""
    s = (v or "").strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        mo, d, y = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    return None


def _year_built(v):
    """YearBuilt -> int, but 0 / blank / pre-1700 = unknown -> NULL (never a fake year)."""
    y = _int(v)
    return y if (y and y > 1700) else None


def assemble_mailing(row: dict) -> str | None:
    line = (row.get("owner_mailing_address") or "").strip()
    csz = " ".join(p for p in [(row.get("owner_mailing_city") or "").strip(),
                               (row.get("owner_mailing_state") or "").strip(),
                               (row.get("owner_mailing_zip") or "").strip()] if p)
    bits = [b for b in (line, csz) if b]
    return ", ".join(bits) or None


def vintage_flag_for(owner_name: str) -> str | None:
    return "govt" if GOVT_RE.search(owner_name or "") else None


def derive_owner_kind(owner: str) -> str | None:
    """human | entity from the shared classifier. govt is left to vintage_flag (never a
    seller), so a govt name maps to NULL here rather than a fake 'human'/'entity'."""
    t = classify_owner_type(owner)                 # 'entity' | 'individual' | 'govt'
    return "entity" if t == "entity" else ("human" if t == "individual" else None)


def rows_from_csv(cfg: dict, source_ref: str):
    """One owner CSV row -> parcel_owner params. A per-county `colmap` (canonical_key ->
    source column) folds each county's native header into the canonical shape; absent =>
    identity (LaPorte's CSV already uses canonical names). owner_kind is derived from the
    name when the county carries no such column (derive_owner_kind=True)."""
    fips = cfg["county_fips"]
    cmap = cfg.get("colmap", {})
    want_kind = cfg.get("derive_owner_kind", False)

    def g(row, key):                                # read a canonical field through the map
        return row.get(cmap.get(key, key))

    with open(cfg["csv"], newline="") as f:
        for r in csv.DictReader(f):
            apn = (g(r, "parcel_apn") or "").strip()
            owner = (g(r, "owner_name") or "").strip()
            if not apn:
                yield None, "no_apn"; continue
            if not owner:
                yield None, "no_owner"; continue
            mailing_row = {
                "owner_mailing_address": (g(r, "owner_mailing_address") or "").strip(),
                "owner_mailing_city": (g(r, "owner_mailing_city") or "").strip(),
                "owner_mailing_state": (g(r, "owner_mailing_state") or "").strip(),
                "owner_mailing_zip": (g(r, "owner_mailing_zip") or "").strip(),
            }
            owner_kind = (g(r, "owner_kind") or "").strip() or None
            if not owner_kind and want_kind:
                owner_kind = derive_owner_kind(owner)
            yield {
                "county_fips": fips,
                "parcel_apn": apn,
                "owner_name": owner,
                "owner_mailing": assemble_mailing(mailing_row),
                "source": cfg["source"],
                "source_ref": source_ref,
                "vintage_flag": vintage_flag_for(owner),
                "situs_address": (g(r, "situs_address") or "").strip() or None,
                "owner_mailing_city": mailing_row["owner_mailing_city"] or None,
                "owner_mailing_state": mailing_row["owner_mailing_state"] or None,
                "owner_mailing_zip": mailing_row["owner_mailing_zip"] or None,
                "property_class": (g(r, "class_code") or "").strip() or None,
                "acres": _num(g(r, "acres")),
                "owner_kind": owner_kind,
                "owner_occupied_proxy": _int(g(r, "owner_occupied_proxy")),
                "last_sale_date": _sale_date(g(r, "last_sale_date")),
                "tenure_floor": _num(g(r, "tenure_floor")),
                "year_built": _year_built(g(r, "year_built")),
                "land_assessed": _num(g(r, "land_assessed")),
                "improvement_assessed": _num(g(r, "improvement_assessed")),
                "total_assessed": _num(g(r, "total_assessed")),
                "last_sale_price": _num(g(r, "last_sale_price")),
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

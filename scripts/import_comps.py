"""
import_comps.py — source-agnostic comps loader (the arbitrage data gap).

Pluggable source adapters (CompSourceAdapter) feed normalized comp rows into ONE
idempotent, provenance-stamped, per-county-canonicalized loader — same modular
doctrine as app/discovery (a new source = a new adapter, never a framework rewrite).

    python -m scripts.import_comps --source csv --path comps.csv          # dry-run
    python -m scripts.import_comps --source csv --path comps.csv --apply   # write
    python -m scripts.import_comps comps.csv                               # back-compat: csv dry-run

Adapters: csv (manual / VA-fed export), bsa (MI BS&A), insdf (IN SDF/XSoft),
acrevalue (PAID — STUB, no calls), + raw rows for tests. fetch() -> raw comp dicts;
the loader CANONICALIZES subject_apn per county (MI strip '11-' via reportall_parcel_id;
IN 71-form — the join keystone: wrong here = comps attach to the wrong/no parcel),
validates, and upserts on the (tenant_id, subject_apn, comp_apn, comp_address)
NULLS-NOT-DISTINCT natural key (>=1 of comp_apn/comp_address REQUIRED or comps collide).
MISSING -> NULL, never 0. Dry-run default; --apply writes. One pull can also carry the
subject's own last sale + assessor value (basis C, captured free) — see fetch() contract.

NEVER fabricate a comp or a sale price. Template: data/comps_template.csv.
"""
import abc
import argparse
import csv
import sys
from datetime import date
from typing import Any, Dict, List, Optional

import psycopg

from app.config import DATABASE_URL
from app.normalizers.reportall_owner import reportall_parcel_id

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
MI_BERRIEN = "26021"     # parcel_apn = county APN minus the '11-' prefix
IN_STJOSEPH = "18141"    # parcel_apn = 71-form (alt_id_1); the 018-form is the ingest id only

COLUMNS = ["subject_apn", "comp_address", "comp_apn", "sale_date", "sale_price",
           "lot_size", "acreage", "property_type", "distance_mi", "source",
           "provenance", "notes"]


# ---------------------------------------------------------------------------
# subject_apn canonicalization — the JOIN KEYSTONE. A comp's subject_apn MUST
# equal landos.owner_leads.parcel_apn or the comp attaches to nothing.
# ---------------------------------------------------------------------------
def canonicalize_subject_apn(apn: Optional[str], county_fips: Optional[str]) -> Optional[str]:
    a = (apn or "").strip()
    if not a:
        return None
    if county_fips == MI_BERRIEN:
        return reportall_parcel_id(a, "11-")     # '11-03-...' -> '03-...' (the stored form)
    # IN_STJOSEPH and default: parcel_apn is already the canonical (71-form for IN); pass through.
    return a


def _num(row, key):
    v = str(row.get(key) or "").strip().replace(",", "").replace("$", "")
    if not v:
        return None
    return float(v)  # raises ValueError on bad input


def validate_comp_row(row: dict):
    """Return (ok, cleaned|None, error). Pure — used by loader + tests."""
    subj = (row.get("subject_apn") or "").strip()
    if not subj:
        return False, None, "missing subject_apn"
    if subj.upper().startswith("EXAMPLE"):
        return False, None, "example row (skipped)"
    comp_apn = (row.get("comp_apn") or "").strip() or None
    comp_addr = (row.get("comp_address") or "").strip() or None
    if not comp_apn and not comp_addr:
        # NULLS NOT DISTINCT: a NULL/NULL pair collides with any other for the subject.
        return False, None, "need comp_apn or comp_address (natural key is NULLS NOT DISTINCT)"
    try:
        price = _num(row, "sale_price")
        lot = _num(row, "lot_size")
        acre = _num(row, "acreage")
        dist = _num(row, "distance_mi")
    except ValueError as e:
        return False, None, f"bad numeric value: {e}"
    if price is not None and price <= 0:
        return False, None, "sale_price must be > 0"
    sd = (row.get("sale_date") or "").strip() or None
    if sd:
        try:
            date.fromisoformat(sd)
        except ValueError:
            return False, None, f"bad sale_date {sd!r} (use YYYY-MM-DD)"
    cleaned = {
        "subject_apn": subj, "comp_address": comp_addr, "comp_apn": comp_apn,
        "sale_date": sd, "sale_price": price, "lot_size": lot, "acreage": acre,
        "property_type": (row.get("property_type") or "").strip() or None,
        "distance_mi": dist, "source": (row.get("source") or "").strip() or None,
        "provenance": (row.get("provenance") or "").strip() or "manual_entry",
        "notes": (row.get("notes") or "").strip() or None,
    }
    return True, cleaned, ""


def _upsert(cur, c) -> bool:
    """Idempotent upsert on the NULLS-NOT-DISTINCT natural key. Returns True if this
    was an INSERT (new row), False if an UPDATE (xmax<>0) — for the idempotency proof."""
    cur.execute(
        """
        insert into landos.comps
          (tenant_id, subject_apn, comp_address, comp_apn, sale_date, sale_price,
           lot_size, acreage, property_type, distance_mi, source, provenance, notes, updated_at)
        values (%(t)s,%(subject_apn)s,%(comp_address)s,%(comp_apn)s,%(sale_date)s,%(sale_price)s,
                %(lot_size)s,%(acreage)s,%(property_type)s,%(distance_mi)s,%(source)s,%(provenance)s,%(notes)s, now())
        on conflict (tenant_id, subject_apn, comp_apn, comp_address) do update set
          sale_date=excluded.sale_date, sale_price=excluded.sale_price, lot_size=excluded.lot_size,
          acreage=excluded.acreage, property_type=excluded.property_type, distance_mi=excluded.distance_mi,
          source=excluded.source, provenance=excluded.provenance, notes=excluded.notes, updated_at=now()
        returning (xmax = 0) as inserted
        """,
        {"t": TENANT, **c},
    )
    return bool(cur.fetchone()[0])


def normalize_and_upsert(cur, rows: List[dict], apply: bool) -> dict:
    """Canonicalize subject_apn per county, validate, and (if apply) upsert. cur may be
    None for a pure dry-run. Returns a summary with insert/update counts + per-subject roll-up."""
    valid, errors, skipped = [], [], 0
    for i, row in enumerate(rows, start=1):
        county = (str(row.get("county_fips") or "").strip() or None)
        canon = canonicalize_subject_apn(row.get("subject_apn"), county)
        row = {**row, "subject_apn": canon}
        ok, cleaned, err = validate_comp_row(row)
        if ok:
            valid.append(cleaned)
        elif "example" in err:
            skipped += 1
        else:
            errors.append((i, err, row.get("subject_apn")))
    inserted = updated = 0
    if apply and cur is not None:
        for c in valid:
            if _upsert(cur, c):
                inserted += 1
            else:
                updated += 1
    by_subject: Dict[str, int] = {}
    for c in valid:
        by_subject[c["subject_apn"]] = by_subject.get(c["subject_apn"], 0) + 1
    return {"valid": valid, "errors": errors, "skipped": skipped,
            "inserted": inserted, "updated": updated, "by_subject": by_subject}


# ===========================================================================
# Source adapters — fetch() -> RAW comp dicts. Same doctrine as DiscoveryAdapter.
# ===========================================================================
class CompSourceAdapter(abc.ABC):
    source: str                 # stamped into comps.source
    provenance: str             # stamped into comps.provenance
    access_mode: str            # 'manual' | 'va_fed' | 'automated' | 'stub'
    access_path: str = ""
    county_fips: str = ""

    @abc.abstractmethod
    def fetch(self, subjects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """subjects: [{parcel_apn (canonical), county_fips, situs_address, acreage}].
        Returns RAW comp dicts (subject_apn may be county-native — the loader
        canonicalizes): {subject_apn, county_fips, comp_apn|comp_address, sale_date,
        sale_price, acreage, lot_size, property_type, distance_mi, notes}.
        May also carry subject self-sale + assessor value for basis C (captured in the
        same pull, free). PUBLIC, ToS-clean only; a va_fed adapter reads an operator CSV."""


class CSVCompAdapter(CompSourceAdapter):
    source = "csv"
    provenance = "manual_entry"
    access_mode = "manual"

    def __init__(self, path: str):
        self.path = path
        self.access_path = path

    def fetch(self, subjects=None) -> List[Dict[str, Any]]:
        with open(self.path) as f:
            return list(csv.DictReader(f))


class BSACompAdapter(CompSourceAdapter):
    """Berrien MI sales via BS&A Online. WIRED ONLY AFTER the STEP 1c probe confirms
    robots/ToU permit it AND the raw shape is machine-readable. Drop-not-fight: any
    challenge -> stays unwired, comps come via CSVCompAdapter (VA/operator export)."""
    source = "bsa"
    provenance = "bsa_online"
    access_mode = "automated"
    county_fips = MI_BERRIEN
    access_path = "https://bsaonline.com/?uid=797"

    def fetch(self, subjects):
        raise NotImplementedError(
            "BS&A fetch is unwired pending the STEP 1c probe (robots.txt + ToU check, "
            "<=5 parcels, drop-not-fight). If prohibited/challenge-walled, comps come via "
            "CSVCompAdapter from a VA/operator BS&A export — same row shape.")


class INSDFCompAdapter(CompSourceAdapter):
    """St. Joseph IN sale prices via the Sales Disclosure dataset / XSoft Engage parcel
    pages. Same gating as BS&A; va_fed fallback (DLGF SDF export) if not machine-readable."""
    source = "in_sdf"
    provenance = "sales_disclosure"
    access_mode = "va_fed"
    county_fips = IN_STJOSEPH
    access_path = "https://engage.xsoftinc.com/stjoseph (+ IN DLGF SDF data)"

    def fetch(self, subjects):
        raise NotImplementedError(
            "IN SDF/XSoft fetch is unwired pending an IN probe; va_fed fallback is the "
            "DLGF Sales Disclosure export ingested via CSVCompAdapter.")


class AcreValueCompAdapter(CompSourceAdapter):
    """PAID, land-native comps. STUB — no live call without credentials AND explicit
    opt-in, per directive. Held until Jeramie's go."""
    source = "acrevalue"
    provenance = "acrevalue"
    access_mode = "stub"
    access_path = "https://www.acrevalue.com (paid API)"

    def fetch(self, subjects):
        import os
        if not (os.environ.get("ACREVALUE_API_KEY") and os.environ.get("ACREVALUE_ALLOW") == "1"):
            raise RuntimeError(
                "acrevalue is a PAID stub — no calls without ACREVALUE_API_KEY and "
                "ACREVALUE_ALLOW=1 (explicit opt-in). Holding per directive.")
        raise NotImplementedError("acrevalue fetch not implemented (stub).")


ADAPTERS = {"csv": CSVCompAdapter, "bsa": BSACompAdapter,
            "insdf": INSDFCompAdapter, "acrevalue": AcreValueCompAdapter}


def fetch_subjects(cur) -> List[Dict[str, Any]]:
    """The 136 qualified owner_leads — canonical parcel_apn + county for the adapters."""
    cur.execute(
        "select parcel_apn, county_fips, situs_address, acreage from landos.owner_leads "
        "where tenant_id=%s and not discovery_seed order by parcel_apn", (TENANT,))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _print_summary(summary: dict, apply: bool, label: str) -> None:
    mode = "APPLY" if apply else "DRY-RUN (no writes)"
    print(f"\ncomps loader [{label}] — {mode}")
    print(f"  valid={len(summary['valid'])}  inserted={summary['inserted']}  "
          f"updated={summary['updated']}  skipped(examples)={summary['skipped']}  "
          f"errors={len(summary['errors'])}")
    for apn, n in sorted(summary["by_subject"].items()):
        print(f"    subject {apn}: {n} comp(s)")
    for i, e, subj in summary["errors"][:12]:
        print(f"    row {i} ({subj}): {e}")


def run_adapter(source: str, path: Optional[str], apply: bool) -> None:
    adapter = (ADAPTERS[source](path) if source == "csv" else ADAPTERS[source]())
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            subjects = fetch_subjects(cur) if source in ("bsa", "insdf", "acrevalue") else None
            rows = adapter.fetch(subjects)
            summary = normalize_and_upsert(cur if apply else None, rows, apply)
            if apply:
                status = "ok" if not summary["errors"] else ("partial" if summary["valid"] else "error")
                cur.execute(
                    "insert into landos.comps_import_log "
                    "(tenant_id, source_file, row_count, valid_count, error_count, status, notes) "
                    "values (%s,%s,%s,%s,%s,%s,%s)",
                    (TENANT, f"{source}:{adapter.access_path}", len(rows), len(summary["valid"]),
                     len(summary["errors"]), status,
                     f"inserted={summary['inserted']} updated={summary['updated']} "
                     f"skipped={summary['skipped']}"))
                conn.commit()
    _print_summary(summary, apply, source)
    if not apply:
        print("  (dry-run — pass --apply to write)")


def main() -> None:
    ap = argparse.ArgumentParser(description="source-agnostic comps loader")
    ap.add_argument("path", nargs="?", help="CSV path (back-compat shorthand for --source csv)")
    ap.add_argument("--source", choices=list(ADAPTERS), help="comp source adapter")
    ap.add_argument("--path", dest="path_opt", help="CSV path for --source csv")
    ap.add_argument("--apply", action="store_true", help="write to landos.comps (default: dry-run)")
    args = ap.parse_args()
    source = args.source or ("csv" if args.path else None)
    if not source:
        ap.error("specify --source {csv,bsa,insdf,acrevalue} (or a CSV path)")
    path = args.path_opt or args.path
    if source == "csv" and not path:
        ap.error("--source csv needs a CSV path")
    run_adapter(source, path, args.apply)


if __name__ == "__main__":
    main()

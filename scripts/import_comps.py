"""
import_comps.py — manual comps importer (the #1 data gap, by hand for now).

    python -m scripts.import_comps <comps.csv>

Validates each row, upserts to landos.comps keyed by (subject_apn, comp_apn,
comp_address), and audit-logs the run to landos.comps_import_log. Rows whose
subject_apn starts with "EXAMPLE" are SKIPPED (the template's illustrative rows
never enter the live DB). provenance defaults to 'manual_entry'.

Template: data/comps_template.csv. NEVER fabricate a comp or a sale price.
"""
import csv
import sys
from datetime import date

import psycopg

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

COLUMNS = ["subject_apn", "comp_address", "comp_apn", "sale_date", "sale_price",
           "lot_size", "acreage", "property_type", "distance_mi", "source",
           "provenance", "notes"]


def _num(row, key):
    v = (row.get(key) or "").strip().replace(",", "").replace("$", "")
    if not v:
        return None
    return float(v)  # raises ValueError on bad input


def validate_comp_row(row: dict):
    """Return (ok, cleaned|None, error). Pure — used by importer + tests."""
    subj = (row.get("subject_apn") or "").strip()
    if not subj:
        return False, None, "missing subject_apn"
    if subj.upper().startswith("EXAMPLE"):
        return False, None, "example row (skipped)"
    comp_apn = (row.get("comp_apn") or "").strip() or None
    comp_addr = (row.get("comp_address") or "").strip() or None
    if not comp_apn and not comp_addr:
        return False, None, "need comp_apn or comp_address"
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


def _upsert(cur, c):
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
        """,
        {"t": TENANT, **c},
    )


def main(path: str) -> None:
    with open(path) as f:
        rows = list(csv.DictReader(f))
    valid, errors, skipped = [], [], 0
    for i, row in enumerate(rows, 2):  # row 1 = header
        ok, cleaned, err = validate_comp_row(row)
        if ok:
            valid.append(cleaned)
        elif "example" in err:
            skipped += 1
        else:
            errors.append((i, err))

    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            for c in valid:
                _upsert(cur, c)
            status = "ok" if not errors else ("partial" if valid else "error")
            cur.execute(
                "insert into landos.comps_import_log "
                "(tenant_id, source_file, row_count, valid_count, error_count, status, notes) "
                "values (%s,%s,%s,%s,%s,%s,%s)",
                (TENANT, path, len(rows), len(valid), len(errors), status,
                 f"skipped_examples={skipped}; " + ("; ".join(f"row{n}:{e}" for n, e in errors[:8]))),
            )
        conn.commit()

    print(f"Imported {len(valid)} comp(s); skipped {skipped} example row(s); {len(errors)} error(s).")
    for n, e in errors[:20]:
        print(f"  row {n}: {e}")
    subjects = sorted({c['subject_apn'] for c in valid})
    if subjects:
        print(f"Subjects with comps now: {', '.join(subjects)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.import_comps <comps.csv>")
        raise SystemExit(1)
    main(sys.argv[1])

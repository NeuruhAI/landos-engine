"""
stage_import_csv.py — archival raw-dump of county CSVs into the CRM staging
tables on lvdi (public.lead_import_batches + public.lead_import_rows). One batch
per file, so the scraped sets are preserved and visible in the Lead Import UI.

    python -m scripts.stage_import_csv <csv_path> [<csv_path> ...]

Contract (Jeramie's load spec, 2026-06-18):
  - Idempotent by file_sha256: a file whose sha already has a batch is SKIPPED
    (never double-loaded).
  - Preserves EVERY column verbatim in `raw` jsonb — archival, no normalization,
    no field-dropping. Blank/duplicate header names are positionally
    disambiguated (col_<i> / name__k) so a header->value object stays lossless;
    the EXACT original header (blanks and all) is archived in
    lead_import_batches.column_mapping.
  - Transactional per file: any error rolls the whole file back — no partial
    batch is ever left behind, so a failed file re-runs cleanly.
  - Rows inserted in 1000-row multi-VALUES chunks.
  - SOLE writer of public.lead_import_*. Never touches acquisition_leads, never
    reads or exposes landos.*. Connects as the privileged role (RLS bypassed on
    write); UI reads stay governed by the existing owner_admin/manager policies.
"""
import csv
import hashlib
import json
import os
import sys

import psycopg
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"          # APG
CREATED_BY = "57eb5e2c-1dc3-41ae-a97b-b55b601ea845"      # jeramieshicks@gmail.com (owner_admin)
CHUNK = 1000


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def read_csv(path: str):
    """(header, rows, encoding) preserving every column. UTF-8 (BOM-tolerant)
    then latin-1 fallback (latin-1 maps every byte, so storage stays lossless)."""
    for enc in ("utf-8-sig", "latin-1"):
        try:
            with open(path, newline="", encoding=enc) as f:
                rdr = csv.reader(f)
                header = next(rdr)
                rows = [r for r in rdr]
            return header, rows, enc
        except UnicodeDecodeError:
            continue
    raise RuntimeError(f"could not decode {path}")


def disambiguate(header):
    """Lossless unique keys: blank -> col_<i> (1-based), duplicate -> name__k."""
    keys, counts = [], {}
    for i, h in enumerate(header):
        base = (h or "").strip() or f"col_{i + 1}"
        if base in counts:
            counts[base] += 1
            base = f"{base}__{counts[base]}"
        else:
            counts[base] = 1
        keys.append(base)
    return keys


def stage_file(conn, path: str) -> dict:
    base = os.path.basename(path)
    sha = sha256_file(path)
    with conn.cursor() as cur:
        cur.execute(
            "select batch_id, row_count from public.lead_import_batches where file_sha256 = %s",
            (sha,),
        )
        hit = cur.fetchone()
        if hit:
            conn.rollback()
            return {"file": base, "sha256": sha, "result": "skipped, already loaded",
                    "batch_id": str(hit[0]), "rows": hit[1]}

        header, rows, enc = read_csv(path)
        keys = disambiguate(header)
        nkey = len(keys)

        cur.execute(
            """insert into public.lead_import_batches
                 (source, file_name, file_sha256, column_mapping, row_count,
                  status, tenant_id, created_by)
               values ('csv_upload', %s, %s, %s, %s, 'staged', %s, %s)
               returning batch_id""",
            (base, sha, Jsonb({"columns": header}), len(rows), TENANT, CREATED_BY),
        )
        batch_id = cur.fetchone()[0]

        inserted = 0
        row_num = 0
        for start in range(0, len(rows), CHUNK):
            chunk = rows[start:start + CHUNK]
            placeholders, params = [], []
            for r in chunk:
                row_num += 1
                rk = keys
                if len(r) > nkey:  # ragged row longer than header — keep the extras
                    rk = keys + [f"col_{j + 1}" for j in range(nkey, len(r))]
                raw = {rk[j]: r[j] for j in range(len(r))}
                placeholders.append("(%s,%s,%s,%s,'staged')")
                params += [batch_id, TENANT, row_num, Jsonb(raw)]
            cur.execute(
                "insert into public.lead_import_rows "
                "(batch_id, tenant_id, row_num, raw, status) values "
                + ",".join(placeholders),
                params,
            )
            inserted += len(chunk)

        # reconcile declared row_count to what actually landed
        cur.execute(
            "update public.lead_import_batches set row_count = %s where batch_id = %s",
            (inserted, batch_id),
        )
    conn.commit()
    return {"file": base, "sha256": sha, "result": "loaded", "batch_id": str(batch_id),
            "rows": inserted, "columns": nkey, "encoding": enc}


def main(paths) -> None:
    results = []
    with psycopg.connect(DATABASE_URL) as conn:
        for p in paths:
            p = os.path.expanduser(p)
            try:
                results.append(stage_file(conn, p))
            except Exception as e:  # noqa: BLE001 — report per-file, keep going
                conn.rollback()
                results.append({"file": os.path.basename(p), "result": f"ERROR: {e}"})
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m scripts.stage_import_csv <csv> [<csv> ...]")
        raise SystemExit(1)
    main(sys.argv[1:])

"""Identity-safe search of already-owned local files.

A historical phone attaches only on a deterministic key:
  candidate_id
  source_row_id / external_lead_id of form NC:{fips}:{apn}
  (fips, apn) jointly

Name-only similarity is counted as LOCAL_REJECTED_AMBIGUOUS and never attached.
iCloud-evicted files (st_blocks=0) are skipped so this sweep cannot force downloads.
"""
from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Dict, Iterable, List, Set

from .phone_normalize import normalize_us_phone
from .t12_append import apn_equal, source_row_id_for

PHONE_HEADERS = {
    "PHONE", "PHONE_1", "PHONE1", "PHONE_2", "PHONE2", "PHONE_3", "PHONE3",
    "MOBILE", "CELL", "CELL_PHONE", "OWNER_PHONE", "PHONE_E164", "PHONE_RAW",
    "FULL_PHONE_NUMBER", "BEST_PHONE", "DIAL",
}
ID_HEADERS = {"CANDIDATE_ID", "ROW_ID", "LEAD_IMPORT_ROW_ID"}
KEY_HEADERS = {"SOURCE_ROW_ID", "EXTERNAL_LEAD_ID", "CANONICAL_PROPERTY_KEY", "TARGET_KEY"}
MAX_FILE_BYTES = 80 * 1024 * 1024
SKIP_DIR_NAMES = {
    ".git", "node_modules", ".venv", "venv", "Library", "iCloud Drive",
    "_raw", "ncsbe", "irs",
}


def _evicted(path: Path) -> bool:
    try:
        st = path.stat()
    except OSError:
        return True
    return getattr(st, "st_blocks", 1) == 0


def _header_map(fieldnames: List[str]) -> Dict[str, str]:
    return {(h or "").strip().upper(): h for h in (fieldnames or [])}


def _has_phone_header(headers: Dict[str, str]) -> List[str]:
    return [headers[k] for k in headers if k in PHONE_HEADERS or k.startswith("PHONE")]


def iter_candidate_csvs(roots: Iterable[Path]) -> List[Path]:
    out = []
    for root in roots:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".")]
            for name in filenames:
                if not name.lower().endswith((".csv", ".tsv")):
                    continue
                path = Path(dirpath) / name
                if _evicted(path):
                    continue
                try:
                    if path.stat().st_size == 0 or path.stat().st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                out.append(path)
    return out


def sweep_local_owned(
    estate: List[dict],
    roots: Iterable[Path],
) -> dict:
    by_cand = {r["candidate_id"]: r for r in estate}
    by_natural = {}
    for r in estate:
        fips = (r.get("fips") or "").strip()
        apn = (r.get("apn") or "").strip()
        if fips and apn:
            by_natural[source_row_id_for(fips, apn)] = r
            by_natural[f"NC:{fips}:{apn.lstrip('0') or '0'}"] = r

    files_seen = 0
    records_seen = 0
    safe = []
    rejected_ambiguous = 0
    seen_keys = set()
    mi_or_foreign = 0

    for path in iter_candidate_csvs(roots):
        files_seen += 1
        try:
            with path.open(newline="", encoding="utf-8", errors="replace") as fh:
                sample = fh.read(4096)
                fh.seek(0)
                dialect = csv.Sniffer().sniff(sample) if sample.strip() else csv.excel
                reader = csv.DictReader(fh, dialect=dialect)
                headers = _header_map(reader.fieldnames or [])
                phone_cols = _has_phone_header(headers)
                if not phone_cols:
                    continue
                id_col = next((headers[k] for k in ID_HEADERS if k in headers), None)
                key_col = next((headers[k] for k in KEY_HEADERS if k in headers), None)
                fips_col = headers.get("FIPS") or headers.get("COUNTY_FIPS")
                apn_col = headers.get("APN") or headers.get("PARCEL_ID")
                name_col = headers.get("OWNER_NAME") or headers.get("OWNER")
                if not id_col and not key_col and not (fips_col and apn_col):
                    # Name-only file: count phone-bearing rows as ambiguous, never attach.
                    for row in reader:
                        if any((row.get(c) or "").strip() for c in phone_cols):
                            rejected_ambiguous += 1
                            records_seen += 1
                    continue
                for row in reader:
                    phones = [(row.get(c) or "").strip() for c in phone_cols]
                    phones = [p for p in phones if p]
                    if not phones:
                        continue
                    records_seen += 1
                    src = None
                    if id_col and (row.get(id_col) or "").strip() in by_cand:
                        src = by_cand[row[id_col].strip()]
                    if src is None and key_col:
                        key = (row.get(key_col) or "").strip()
                        src = by_natural.get(key)
                    if src is None and fips_col and apn_col:
                        fips = "".join(ch for ch in (row.get(fips_col) or "") if ch.isdigit())
                        apn = (row.get(apn_col) or "").strip()
                        if fips.startswith("26") or fips.startswith("18"):
                            mi_or_foreign += 1
                            continue
                        for cand in (by_natural.get(source_row_id_for(fips, apn)),):
                            if cand:
                                src = cand
                                break
                        if src is None:
                            for estate_row in (by_cand.values() if False else []):
                                break
                            # Direct APN+FIPS compare against map
                            src = by_natural.get(source_row_id_for(fips, apn))
                            if src is None:
                                for er in [by_cand[k] for k in ()]:
                                    pass
                    if src is None:
                        rejected_ambiguous += 1
                        continue
                    fips = (src.get("fips") or "")
                    if fips.startswith("26") or fips.startswith("18"):
                        mi_or_foreign += 1
                        continue
                    for raw in phones:
                        phone = normalize_us_phone(raw)
                        if phone.classification != "VALID_US" or not phone.e164:
                            continue
                        ident = (src["candidate_id"], phone.e164)
                        if ident in seen_keys:
                            continue
                        seen_keys.add(ident)
                        safe.append({
                            "candidate_id": src["candidate_id"],
                            "apn": src.get("apn") or "",
                            "phone": phone.e164,
                            "county": src.get("county") or "",
                            "fips": src.get("fips") or "",
                            "owner_name": src.get("owner_name") or "",
                            "phone_source": "LOCAL_OWNED_IDENTITY",
                            "local_file": str(path),
                            "numbering_plan_valid": "YES",
                        })
        except Exception:
            continue

    return {
        "LOCAL_FILES_SCANNED": files_seen,
        "LOCAL_PHONE_RECORDS_SEEN": records_seen,
        "LOCAL_IDENTITY_SAFE_MATCHES": len(safe),
        "LOCAL_REJECTED_AMBIGUOUS": rejected_ambiguous,
        "LOCAL_MI_IN_EXCLUDED": mi_or_foreign,
        "rows": safe,
    }

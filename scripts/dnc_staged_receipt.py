"""Staged Tracerfy DNC receipt for canonical contact intake.

This is the missing hop between a paid (or resumed) Tracerfy /dnc/queue
result and scripts.intake_tracerfy_phones. It writes files only.

It never:
  * calls Tracerfy
  * writes landos.* or public.*
  * infers consent
  * weakens an opt-out
  * treats a phone's existence as permission to call
  * stamps dnc_status='clear' (only intake may bless clear from scrub_status=clean)

Intake (scripts.intake_tracerfy_phones) is the sole spine DNC writer.
sync_to_crm is the sole CRM writer. public.callable_leads remains callability
authority. Unknown / missing / synthetic-555 evidence stays non-callable.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any


CSV_FIELDS = [
    "target_key", "full_name", "phone", "phone_type", "confidence", "source",
    "pulled_at", "scrub_status", "scrub_flags", "scrubbed_at",
]
RECEIPT_KIND = "tracerfy_dnc_staged_receipt"
SOURCE_STAMP = "tracerfy_dnc_receipt"
PROVIDER_DNC_QUEUE = "{base}/dnc/queue/{queue_id}"

# Read-only CRM hop: owner_leads.lead_id -> acquisition_leads.lead_id.
# Intake's key-convention census accepts only uniform CRM lead_id=(1,0,0)
# for staged DNC blessing. LandOS lead_id receipts are routed away from
# intake to import_resolved and can never stamp clear.
CRM_TARGET_KEY_SQL = """
    select a.source_lead_id::text as owner_lead_id,
           a.lead_id::text as crm_lead_id
      from public.acquisition_leads a
     where a.tenant_id = %(tenant)s
       and a.source_lead_id = any(%(owner_lead_ids)s::uuid[])
     order by a.source_lead_id, a.lead_id
"""


class DncReceiptError(RuntimeError):
    """Fail-closed receipt construction. Never fabricate a partial CSV."""


def nanp_key(phone: str) -> str:
    number = re.sub(r"\D", "", phone or "")
    if len(number) == 10:
        return number
    if len(number) == 11 and number.startswith("1"):
        return number[1:]
    raise DncReceiptError(
        f"STOP — unsupported NANP phone shape: digit_length={len(number)}")


def is_synthetic_555(phone: str) -> bool:
    """NANP central-office code (digits 4-6 of the 10-digit number) is 555.

    Matches public.callable_leads migration 037: fictional/test exchange,
    never dialable regardless of vendor is_clean.
    """
    return nanp_key(phone)[3:6] == "555"


def classify_vendor_result(
    info: dict[str, Any] | None,
    phone: str | None = None,
) -> tuple[str, str]:
    """Map vendor truth to staged scrub_status.

    clean     — explicit is_clean with no DNC flags and not a 555 exchange
    dnc       — explicit block (flags or is_clean False)
    unchecked — missing/unknown/synthetic; never a clear verdict

    Unknown must remain non-callable without becoming a sticky do_not_call.
    Intake only blesses scrub_status='clean'. target_verdict treats
    unchecked as unverified (None) and dnc-only as do_not_call.
    """
    if phone and is_synthetic_555(phone):
        return "unchecked", "synthetic_555"
    if info is None:
        return "unchecked", "vendor_missing"
    flag_names = [
        name for name in ("national_dnc", "state_dnc", "dma", "litigator")
        if bool(info.get(name))
    ]
    is_clean = info.get("is_clean")
    if is_clean is True and not flag_names:
        return "clean", ""
    if flag_names or is_clean is False:
        return "dnc", ";".join(flag_names or ["vendor_not_clean"])
    return "unchecked", "vendor_unknown"


def canonicalize_vendor_index(
    vendor_index: dict[str, dict[str, Any]],
    *,
    queue_id: str,
) -> dict[str, dict[str, Any]]:
    canonical: dict[str, dict[str, Any]] = {}
    collisions: list[str] = []
    for raw_phone, info in vendor_index.items():
        key = nanp_key(raw_phone)
        if key in canonical:
            collisions.append(key)
        canonical[key] = info
    if collisions:
        raise DncReceiptError(
            f"STOP — Tracerfy result alias collision: queue_id={queue_id} "
            f"duplicate_keys={sorted(set(collisions))}; no receipt written")
    return canonical


def require_exact_vendor_membership(
    submitted_phones: list[str],
    vendor_index: dict[str, dict[str, Any]],
    *,
    queue_id: str,
) -> dict[str, dict[str, Any]]:
    submitted = [nanp_key(p) for p in submitted_phones]
    submitted_set = set(submitted)
    if len(submitted_set) != len(submitted):
        dupes = sorted(k for k in submitted_set if submitted.count(k) > 1)
        raise DncReceiptError(
            f"STOP — submitted phone alias collision: queue_id={queue_id} "
            f"duplicate_keys={dupes}; no receipt written")
    canonical = canonicalize_vendor_index(vendor_index, queue_id=queue_id)
    returned = set(canonical)
    if returned != submitted_set:
        missing = sorted(submitted_set - returned)
        extra = sorted(returned - submitted_set)
        raise DncReceiptError(
            f"STOP — Tracerfy result mismatch: queue_id={queue_id} "
            f"missing={missing} extra={extra}; no partial receipt written")
    return canonical


def compile_crm_target_key_map(
    rows: list[dict[str, Any]],
    required_owner_lead_ids: list[str],
) -> dict[str, str]:
    """owner_lead_id -> CRM acquisition_leads.lead_id. Fail closed on drift."""
    required = [str(x).strip() for x in required_owner_lead_ids]
    required_set = set(required)
    if len(required_set) != len(required):
        raise DncReceiptError(
            "STOP — duplicate owner_lead_id in DNC receipt mapping request")
    by_owner: dict[str, str] = {}
    owner_collisions: list[str] = []
    for row in rows:
        oid = str(row.get("owner_lead_id") or "").strip()
        cid = str(row.get("crm_lead_id") or "").strip()
        if not oid or not cid:
            raise DncReceiptError(
                "STOP — CRM target-key map row missing owner_lead_id or crm_lead_id")
        if oid in by_owner and by_owner[oid] != cid:
            owner_collisions.append(oid)
        by_owner[oid] = cid
    if owner_collisions:
        raise DncReceiptError(
            "STOP — owner_lead_id maps to multiple CRM lead_ids: "
            f"{sorted(set(owner_collisions))}")
    crm_owners: dict[str, list[str]] = {}
    for oid, cid in by_owner.items():
        crm_owners.setdefault(cid, []).append(oid)
    crm_collisions = {cid: oids for cid, oids in crm_owners.items() if len(oids) > 1}
    if crm_collisions:
        raise DncReceiptError(
            "STOP — CRM lead_id maps from multiple owner_leads: "
            f"{sorted(crm_collisions)}")
    missing = sorted(required_set - set(by_owner))
    if missing:
        raise DncReceiptError(
            "STOP — CRM acquisition_leads.lead_id mapping missing for "
            f"owner_lead_ids={missing}; intake cannot bless clear on LandOS keys")
    return by_owner


def submitted_phones_from_recs(recs: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[str]:
    seen: list[str] = []
    seen_set: set[str] = set()
    for _target, contacts in recs:
        for phone in contacts.get("phones") or []:
            number = phone.get("number")
            if not number:
                continue
            key = nanp_key(number)
            if key not in seen_set:
                seen_set.add(key)
                seen.append(number)
    return seen


def owner_ids_with_phones(recs: list[tuple[dict[str, Any], dict[str, Any]]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for target, contacts in recs:
        if not contacts.get("phones"):
            continue
        oid = str(target["lead_id"]).strip()
        if oid not in seen:
            seen.add(oid)
            out.append(oid)
    return out


def build_staged_rows(
    recs: list[tuple[dict[str, Any], dict[str, Any]]],
    vendor_index: dict[str, dict[str, Any]],
    *,
    target_key_by_owner: dict[str, str],
    scrubbed_at: str,
    source: str = SOURCE_STAMP,
    queue_id: str,
) -> list[dict[str, str]]:
    submitted = submitted_phones_from_recs(recs)
    canonical = require_exact_vendor_membership(
        submitted, vendor_index, queue_id=queue_id)
    compile_crm_target_key_map(
        [{"owner_lead_id": k, "crm_lead_id": v} for k, v in target_key_by_owner.items()],
        owner_ids_with_phones(recs),
    )
    staged: list[dict[str, str]] = []
    seen_pairs: set[tuple[str, str]] = set()
    for target, contacts in recs:
        owner_id = str(target["lead_id"]).strip()
        if not contacts.get("phones"):
            continue
        target_key = target_key_by_owner[owner_id]
        full_name = target.get("owner_name") or ""
        for phone in contacts["phones"]:
            raw = phone.get("number") or ""
            key = nanp_key(raw)
            pair = (target_key, key)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            status, flags = classify_vendor_result(canonical.get(key), raw)
            confidence = phone.get("confidence")
            staged.append({
                "target_key": target_key,
                "full_name": full_name,
                "phone": raw,
                "phone_type": phone.get("type") or "",
                "confidence": "" if confidence in (None, "") else str(confidence),
                "source": source,
                "pulled_at": scrubbed_at,
                "scrub_status": status,
                "scrub_flags": flags,
                "scrubbed_at": scrubbed_at,
            })
    staged.sort(key=lambda row: (row["target_key"], nanp_key(row["phone"])))
    return staged


def _write_private(path: Path, writer) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", newline="") as f:
            writer(f)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def receipt_paths(output_dir: Path | str, dnc_queue_id: str) -> dict[str, Path]:
    output_dir = Path(output_dir)
    stem = f"tracerfy_dnc_receipt_{dnc_queue_id}"
    return {
        "csv": output_dir / f"{stem}.csv",
        "manifest": output_dir / f"{stem}.manifest.json",
        "vendor": output_dir / f"tracerfy_dnc_vendor_{dnc_queue_id}.json",
    }


def persist_vendor_payload(
    output_dir: Path | str,
    *,
    dnc_queue_id: str,
    vendor_rows: list[Any],
    trace_queue_id: str | int | None = None,
    provider_base: str = "https://tracerfy.com/v1/api",
) -> dict[str, Any]:
    """Durable vendor dump for crash/restart. No network. No DB."""
    paths = receipt_paths(output_dir, str(dnc_queue_id))
    payload = {
        "kind": RECEIPT_KIND + "_vendor",
        "dnc_queue_id": str(dnc_queue_id),
        "trace_queue_id": None if trace_queue_id is None else str(trace_queue_id),
        "provider_operation": PROVIDER_DNC_QUEUE.format(
            base=provider_base.rstrip("/"), queue_id=dnc_queue_id),
        "rows": vendor_rows,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    vendor_path = paths["vendor"]
    if vendor_path.exists():
        existing = vendor_path.read_bytes()
        if existing != raw:
            raise DncReceiptError(
                f"STOP — existing vendor dump disagrees: {vendor_path}")
        return {
            "path": vendor_path,
            "sha256": _sha256_bytes(existing),
            "payload": payload,
            "reused": True,
        }

    def writer(f):
        f.write(raw.decode())

    _write_private(vendor_path, writer)
    return {
        "path": vendor_path,
        "sha256": _sha256_bytes(raw),
        "payload": payload,
        "reused": False,
    }


def persist_dnc_staged_receipt(
    recs: list[tuple[dict[str, Any], dict[str, Any]]],
    vendor_index: dict[str, dict[str, Any]],
    *,
    output_dir: Path | str,
    dnc_queue_id: str,
    target_key_by_owner: dict[str, str],
    scrubbed_at: str,
    trace_queue_id: str | int | None = None,
    vendor_rows: list[Any] | None = None,
    provider_base: str = "https://tracerfy.com/v1/api",
    source: str = SOURCE_STAMP,
) -> dict[str, Any]:
    """Write the ccT3 staged CSV + provenance. Deterministic for a fixed scrubbed_at."""
    dnc_queue_id = str(dnc_queue_id)
    paths = receipt_paths(output_dir, dnc_queue_id)
    staged = build_staged_rows(
        recs,
        vendor_index,
        target_key_by_owner=target_key_by_owner,
        scrubbed_at=scrubbed_at,
        source=source,
        queue_id=dnc_queue_id,
    )
    counts = Counter(row["scrub_status"] for row in staged)
    vendor_dump = persist_vendor_payload(
        output_dir,
        dnc_queue_id=dnc_queue_id,
        vendor_rows=list(vendor_rows or []),
        trace_queue_id=trace_queue_id,
        provider_base=provider_base,
    )

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    w.writerows(staged)
    expected_csv = buf.getvalue().encode()

    csv_path = paths["csv"]
    if csv_path.exists():
        existing = csv_path.read_bytes()
        if existing != expected_csv:
            raise DncReceiptError(
                f"STOP — existing staged receipt disagrees: {csv_path}")
        csv_sha = _sha256_bytes(existing)
        reused = True
    else:
        _write_private(csv_path, lambda f: f.write(expected_csv.decode()))
        csv_sha = _sha256_bytes(expected_csv)
        reused = False

    manifest = {
        "kind": RECEIPT_KIND,
        "dnc_queue_id": dnc_queue_id,
        "trace_queue_id": None if trace_queue_id is None else str(trace_queue_id),
        "provider_operation": PROVIDER_DNC_QUEUE.format(
            base=provider_base.rstrip("/"), queue_id=dnc_queue_id),
        "generated_at_utc": scrubbed_at,
        "source": source,
        "csv_file": csv_path.name,
        "csv_sha256": csv_sha,
        "vendor_file": vendor_dump["path"].name,
        "vendor_sha256": vendor_dump["sha256"],
        "submitted_phones": [nanp_key(p) for p in submitted_phones_from_recs(recs)],
        "target_key_by_owner": dict(sorted(target_key_by_owner.items())),
        "rows": len(staged),
        "clean": counts["clean"],
        "dnc": counts["dnc"],
        "unchecked": counts["unchecked"],
        "reused": reused,
    }

    def manifest_writer(f):
        f.write(json.dumps(manifest, sort_keys=True, indent=2))
        f.write("\n")

    manifest_path = paths["manifest"]
    if manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        comparable = {k: v for k, v in manifest.items() if k != "reused"}
        existing_cmp = {k: v for k, v in existing.items() if k != "reused"}
        if existing_cmp != comparable:
            raise DncReceiptError(
                f"STOP — existing DNC receipt manifest disagrees: {manifest_path}")
    else:
        _write_private(manifest_path, manifest_writer)

    return {
        "csv": csv_path,
        "manifest": manifest_path,
        "vendor": vendor_dump["path"],
        "csv_sha256": csv_sha,
        "staged": staged,
        "manifest_body": manifest,
        "reused": reused,
    }

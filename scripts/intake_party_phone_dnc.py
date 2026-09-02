"""Party-phone DNC intake — the A-PRIME writer. No owner_leads minting.

    python -m scripts.intake_party_phone_dnc                 # dry-run (default)
    python -m scripts.intake_party_phone_dnc --apply         # founder-gated writes
    python -m scripts.intake_party_phone_dnc --limit 5

Writes the July skip_trace_meta phone shape onto public.party_phones +
candidate_contact_resolution (lead-level). PASS-only. csv_sha256 on both
levels. Never copies into acquisition_leads.phone_1. Never calls a vendor.
Never purchases.

Input is a staged ccT3 CSV (scripts.dnc_staged_receipt.CSV_FIELDS). target_key
is the candidate import_row_id (uuid) or party_id. Mixed conventions stop.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from scripts.dnc_staged_receipt import nanp_key
from scripts.intake_tracerfy_phones import is_scrub_pass

__VERSION__ = "aprime-party-phone-dnc/1.0.0"
DNC_STATES = ("clear", "do_not_call", "unchecked")
JULY_PHONE_KEYS = (
    "type",
    "number",
    "source",
    "confidence",
    "dnc_status",
    "verified_2x",
    "dnc_scrubbed_at",
    "dnc_verified_via",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def is_synthetic_555(phone: str) -> bool:
    try:
        return nanp_key(phone)[3:6] == "555"
    except Exception:
        return False


def e164(phone: str) -> str:
    key = nanp_key(phone)
    return "+1" + key


def july_phone_entry(
    *,
    number: str,
    phone_type: str,
    source: str,
    confidence: float,
    dnc_status: str,
    scrubbed_at: str,
    verified_via: str,
    verified_2x: bool = True,
) -> Dict[str, Any]:
    if dnc_status not in DNC_STATES:
        raise ValueError(f"dnc_status not in {DNC_STATES}: {dnc_status}")
    entry = {
        "type": phone_type or "mobile",
        "number": e164(number) if dnc_status != "unchecked" or len(re.sub(r"\D", "", number)) >= 10 else number,
        "source": source,
        "confidence": confidence,
        "dnc_status": dnc_status,
        "verified_2x": bool(verified_2x),
        "dnc_scrubbed_at": scrubbed_at if dnc_status != "unchecked" else "",
        "dnc_verified_via": verified_via if dnc_status != "unchecked" else "",
    }
    return {k: entry[k] for k in JULY_PHONE_KEYS}


def scrub_receipt(
    *,
    source: str,
    csv_sha256: str,
    scrubbed_at: str,
    dnc_phone_hits: int,
    clean_phone_hits: int,
    csv_file: str,
    run_id: str,
) -> Dict[str, Any]:
    return {
        "source": source,
        "dnc_flags": {},
        "csv_sha256": csv_sha256,
        "scrubbed_at": scrubbed_at,
        "dnc_phone_hits": dnc_phone_hits,
        "clean_phone_hits": clean_phone_hits,
        "csv_file": csv_file,
        "run_id": run_id,
    }


def lead_level_status(phone_statuses: Iterable[str]) -> str:
    """Lead-level clear only if at least one clean and zero dnc hits. Mixed → do_not_call."""
    states = {s for s in phone_statuses if s in DNC_STATES}
    if "do_not_call" in states:
        return "do_not_call"
    if states == {"clear"} or (states == {"clear", "unchecked"} and "clear" in states):
        return "clear" if "clear" in states and "do_not_call" not in states else "unchecked"
    if "clear" in states and "do_not_call" not in states:
        return "clear"
    return "unchecked"


def project_owner_leads_meta(meta: Mapping[str, Any]) -> Dict[str, Any]:
    """Map the proven July skip_trace_meta onto the party-phone write shape. Pure."""
    phones_in = list(meta.get("phones") or [])
    projected = []
    for p in phones_in:
        if not isinstance(p, dict):
            continue
        number = str(p.get("number") or "")
        if not number:
            continue
        projected.append(
            july_phone_entry(
                number=number,
                phone_type=str(p.get("type") or "mobile"),
                source=str(p.get("source") or "truepeoplesearch"),
                confidence=float(p.get("confidence") or 0.9),
                dnc_status=str(p.get("dnc_status") or "unchecked"),
                scrubbed_at=str(p.get("dnc_scrubbed_at") or ""),
                verified_via=str(p.get("dnc_verified_via") or ""),
                verified_2x=bool(p.get("verified_2x", True)),
            )
        )
    receipt = dict(meta.get("scrub_receipt") or {})
    return {
        "lead_dnc_status": str(meta.get("dnc_status") or "unchecked"),
        "lead_dnc_scrubbed_at": str(meta.get("dnc_scrubbed_at") or receipt.get("scrubbed_at") or ""),
        "phones": projected,
        "scrub_receipt": receipt,
        "import_provenance": dict(meta.get("import_provenance") or {}),
    }


def plan_from_staged_csv(
    csv_path: Path,
    *,
    source: str = "ccT3",
    run_id: str | None = None,
) -> Dict[str, Any]:
    raw = csv_path.read_bytes()
    csv_sha = sha256_bytes(raw)
    with csv_path.open(newline="") as f:
        reader = csv.DictReader(f)
        fields = set(reader.fieldnames or [])
        rows = list(reader)
    missing = [c for c in ("target_key", "phone", "scrub_status", "scrubbed_at") if c not in fields]
    if missing:
        raise RuntimeError(f"STOP — staged CSV missing {missing}")
    by_target: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = (row.get("target_key") or "").strip()
        by_target[key].append(row)
    if any(not k for k in by_target):
        raise RuntimeError("STOP — empty target_key")
    try:
        for k in by_target:
            uuid.UUID(k)
    except ValueError as exc:
        raise RuntimeError("STOP — target_key must be a candidate or party uuid") from exc

    stamp = utc_now()
    run_id = run_id or f"party-dnc-{stamp[:10].replace('-', '')}"
    planned: List[Dict[str, Any]] = []
    for target, group in sorted(by_target.items()):
        phone_writes = []
        for row in group:
            number = row.get("phone") or ""
            if is_synthetic_555(number):
                status = "unchecked"
            elif is_scrub_pass(row):
                status = "clear"
            elif (row.get("scrub_status") or "").strip().lower() in ("dnc", "do_not_call"):
                status = "do_not_call"
            else:
                status = "unchecked"
            scrubbed_at = (row.get("scrubbed_at") or "").strip()
            if status != "unchecked" and not scrubbed_at:
                raise RuntimeError(
                    f"STOP — {status} without dnc_scrubbed_at for {target} {number}"
                )
            phone_writes.append(
                {
                    "target_key": target,
                    "phone_e164": e164(number) if re.sub(r"\D", "", number) else number,
                    "dnc_status": status,
                    "dnc_scrubbed_at": scrubbed_at if status != "unchecked" else None,
                    "entry": july_phone_entry(
                        number=number,
                        phone_type=row.get("phone_type") or "mobile",
                        source=source,
                        confidence=0.9,
                        dnc_status=status,
                        scrubbed_at=scrubbed_at,
                        verified_via=source,
                    ),
                    "row": row,
                }
            )
        statuses = [w["dnc_status"] for w in phone_writes]
        lead_status = lead_level_status(statuses)
        if lead_status == "clear" and not any(w["dnc_scrubbed_at"] for w in phone_writes if w["dnc_status"] == "clear"):
            raise RuntimeError(f"STOP — lead-level clear without timestamp: {target}")
        planned.append(
            {
                "target_key": target,
                "lead_dnc_status": lead_status,
                "lead_dnc_scrubbed_at": next(
                    (w["dnc_scrubbed_at"] for w in phone_writes if w["dnc_status"] == lead_status and w["dnc_scrubbed_at"]),
                    None,
                ),
                "phones": phone_writes,
                "scrub_receipt": scrub_receipt(
                    source=source,
                    csv_sha256=csv_sha,
                    scrubbed_at=stamp,
                    dnc_phone_hits=sum(s == "do_not_call" for s in statuses),
                    clean_phone_hits=sum(s == "clear" for s in statuses),
                    csv_file=csv_path.name,
                    run_id=run_id,
                ),
            }
        )
    return {
        "kind": "party_phone_dnc_intake_plan",
        "version": __VERSION__,
        "csv_file": str(csv_path),
        "csv_sha256": csv_sha,
        "run_id": run_id,
        "generated_at": stamp,
        "apply": False,
        "rows": len(rows),
        "targets": len(planned),
        "clean_phones": sum(w["dnc_status"] == "clear" for t in planned for w in t["phones"]),
        "dnc_phones": sum(w["dnc_status"] == "do_not_call" for t in planned for w in t["phones"]),
        "unchecked_phones": sum(w["dnc_status"] == "unchecked" for t in planned for w in t["phones"]),
        "writes_acquisition_leads_phone": False,
        "mints_owner_leads": False,
        "provider_spend_usd": 0.0,
        "planned": planned,
    }


WRITE_PHONE_SQL = """
    update public.party_phones
       set dnc_status = %(dnc_status)s,
           dnc_scrubbed_at = %(dnc_scrubbed_at)s,
           dnc_verified_via = %(dnc_verified_via)s,
           scrub_receipt = %(scrub_receipt)s
     where party_id = %(party_id)s
       and tenant_id = %(tenant)s
       and right(regexp_replace(phone_e164, '[^0-9]', '', 'g'), 10)
           = right(regexp_replace(%(phone)s, '[^0-9]', '', 'g'), 10)
"""

WRITE_CCR_SQL = """
    update public.candidate_contact_resolution
       set dnc_status = %(dnc_status)s,
           dnc_scrubbed_at = %(dnc_scrubbed_at)s
     where import_row_id = %(import_row_id)s
       and tenant_id = %(tenant)s
"""

FORBIDDEN_SQL = "acquisition_leads"


def assert_no_phone1_write(sql: str) -> None:
    blob = sql.lower()
    if "update public.acquisition_leads" in blob or "acquisition_leads.phone_1" in blob:
        raise RuntimeError("STOP — party-phone intake must not write acquisition_leads.phone_1")


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="intake_party_phone_dnc")
    p.add_argument("--csv", required=False, help="staged ccT3 CSV")
    p.add_argument("--apply", action="store_true", help="write (founder-gated)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--source", default="ccT3")
    args = p.parse_args(argv)
    if not args.csv:
        p.print_help()
        return 64
    path = Path(args.csv)
    plan = plan_from_staged_csv(path, source=args.source)
    if args.limit:
        plan["planned"] = plan["planned"][: args.limit]
        plan["targets"] = len(plan["planned"])
    assert_no_phone1_write(WRITE_PHONE_SQL)
    assert_no_phone1_write(WRITE_CCR_SQL)
    if FORBIDDEN_SQL in WRITE_PHONE_SQL:
        raise RuntimeError("STOP")
    if args.apply:
        print("STOP — --apply refused in this session. Founder spend/write authorization required.")
        print(json.dumps({k: plan[k] for k in plan if k != "planned"}, sort_keys=True, indent=2))
        return 78
    out = {k: v for k, v in plan.items() if k != "planned"}
    out["apply"] = False
    print(json.dumps(out, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

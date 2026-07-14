"""
intake_tracerfy_phones.py — land the DNC-scrubbed Tracerfy phones on the SPINE.

    python -m scripts.intake_tracerfy_phones                 # dry-run (default, no writes)
    python -m scripts.intake_tracerfy_phones --apply          # write skip_trace_meta.phones
    python -m scripts.intake_tracerfy_phones --limit 5        # sample (dry-run proof)

WHAT THIS IS (and is NOT a parallel writer): the ONLY durable home for a phone
is landos.owner_leads.skip_trace_meta.phones — the exact surface
scripts/import_resolved.py writes and scripts/sync_to_crm.py reads. This script
mirrors that write verbatim (same table, same columns, same phones[] shape,
same import_provenance block); it just adapts the Tracerfy batch-async staged
CSV into it. It writes landos.* ONLY. It NEVER touches public.acquisition_leads
— sync_to_crm remains the sole CRM writer, and it re-derives phone_1/phone_2
from this meta under its own DNC + callable gate (not is_dnc AND
skip_trace_status in (traced_web,traced_paid) AND confidence_score >= 0.6).
Writing phones to the CRM directly would be a parallel writer AND would be
NULLed on the next sync — so we don't.

FIREWALL / RAILS (JOB 5 directive):
  * scrub-PASS ONLY — the staged CSV carries the ccT3 DNC scrub columns
    (scrub_status/scrub_flags/scrubbed_at). We consume scrub_status='clean'
    rows and DROP every 'dnc' row. Phones are not callable until scrubbed, and
    a national/state-DNC number never enters the spine.
  * consent surface — a lead whose CRM row is dnc=true is excluded even if its
    numbers scrubbed clean (belt and suspenders over the scrub).
  * never overwrite — a lead that already carries skip_trace_meta.phones is
    left untouched; we only fill phoneless leads.
  * source-stamped — every landed number and the meta provenance carry
    source='tracerfy_20260713'.
  * tenant-qualified + idempotent — keyed on owner_leads.lead_id; a re-run fills
    only still-phoneless leads and re-stamps provenance in place (converges).
    Within the CSV, (target_key, normalized number) de-dupes.

KEY BRIDGE: the staged target_key IS public.acquisition_leads.lead_id (the
skip-trace targets were exported from the CRM board, source_kind
acq_lead_distress). The spine row is acquisition_leads.source_lead_id ->
landos.owner_leads.lead_id. Targets with no source_lead_id (CRM-native leads)
are a named skip — no spine row to write.

TRACERFY ToS (pinned): traced phones are INTERNAL-USE-ONLY — for our own
outreach on these leads. They are NEVER exported, resold, or shared to a third
party. Retention follows the lead; a lead purge removes them.
"""
import argparse
import csv
import json
import os
import sys
import uuid
from collections import defaultdict
from typing import Any, Dict, List

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.run_manifest import sha256_file, git_commit_info, utc_now, insert_run_log
from scripts.import_resolved import norm_phone   # reuse the spine normalizer

__VERSION__ = "1.0.0"

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
SOURCE_STAMP = "tracerfy_20260713"
STAGED_CSV = os.path.expanduser(
    "~/neuruh-repos/NeuruhAI/landos-ingest/data/strategy/skiptrace/staged/skiptrace_phones.csv")

# Per-phone confidence for a PAID, DNC-scrubbed Tracerfy trace. Documented, not
# fabricated per-lead: a paid verified trace is callable-grade (clears
# sync_to_crm THRESHOLD 0.6); mobile ranks above landline for reachability so
# the sync's confidence-desc sort puts the mobile in phone_1. The Tracerfy CSV
# carries no per-number confidence, so this mapping is the intake's convention.
CONFIDENCE_BY_TYPE = {"mobile": 0.80, "landline": 0.70}
DEFAULT_CONFIDENCE = 0.70
SYNC_THRESHOLD = 0.6   # mirror scripts/sync_to_crm.THRESHOLD (projection gate)


# ---- pure helpers -----------------------------------------------------------

def norm_type(raw: str | None) -> str | None:
    t = (raw or "").strip().lower()
    return t if t in CONFIDENCE_BY_TYPE else (t or None)


def confidence_for(ptype: str | None) -> float:
    return CONFIDENCE_BY_TYPE.get((ptype or "").strip().lower(), DEFAULT_CONFIDENCE)


def is_scrub_pass(row: Dict[str, str]) -> bool:
    """scrub-PASS = the ccT3 scrub ran AND marked the number clean. Anything not
    exactly 'clean' (dnc, blank, unknown) is dropped — fail-closed."""
    return (row.get("scrub_status") or "").strip().lower() == "clean"


def build_phones(rows: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """A target's scrub-PASS rows -> the phones[] shape sync_to_crm consumes,
    de-duped on normalized number, sorted mobile/high-confidence first."""
    seen: set = set()
    phones: List[Dict[str, Any]] = []
    for r in rows:
        num = norm_phone(r.get("phone"))
        if not num or num in seen:
            continue
        seen.add(num)
        ptype = norm_type(r.get("phone_type"))
        phones.append({
            "number": num,
            "type": ptype,
            "confidence": confidence_for(ptype),
            "source": SOURCE_STAMP,
            "verified_2x": False,          # single source (Tracerfy) — not cross-verified
        })
    phones.sort(key=lambda p: -(p["confidence"] or 0))
    return phones


def load_staged(csv_path: str):
    """Read the staged CSV; return (clean_by_target, stats). scrub-PASS filter
    is applied here and counted so the dry-run proves it."""
    with open(csv_path, newline="") as f:
        all_rows = list(csv.DictReader(f))
    clean = [r for r in all_rows if is_scrub_pass(r)]
    dropped = [r for r in all_rows if not is_scrub_pass(r)]
    by_target: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in clean:
        by_target[(r.get("target_key") or "").strip()].append(r)
    stats = {
        "csv_rows": len(all_rows),
        "scrub_pass_rows": len(clean),
        "scrub_dropped_rows": len(dropped),
        "dropped_flags": _count(dropped, "scrub_flags"),
        "clean_targets": len(by_target),
    }
    return by_target, stats


def _count(rows, col):
    out: Dict[str, int] = {}
    for r in rows:
        k = (r.get(col) or "(none)").strip() or "(none)"
        out[k] = out.get(k, 0) + 1
    return out


# ---- the intake -------------------------------------------------------------

BRIDGE_SQL = """
    select a.lead_id::text          as target_key,
           a.source_lead_id::text   as owner_lead_id,
           a.dnc                     as acq_dnc,
           ol.lead_id is not null    as has_spine_row,
           coalesce(jsonb_array_length(ol.skip_trace_meta->'phones'), 0) as existing_phones,
           ol.skip_trace_meta        as meta
    from public.acquisition_leads a
    left join landos.owner_leads ol
      on ol.lead_id = a.source_lead_id and ol.tenant_id = %(tenant)s
    where a.lead_id::text = any(%(keys)s)
"""

WRITE_SQL = """
    update landos.owner_leads
       set skip_trace_meta = %(meta)s,
           confidence_score = %(conf)s,
           skip_trace_status = 'traced_paid',
           updated_at = now()
     where tenant_id = %(tenant)s
       and lead_id = %(owner_lead_id)s
       and coalesce(jsonb_array_length(skip_trace_meta->'phones'), 0) = 0
"""


def run(csv_path: str, dry_run: bool = True, limit: int | None = None) -> Dict[str, Any]:
    run_id = str(uuid.uuid4())
    started = utc_now()
    csv_sha = sha256_file(csv_path)
    by_target, stats = load_staged(csv_path)

    targets = sorted(by_target)
    if limit:
        targets = targets[:limit]

    outcome = {"landed": 0, "skip_unmapped": 0, "skip_no_spine": 0,
               "skip_dnc": 0, "skip_has_phones": 0, "phones_written": 0}
    rows_out = []

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(BRIDGE_SQL, {"tenant": TENANT, "keys": targets})
        bridge = {r["target_key"]: r for r in cur.fetchall()}

        for tk in targets:
            b = bridge.get(tk)
            phones = build_phones(by_target[tk])
            if b is None:
                outcome["skip_unmapped"] += 1
                rows_out.append((tk, "UNMAPPED", None, len(phones)))
                continue
            if not b["owner_lead_id"] or not b["has_spine_row"]:
                outcome["skip_no_spine"] += 1
                rows_out.append((tk, "NO_SPINE_ROW", None, len(phones)))
                continue
            if b["acq_dnc"]:
                outcome["skip_dnc"] += 1
                rows_out.append((tk, "DNC_CONSENT", b["owner_lead_id"], len(phones)))
                continue
            if b["existing_phones"] > 0:
                outcome["skip_has_phones"] += 1
                rows_out.append((tk, "HAS_PHONES", b["owner_lead_id"], len(phones)))
                continue
            if not phones:
                outcome["skip_unmapped"] += 1
                rows_out.append((tk, "NO_CLEAN_PHONE", b["owner_lead_id"], 0))
                continue

            top_conf = max(p["confidence"] for p in phones)
            # MERGE: preserve any prior meta keys (registry pierce etc.); only add ours.
            meta = dict(b["meta"] or {})
            meta.update({
                "phones": phones,
                "dnc_status": "clear",             # scrub-PASS clears national/state DNC
                "verified_via": ["tracerfy"],
                "resolved_at": started.date().isoformat(),
                "import_provenance": {
                    "run_id": run_id,
                    "imported_at": started.isoformat().replace("+00:00", "Z"),
                    "source": SOURCE_STAMP,
                    "csv_file": os.path.basename(csv_path),
                    "csv_sha256": csv_sha,
                    "scrub": "ccT3 national/state DNC scrub, PASS-only",
                    "tos": "Tracerfy internal-use-only; never exported/resold",
                    "status_set": "traced_paid",
                },
            })
            outcome["landed"] += 1
            outcome["phones_written"] += len(phones)
            rows_out.append((tk, "LAND", b["owner_lead_id"], len(phones)))
            if not dry_run:
                cur.execute(WRITE_SQL, {"meta": Jsonb(meta), "conf": top_conf,
                                        "tenant": TENANT, "owner_lead_id": b["owner_lead_id"]})
                if cur.rowcount != 1:
                    raise RuntimeError(
                        f"expected to write 1 owner_lead for target {tk} "
                        f"(->{b['owner_lead_id']}), got {cur.rowcount} — a phone may "
                        f"have appeared since bridge read; aborting for safety")

        if not dry_run and outcome["landed"]:
            manifest = {
                "run_id": run_id,
                "generated_at_utc": started.isoformat().replace("+00:00", "Z"),
                "kind": "intake_tracerfy_phones",
                "county_fips": "*",
                "git_commit": git_commit_info(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                ).get("git_commit"),
                "scorer_version": f"intake_tracerfy_phones/{__VERSION__}",
                "source": SOURCE_STAMP,
                "csv_file": os.path.basename(csv_path),
                "csv_sha256": csv_sha,
                "row_count": outcome["landed"],
                "outcome": outcome,
                "stats": stats,
            }
            os.makedirs("data", exist_ok=True)
            mpath = os.path.join(
                "data", f"intake_tracerfy_phones_{started.strftime('%Y%m%dT%H%M%SZ')}.manifest.json")
            with open(mpath, "w") as f:
                json.dump(manifest, f, indent=2)
            insert_run_log(cur, TENANT, manifest, mpath)
            outcome["manifest"] = mpath

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

    _print_receipt(csv_path, csv_sha, stats, outcome, rows_out, dry_run, limit)
    return {"dry_run": dry_run, "stats": stats, "outcome": outcome}


def _print_receipt(csv_path, csv_sha, stats, outcome, rows_out, dry_run, limit):
    mode = "DRY-RUN (no writes)" if dry_run else "APPLY"
    print(f"\n{'='*72}\nTRACERFY PHONE INTAKE — {mode}\n{'='*72}")
    print(f"csv={os.path.basename(csv_path)}  sha256={csv_sha[:16]}…"
          + (f"  limit={limit}" if limit else ""))
    print("\nSCRUB-PASS FILTER (proof):")
    print(f"  csv_rows={stats['csv_rows']}  scrub_PASS(clean)={stats['scrub_pass_rows']}  "
          f"DROPPED(dnc/other)={stats['scrub_dropped_rows']}")
    print(f"  dropped by flag: {stats['dropped_flags']}")
    print(f"  clean targets: {stats['clean_targets']}")
    print("\nINTAKE OUTCOME:")
    print(f"  LAND (phoneless spine leads filled): {outcome['landed']}  "
          f"(+{outcome['phones_written']} numbers)")
    print(f"  skip unmapped/no-clean-phone: {outcome['skip_unmapped']}  "
          f"no-spine-row: {outcome['skip_no_spine']}  "
          f"DNC-consent: {outcome['skip_dnc']}  already-has-phones: {outcome['skip_has_phones']}")
    print("\n  target_key  verdict        owner_lead   #phones")
    for tk, verdict, ol, n in rows_out[:25]:
        print(f"  {tk[:8]}…  {verdict:<13}  {(ol[:8]+'…') if ol else '-':<11}  {n}")
    if len(rows_out) > 25:
        print(f"  … +{len(rows_out) - 25} more")

    if dry_run:
        print("\nNO writes (rolled back). To land on the spine, the operator runs:")
        print("\n  ┌─ GATED OPERATOR FIRE (phones → spine, then CRM projection) ───────────")
        print("  │ cd ~/neuruh-repos/NeuruhAI/landos-engine")
        print("  │ export $(grep -E '^DATABASE_URL=' .env | head -1)")
        print("  │ .venv/bin/python -m scripts.intake_tracerfy_phones --apply")
        print("  │   # then project to the CRM board (sole CRM writer, DNC+callable gated):")
        print("  │ .venv/bin/python -m scripts.sync_to_crm")
        print("  └───────────────────────────────────────────────────────────────────────")
        print(f"  Effect: {outcome['landed']} leads -> skip_trace_status=traced_paid, "
              f"phones callable (conf ≥ {SYNC_THRESHOLD}).")


def main() -> None:
    ap = argparse.ArgumentParser(description="Land DNC-scrubbed Tracerfy phones on the spine")
    ap.add_argument("--csv", default=STAGED_CSV, help="staged phones CSV")
    ap.add_argument("--apply", action="store_true", help="write (default is dry-run)")
    ap.add_argument("--limit", type=int, help="only the first N targets (dry-run proof)")
    a = ap.parse_args()
    if not os.path.exists(a.csv):
        sys.exit(f"no such staged CSV: {a.csv}")
    run(a.csv, dry_run=not a.apply, limit=a.limit)


if __name__ == "__main__":
    main()

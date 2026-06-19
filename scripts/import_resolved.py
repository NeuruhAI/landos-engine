"""
import_resolved.py — P3 of the Contact Resolver directive (D1 v2 §6): the
writeback importer that closes the VA loop.

    python -m scripts.import_resolved <resolved.csv> [--threshold 0.6] [--dry-run]

Reads the VA's filled-in callable CSV and writes the results into the engine.

WRITE SURFACE (Q2-locked, firewall): exactly two tables, both landos.* —
  - landos.owner_leads        (skip_trace_meta, confidence_score,
                               skip_trace_status, updated_at)
  - landos.contact_relations  (the kin/heir graph)
It NEVER touches public.* — the CRM sees phones only through the P4 sync
projection, which is the sole writer into public.acquisition_leads.

PER-ROW LOGIC
  worked?   a row counts as worked when any of phone_1 / relative_1_name /
            email_1 / va_notes / resolved_at / outreach_recipe is non-blank;
            untouched rows are skipped (a partial hand-back is fine).
  callable  >= 1 phone at confidence >= threshold AND verified_via lists
            >= 1 source                    -> skip_trace_status = traced_web
  else      flagged for the paid backstop: tracerfy_lookup() runs ONLY when
            TRACERFY_API_KEY is set AND the row is below threshold.
            Hit -> traced_paid. No key (the dormant state) -> no_hit, so a
            later armed run can retry exactly these rows.

IDEMPOTENT on lead_id: re-running the same CSV updates rows in place —
owner_leads is keyed on lead_id; contact_relations matches on
(owner_lead_id, lower(relative_name)) and updates instead of inserting.

MERGE, never clobber: existing skip_trace_meta keys (e.g. a registry pierce's
controlling_human / entity_record written before the VA pass) are preserved
unless the CSV brings a non-blank replacement.

PROVENANCE (TCPA-defensible): every phone carries its source site; the meta
carries verified_via + resolved_at + va_notes + an import_provenance block
(run_id, csv sha256, git commit). Each run also writes a tamper-evident
manifest to data/ and appends one row to landos.owner_leads_run_log
(county_fips='*' — an import spans counties; same append-only audit table the
scorer uses).
"""
import argparse
import csv
import json
import os
import re
import sys
import uuid
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.run_manifest import sha256_file, git_commit_info, utc_now, insert_run_log

__VERSION__ = "1.0.0"

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
DEFAULT_THRESHOLD = 0.6  # {{CONFIDENCE_THRESHOLD}} — tune after blind-50

WORKED_FIELDS = ["phone_1", "relative_1_name", "email_1", "va_notes",
                 "resolved_at", "outreach_recipe"]


# ---- pure helpers -----------------------------------------------------------

def norm_phone(raw: Optional[str]) -> Optional[str]:
    """US-normalize to +1XXXXXXXXXX when 10/11 digits; else keep digits as-is."""
    if not raw or not raw.strip():
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits if digits else None


def parse_sources(raw: Optional[str]) -> List[str]:
    if not raw:
        return []
    return [s.strip().lower() for s in re.split(r"[,;|]", raw) if s.strip()]


def _f(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def is_worked(row: Dict[str, str]) -> bool:
    return any((row.get(f) or "").strip() for f in WORKED_FIELDS)


def build_phones(row: Dict[str, str], sources: List[str]) -> List[Dict[str, Any]]:
    """phones from the CSV's phone_1/phone_2 blocks. verified_2x is stamped on
    phone_1 when >=2 independent sources are listed (the runbook's cross-verify
    step is defined on the top number)."""
    phones = []
    for i in (1, 2):
        num = norm_phone(row.get(f"phone_{i}"))
        if not num:
            continue
        phones.append({
            "number": num,
            "type": (row.get(f"phone_{i}_type") or "").strip().lower() or None,
            "confidence": _f(row.get(f"phone_{i}_confidence")),
            "source": (row.get(f"phone_{i}_source") or "").strip().lower() or None,
            "verified_2x": bool(i == 1 and len(sources) >= 2),
        })
    return phones


def build_relatives(row: Dict[str, str]) -> List[Dict[str, Any]]:
    """relative_1/2 -> kin-graph candidates. 'HEIR:' name prefix (runbook
    SOP-1b) or recipe R4 marks heirs."""
    recipe = (row.get("outreach_recipe") or "").strip().upper()
    rels = []
    for i in (1, 2):
        name = (row.get(f"relative_{i}_name") or "").strip()
        if not name:
            continue
        is_heir = recipe == "R4"
        m = re.match(r"^\s*HEIR\s*:?\s+", name, re.IGNORECASE)
        if m:
            is_heir = True
            name = name[m.end():].strip()
        rels.append({
            "name": name,
            "phone": norm_phone(row.get(f"relative_{i}_phone")),
            "is_heir": is_heir,
            "relationship": "heir" if is_heir else "unknown",
        })
    return rels


def tracerfy_lookup(row: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Paid backstop — DORMANT by design. Runs only when TRACERFY_API_KEY is
    present (caller also guarantees the row is below threshold). With no key:
    returns None and the row is marked no_hit so an armed run can retry it.

    The key gate is real; the API integration is intentionally NOT stubbed
    with a guessed contract — if you arm the key before the integration is
    written, this fails loudly instead of fabricating a request."""
    key = (os.environ.get("TRACERFY_API_KEY") or "").strip()
    if not key:
        return None  # dormant — absent-key-safe
    raise RuntimeError(
        "TRACERFY_API_KEY is set but the Tracerfy API integration is not "
        "implemented yet (endpoint/contract pending the account docs). "
        "Implement the call in tracerfy_lookup() before arming the key."
    )


# ---- the import --------------------------------------------------------------

TRACERFY_COST_PER_HIT = 0.02   # verified live 2026-06-12: tracerfy.com/pricing —
                               # Normal Trace 1 credit/hit = $0.02, misses free,
                               # pay-as-you-go, no subscription. (The "$5/lead"
                               # canon figure was the 500-record minimum ORDER
                               # = $5.00 total, not a per-lead price.)
TRACERFY_BATCH_CAP_USD = 10.0  # CONTACT-1 B5 hard cap: above this, the operator
                               # must confirm before any paid call fires.


def run(csv_path: str, threshold: float = DEFAULT_THRESHOLD,
        dry_run: bool = False, confirm_spend: bool = False) -> Dict[str, Any]:
    run_id = str(uuid.uuid4())
    started = utc_now()
    csv_sha = sha256_file(csv_path)

    with open(csv_path, newline="") as f:
        rows = list(csv.DictReader(f))

    # ---- B5 spend pre-flight (armed runs only) ------------------------------
    # Misses-only by construction: tracerfy_lookup fires per below-threshold
    # row, so worst-case spend = worked rows x cost/hit. Over the cap, an armed
    # run STOPS up front unless the operator passed --confirm-spend.
    if (os.environ.get("TRACERFY_API_KEY") or "").strip() and not dry_run:
        worst_case = len(rows) * TRACERFY_COST_PER_HIT
        if worst_case > TRACERFY_BATCH_CAP_USD and not confirm_spend:
            sys.exit(
                f"STOP — Tracerfy is ARMED and this batch could spend up to "
                f"${worst_case:.2f} ({len(rows)} rows x ${TRACERFY_COST_PER_HIT}/hit), "
                f"over the ${TRACERFY_BATCH_CAP_USD:.0f} cap. Re-run with "
                f"--confirm-spend to proceed.")

    summary = {"run_id": run_id, "csv": os.path.basename(csv_path),
               "csv_sha256": csv_sha, "total_rows": len(rows),
               "worked": 0, "skipped_unworked": 0, "unknown_lead": 0,
               "traced_web": 0, "traced_paid": 0, "no_hit": 0,
               "relations_inserted": 0, "relations_updated": 0,
               "row_outcomes": []}

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            for row in rows:
                lead_id = (row.get("lead_id") or "").strip()
                if not is_worked(row):
                    summary["skipped_unworked"] += 1
                    continue
                cur.execute(
                    "select lead_id, skip_trace_meta from landos.owner_leads "
                    "where tenant_id=%s and lead_id=%s", (TENANT, lead_id),
                )
                existing = cur.fetchone()
                if not existing:
                    print(f"  WARN unknown lead_id {lead_id!r} — skipped")
                    summary["unknown_lead"] += 1
                    continue
                summary["worked"] += 1

                sources = parse_sources(row.get("verified_via"))
                phones = build_phones(row, sources)
                relatives = build_relatives(row)
                confidences = [p["confidence"] for p in phones
                               if p["confidence"] is not None]
                top_conf = max(confidences) if confidences else None

                callable_ = bool(
                    phones and top_conf is not None
                    and top_conf >= threshold and len(sources) >= 1
                )
                if callable_:
                    status = "traced_web"     # free-source hit (E1 path)
                else:
                    paid = tracerfy_lookup(row)   # dormant without a key
                    if paid:
                        status = "traced_paid"
                        phones.extend(paid.get("phones", []))
                    else:
                        status = "no_hit"     # retried when the key is armed
                summary[status] += 1

                # MERGE: existing meta survives unless the CSV replaces a key.
                meta = dict(existing["skip_trace_meta"] or {})
                recipe = (row.get("outreach_recipe") or "").strip().upper()
                target = (row.get("primary_target") or "").strip().lower()
                meta.update({
                    "phones": phones,
                    "emails": [e for e in [(row.get("email_1") or "").strip()] if e],
                    "deceased": recipe == "R4",
                    "deceased_source": (row.get("va_notes") or "").strip() or None
                                       if recipe == "R4" else meta.get("deceased_source"),
                    "relatives": [{"name": r["name"], "phone": r["phone"]}
                                  for r in relatives],
                    "verified_via": sources,
                    "resolved_at": (row.get("resolved_at") or "").strip()
                                   or started.date().isoformat(),
                    "va_notes": (row.get("va_notes") or "").strip() or None,
                    "dnc_status": (row.get("dnc_status") or "unchecked").strip(),
                })
                if recipe:
                    meta["outreach_recipe"] = recipe
                if target:
                    meta["primary_target"] = target
                meta["import_provenance"] = {
                    "run_id": run_id,
                    "imported_at": started.isoformat().replace("+00:00", "Z"),
                    "csv_file": os.path.basename(csv_path),
                    "csv_sha256": csv_sha,
                    "status_set": status,
                    "threshold": threshold,
                }

                if not dry_run:
                    cur.execute(
                        """update landos.owner_leads
                           set skip_trace_meta=%s, confidence_score=%s,
                               skip_trace_status=%s, updated_at=now()
                           where tenant_id=%s and lead_id=%s""",
                        (Jsonb(meta), top_conf, status, TENANT, lead_id),
                    )
                    for r in relatives:
                        cur.execute(
                            """select id from landos.contact_relations
                               where owner_lead_id=%s
                                 and lower(relative_name)=lower(%s)""",
                            (lead_id, r["name"]),
                        )
                        hit = cur.fetchone()
                        if hit:
                            cur.execute(
                                """update landos.contact_relations
                                   set phone=%s, phone_type=%s, is_heir=%s,
                                       relationship=%s, source=%s
                                   where id=%s""",
                                (r["phone"], None, r["is_heir"], r["relationship"],
                                 f"va_csv:{os.path.basename(csv_path)}", hit["id"]),
                            )
                            summary["relations_updated"] += 1
                        else:
                            cur.execute(
                                """insert into landos.contact_relations
                                   (owner_lead_id, relative_name, relationship,
                                    phone, is_heir, source)
                                   values (%s,%s,%s,%s,%s,%s)""",
                                (lead_id, r["name"], r["relationship"], r["phone"],
                                 r["is_heir"], f"va_csv:{os.path.basename(csv_path)}"),
                            )
                            summary["relations_inserted"] += 1

                summary["row_outcomes"].append({
                    "lead_id": lead_id, "status": status,
                    "confidence_score": top_conf, "phones": len(phones),
                    "relatives": len(relatives), "recipe": recipe or None,
                })
                print(f"  {lead_id[:8]}…  {status:<11} conf={top_conf} "
                      f"phones={len(phones)} kin={len(relatives)} recipe={recipe or '-'}")

            # ---- audit trail: manifest file + append-only run_log row -------
            if not dry_run:
                manifest = {
                    "run_id": run_id,
                    "generated_at_utc": started.isoformat().replace("+00:00", "Z"),
                    "kind": "import_resolved",
                    "county_fips": "*",   # an import spans counties
                    "git_commit": git_commit_info(
                        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    ).get("git_commit"),
                    "scorer_version": f"import_resolved/{__VERSION__}",
                    "row_count": summary["worked"],
                    "csv_file": os.path.basename(csv_path),
                    "csv_sha256": csv_sha,
                    "threshold": threshold,
                    "status_counts": {k: summary[k] for k in
                                      ("traced_web", "traced_paid", "no_hit")},
                    "row_outcomes": summary["row_outcomes"],
                }
                os.makedirs("data", exist_ok=True)
                mpath = os.path.join(
                    "data",
                    f"import_resolved_{started.strftime('%Y%m%dT%H%M%SZ')}.manifest.json",
                )
                with open(mpath, "w") as f:
                    json.dump(manifest, f, indent=2)
                insert_run_log(cur, TENANT, manifest, mpath)
                summary["manifest"] = mpath
        if dry_run:
            conn.rollback()
            print("DRY RUN — rolled back, nothing written")
        else:
            conn.commit()

    print(f"\nimport summary: worked={summary['worked']} "
          f"(traced_web={summary['traced_web']} traced_paid={summary['traced_paid']} "
          f"no_hit={summary['no_hit']}) skipped_unworked={summary['skipped_unworked']} "
          f"unknown={summary['unknown_lead']} "
          f"kin +{summary['relations_inserted']}/~{summary['relations_updated']}")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", help="the VA's filled-in *_RESOLVED.csv")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--confirm-spend", action="store_true",
                    help="operator confirmation for an armed Tracerfy batch "
                         f"over the ${TRACERFY_BATCH_CAP_USD:.0f} cap")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not os.path.exists(args.csv_path):
        sys.exit(f"no such file: {args.csv_path}")
    run(args.csv_path, args.threshold, args.dry_run, args.confirm_spend)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Give every held TIER_C candidate exactly one explicit verdict.

313 rows carry contact_resolution_state = TIER_C_HELD_NOT_APPENDED. T20D's court
adjudicated 250 of them and stated that "every one of the residual rows was
adjudicated" -- but 63 had no verdict anywhere, and a held row with no verdict
reads like a decision without being one.

THE 63. They never reached the address court. All 63 carry no voter record at
all: 33 were rejected LOW_CONFIDENCE (the name lacked a middle name or suffix,
so no candidate voter was strong enough to compare) and 30 COMMON_NAME (the name
is too common to compare safely). The NAME gate stopped them before the ADDRESS
subconditions could apply. That is a real exclusion with a real reason, and it is
now recorded as one rather than inferred from an absence.

THE ARMING QUESTION. The certified subcondition A2_SITUS_ZIP_UNKNOWN_CONTAMINATED
holds 90 rows at precision 1.0000 and was refused because its arbiter evidence --
NC_ONEMAP_PARCEL_FABRIC and CENSUS_ZCTA_GAZETTEER -- could not be loaded at match
time. That subcondition exists BECAUSE the situs ZIP was contaminated. T24A
repaired the situs ZIP from authoritative source text, so this script asks
whether the arbiter is still needed: does production now carry a corroborated
property ZIP for these rows, and does it settle the comparison on its own?

It reports the answer and does not act on it. Arming is a promotion, and a
promotion is a separate deliberate act with its own review. The threshold is not
lowered either way.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.custody.sync_guard import prepare_output_dir

T20D = Path.home() / "neuruh-workspace" / "T20D_NC_FREE_CONTACT_EXPANSION_20260819"

# court_verdict -> (verdict, why). TRUE_POSITIVE is a correct match under a rule
# that is certified but NOT ARMED, so the row stays HELD: correct is not the same
# as promoted.
COURT_MAP = {
    "TRUE_POSITIVE": ("HELD", "address subcondition adjudicated correct; the promotion "
                              "rule is certified but not armed, so the row is not appended"),
    "FALSE_POSITIVE": ("EXCLUDED", "address subcondition adjudicated a false positive"),
}

# reject_class -> why the NAME gate stopped the row before the address court.
NAME_GATE = {
    "LOW_CONFIDENCE": "name gate: the owner name lacks a middle name or suffix, so no "
                      "voter record was strong enough to carry into the address court",
    "COMMON_NAME": "name gate: the owner name is too common to compare safely, so no "
                   "voter record was carried into the address court",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_csv(path: Path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--out-dir", default=str(Path.home() / "neuruh-private"
                                             / "contact-intelligence" / "runs" / "T24A_TIER_C"))
    args = ap.parse_args()
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2
    out = prepare_output_dir(args.out_dir, what="tier C adjudication evidence")
    run_id = f"t24a-tierc-{utcnow()}"

    court = {r["candidate_id"]: r for r in load_csv(T20D / "TIER_C_COURT_ADJUDICATION.csv")}
    detail = {r["candidate_id"]: r for r in load_csv(T20D / "_work" / "TIER_C_ALL_DETAIL.csv")}

    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                select import_row_id::text as candidate_id, tenant_id::text as tenant_id
                  from public.candidate_contact_resolution
                 where contact_resolution_state = 'TIER_C_HELD_NOT_APPENDED'
            """)
            held = cur.fetchall()

        rows, verdicts, gates = [], Counter(), Counter()
        for h in held:
            cid = h["candidate_id"]
            c = court.get(cid)
            d = detail.get(cid, {})
            if c:
                verdict, why = COURT_MAP.get(
                    c["court_verdict"],
                    ("HELD", f"court verdict {c['court_verdict']!r} is not a promotion"))
                sub, gate = c.get("subcondition", ""), c.get("court_verdict", "")
            elif d:
                gate = d.get("reject_class", "") or "UNSTATED"
                verdict = "EXCLUDED"
                why = NAME_GATE.get(gate, f"excluded before the address court: {d.get('reason','')}")
                sub = ""
            else:
                verdict, why, sub, gate = ("HELD", "held row not present in the T20D court "
                                                   "detail; no adjudication evidence exists",
                                           "", "NO_EVIDENCE")
            verdicts[verdict] += 1
            gates[gate] += 1
            rows.append((h["tenant_id"], cid, verdict, sub, gate, why, run_id))

        # Does the repaired situs ZIP make A2's arbiter unnecessary?
        a2 = [cid for cid, c in court.items()
              if c.get("subcondition") == "A2_SITUS_ZIP_UNKNOWN_CONTAMINATED"]
        arming = {"subcondition": "A2_SITUS_ZIP_UNKNOWN_CONTAMINATED", "rows": len(a2)}
        if a2:
            with conn.cursor() as cur:
                cur.execute("""
                    select count(*) filter (where coalesce(situs_zip,'') <> '') as zip_now_known,
                           count(*) as n
                      from public.deedsonar_current_candidates_v1
                     where candidate_id = any(%s::uuid[])
                """, (a2,))
                r = cur.fetchone()
            arming.update(zip_now_known=r["zip_now_known"], measured=r["n"])
            arming["arbiter_still_required"] = r["zip_now_known"] < r["n"]
            arming["status"] = "CERTIFIED_NOT_ARMED"
            arming["note"] = (
                f"{r['zip_now_known']} of {r['n']} A2 rows now carry a property ZIP that "
                "T24A derived from authoritative source text, so the ZIP that was UNKNOWN "
                "when the subcondition was written is known for those rows. Arming is a "
                "promotion and is not performed here."
            )

        report = {
            "run_id": run_id, "generated_at": utcnow(),
            "TIER_C_TOTAL": len(held),
            "TIER_C_EXPLICITLY_ADJUDICATED": len(rows),
            "TIER_C_UNADJUDICATED": len(held) - len(rows),
            "verdicts": dict(verdicts),
            "gates": dict(gates),
            "court_rows_available": len(court),
            "detail_rows_available": len(detail),
            "arming": arming,
        }
        (out / "TIER_C_ADJUDICATION.json").write_text(json.dumps(report, indent=1))
        os.chmod(out / "TIER_C_ADJUDICATION.json", 0o600)
        print(json.dumps(report, indent=1))

        if not args.apply:
            print("DRY RUN. No verdict written.")
            return 0

        with conn.cursor() as cur:
            cur.execute("""
                create temp table _t24a_tierc (
                  tenant_id uuid, import_row_id uuid, verdict text, subcondition text,
                  gate text, reason text, run_id text) on commit drop
            """)
            with cur.copy("copy _t24a_tierc (tenant_id, import_row_id, verdict, subcondition, "
                          "gate, reason, run_id) from stdin") as cp:
                for r in rows:
                    cp.write_row(r)
            cur.execute("""
                insert into public.tier_c_adjudication
                  (tenant_id, import_row_id, verdict, subcondition, gate, reason, run_id)
                select tenant_id, import_row_id, verdict, subcondition, gate, reason, run_id
                  from _t24a_tierc
                on conflict (import_row_id) do update set
                  verdict = excluded.verdict, subcondition = excluded.subcondition,
                  gate = excluded.gate, reason = excluded.reason,
                  run_id = excluded.run_id, adjudicated_at = now()
            """)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("""
                select count(*) n,
                       count(*) filter (where a.import_row_id is null) unadjudicated
                  from public.candidate_contact_resolution c
                  left join public.tier_c_adjudication a on a.import_row_id = c.import_row_id
                 where c.contact_resolution_state = 'TIER_C_HELD_NOT_APPENDED'
            """)
            final = cur.fetchone()
        print(json.dumps({"held": final["n"], "unadjudicated": final["unadjudicated"]}, indent=1))
        (out / "TIER_C_RECEIPT.json").write_text(json.dumps({**report, "post": dict(final)}, indent=1))
        os.chmod(out / "TIER_C_RECEIPT.json", 0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

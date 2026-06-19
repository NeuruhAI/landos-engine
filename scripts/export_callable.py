"""
export_callable.py — P2 of the Contact Resolver directive (D1 v2): the VA's working file.

READ-ONLY against the DB. Regenerates the callable export split ENGINE-SIDE:

    exports/callable_prep_individuals_and_entities_119_<YYYY-MM-DD>.csv
    exports/callable_review_govt_17_<YYYY-MM-DD>.csv

Layout = the CRM-side prep split's 14 base columns (unchanged, same order, same
sop_path/govt_flag values — continuity with the file the VA has already seen),
PLUS the v1 §3 contact-fill block the VA fills during resolution, PLUS the two
v2 §5 columns the VA stamps as the resolver's last step:

    outreach_recipe  (R1–R5)   — blank; decided by liveness + ownership form
    primary_target   (owner|member|trustee|heirs|PR) — blank; who to actually call

Routing source of truth = the engine's stored owner_type (the §3 12-token regex
is RETIRED per v2 §2):  individual → SOP-1-direct, entity → SOP-2-pierce,
govt → govt-review (carved into the review file, not the callable 119).

dnc_status defaults to 'unchecked' — a row is NOT dialed until a fresh scrub
flips it to 'clear' immediately before dialing (compliance gate, see VA_RUNBOOK).

Counts are HARD-ASSERTED (119 = 38 individual + 81 entity; 17 govt) and the
script exits 1 on any mismatch — the P0 ground truth must hold or we stop.

    python -m scripts.export_callable
"""
import csv
import os
import sys
from datetime import date

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
QUALIFIED_WHERE = "tenant_id = %s and not discovery_seed"

EXPECT = {"individual": 38, "entity": 81, "govt": 17}

# 14 base columns — byte-identical to the CRM-side prep split header.
BASE_COLS = [
    "lead_id", "owner_name", "owner_type", "owner_mailing_address",
    "owner_mailing_city", "owner_mailing_state", "owner_mailing_zip",
    "situs_address", "county_fips", "parcel_apn", "final_lead_score",
    "skip_trace_status", "govt_flag", "sop_path",
]
# v1 §3 contact-fill block — the VA writes into these.
FILL_COLS = [
    "phone_1", "phone_1_type", "phone_1_confidence", "phone_1_source",
    "phone_2", "phone_2_type", "phone_2_confidence", "phone_2_source",
    "relative_1_name", "relative_1_phone", "relative_2_name", "relative_2_phone",
    "email_1", "verified_via", "resolved_at", "va_notes", "dnc_status",
]
# v2 §5 — the resolver's last step; blank until the VA stamps them.
RECIPE_COLS = ["outreach_recipe", "primary_target"]

ALL_COLS = BASE_COLS + FILL_COLS + RECIPE_COLS

SOP_PATH = {"individual": "SOP-1-direct", "entity": "SOP-2-pierce", "govt": "govt-review"}


def fetch_rows() -> list:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            f"""select lead_id, owner_name, owner_type, owner_mailing_address,
                       owner_mailing_city, owner_mailing_state, owner_mailing_zip,
                       situs_address, county_fips, parcel_apn, final_lead_score,
                       skip_trace_status
                from landos.owner_leads
                where {QUALIFIED_WHERE}
                order by final_lead_score desc nulls last, owner_name, lead_id""",
            (TENANT,),
        )
        return cur.fetchall()


def to_csv_row(r: dict) -> dict:
    out = {c: "" for c in ALL_COLS}
    for c in BASE_COLS:
        if c in r:
            out[c] = r[c] if r[c] is not None else ""
    out["govt_flag"] = r["owner_type"] == "govt"
    out["sop_path"] = SOP_PATH.get(r["owner_type"], "")
    out["dnc_status"] = "unchecked"   # never dial until a fresh scrub says 'clear'
    return out


def write_csv(path: str, rows: list) -> None:
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=ALL_COLS)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    rows = fetch_rows()
    by_type = {}
    for r in rows:
        by_type.setdefault(r["owner_type"], []).append(r)

    failures = []
    for t, n in EXPECT.items():
        got = len(by_type.get(t, []))
        if got != n:
            failures.append(f"{t}: expected {n}, got {got}")
    if failures:
        print("COUNT ASSERTION FAIL — ground truth moved, stopping:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)

    callable_rows = [to_csv_row(r) for r in by_type["individual"] + by_type["entity"]]
    callable_rows.sort(key=lambda r: (-(r["final_lead_score"] or 0), r["owner_name"]))
    govt_rows = [to_csv_row(r) for r in by_type["govt"]]
    govt_rows.sort(key=lambda r: (-(r["final_lead_score"] or 0), r["owner_name"]))

    os.makedirs("exports", exist_ok=True)
    stamp = date.today().isoformat()
    p1 = f"exports/callable_prep_individuals_and_entities_119_{stamp}.csv"
    p2 = f"exports/callable_review_govt_17_{stamp}.csv"
    write_csv(p1, callable_rows)
    write_csv(p2, govt_rows)

    print(f"counts: individual={len(by_type['individual'])} entity={len(by_type['entity'])} "
          f"govt={len(by_type['govt'])}  -> 119/17 PASS")
    print(f"wrote {p1} ({len(callable_rows)} rows)")
    print(f"wrote {p2} ({len(govt_rows)} rows)")
    print(f"columns ({len(ALL_COLS)}): {','.join(ALL_COLS)}")


if __name__ == "__main__":
    main()

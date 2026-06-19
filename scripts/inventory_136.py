"""
inventory_136.py — P0 of the Contact Resolver directive (D1): prove the 136.

READ-ONLY. Runs four SELECTs against landos.owner_leads and prints the results
with a PASS/FAIL verdict per check. Writes nothing, anywhere.

What it proves (the D1 §1 ground truth, against the live DB):
  1. RAW        — total rows for the tenant                          → expect 286
  2. QUALIFIED  — raw minus discovery_seed bootstrap leads           → expect 136
                  This is the EXACT live-queue exclusion the scorer applies
                  (scripts/owner_leads.py:225:
                      live = [r for r in scored if not r["discovery_seed"]]
                  ), expressed in SQL as: not discovery_seed
  3. TRACE      — skip_trace_status breakdown; every QUALIFIED row   → expect 136
                  must be 'not_traced' (phones are the only gap)
  4. SPLIT      — person/entity ratio of the qualified set, by the D1 §3
                  entity-token regex, cross-checked against the engine's stored
                  owner_type (written by app/score_absentee.classify_owner_type)

Any mismatch prints the discrepancy and exits 1 — the gate fails loudly.

    python -m scripts.inventory_136
"""
import re
import sys

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

# Same tenant constant the scorer uses (scripts/owner_leads.py).
TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

EXPECT_RAW = 286
EXPECT_QUALIFIED = 136

# D1 §3 entity tokens, word-bounded so 'CO' matches 'SMITH CO' but not 'COLEMAN'.
ENTITY_TOKENS = (
    "LLC|INC|CORP|CO|LP|LLP|TRUST|FOUNDATION|ASSOCIATES|PROPERTIES|HOLDINGS|GROUP"
)
ENTITY_TOKEN_RE = re.compile(r"\b(" + ENTITY_TOKENS + r")\b", re.IGNORECASE)

# The qualified filter — the scorer's discovery_seed exclusion, in SQL.
QUALIFIED_WHERE = "tenant_id = %s and not discovery_seed"
QUALIFIED_SQL = (
    "select count(*) as n from landos.owner_leads where " + QUALIFIED_WHERE
)


def main() -> None:
    failures = []

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            # 1 — RAW count
            cur.execute(
                "select count(*) as n from landos.owner_leads where tenant_id = %s",
                (TENANT,),
            )
            raw = cur.fetchone()["n"]

            # 2 — QUALIFIED count (the scorer's exact exclusion)
            cur.execute(QUALIFIED_SQL, (TENANT,))
            qualified = cur.fetchone()["n"]

            # 3 — skip_trace_status breakdown, raw and qualified
            cur.execute(
                "select skip_trace_status, count(*) as n from landos.owner_leads "
                "where tenant_id = %s group by 1 order by 1",
                (TENANT,),
            )
            trace_raw = cur.fetchall()
            cur.execute(
                "select skip_trace_status, count(*) as n from landos.owner_leads "
                "where " + QUALIFIED_WHERE + " group by 1 order by 1",
                (TENANT,),
            )
            trace_qual = cur.fetchall()

            # 4 — owner names for the regex split + stored owner_type cross-check
            cur.execute(
                "select owner_name from landos.owner_leads where " + QUALIFIED_WHERE,
                (TENANT,),
            )
            qual_names = [r["owner_name"] or "" for r in cur.fetchall()]
            cur.execute(
                "select owner_name from landos.owner_leads where tenant_id = %s",
                (TENANT,),
            )
            raw_names = [r["owner_name"] or "" for r in cur.fetchall()]
            cur.execute(
                "select owner_type, count(*) as n from landos.owner_leads "
                "where " + QUALIFIED_WHERE + " group by 1 order by 1",
                (TENANT,),
            )
            stored_type = cur.fetchall()

    # ---- 1: RAW ------------------------------------------------------------
    ok = raw == EXPECT_RAW
    if not ok:
        failures.append(f"raw count {raw} != expected {EXPECT_RAW}")
    print(f"[1] RAW rows (tenant {TENANT}): {raw}  "
          f"(expect {EXPECT_RAW}) {'PASS' if ok else 'FAIL'}")

    # ---- 2: QUALIFIED --------------------------------------------------------
    ok = qualified == EXPECT_QUALIFIED
    if not ok:
        failures.append(f"qualified count {qualified} != expected {EXPECT_QUALIFIED}")
    print(f"\n[2] QUALIFIED rows: {qualified}  "
          f"(expect {EXPECT_QUALIFIED}) {'PASS' if ok else 'FAIL'}")
    print(f"    filter SQL: {QUALIFIED_SQL % ('%s',)}")
    print("    source:     scripts/owner_leads.py:225 — "
          'live = [r for r in scored if not r["discovery_seed"]]')
    print(f"    excluded (discovery_seed): {raw - qualified}")

    # ---- 3: skip_trace_status -----------------------------------------------
    print("\n[3] skip_trace_status breakdown")
    print("    raw (all rows):")
    for r in trace_raw:
        print(f"      {r['skip_trace_status'] or 'NULL':<12} {r['n']}")
    print("    qualified only:")
    for r in trace_qual:
        print(f"      {r['skip_trace_status'] or 'NULL':<12} {r['n']}")
    not_traced_qual = sum(
        r["n"] for r in trace_qual if r["skip_trace_status"] == "not_traced"
    )
    ok = not_traced_qual == qualified
    if not ok:
        failures.append(
            f"qualified not_traced {not_traced_qual} != qualified {qualified} "
            f"(some rows already traced?)"
        )
    print(f"    qualified not_traced: {not_traced_qual}/{qualified} "
          f"{'PASS' if ok else 'FAIL'}")

    # ---- 4: person/entity split ----------------------------------------------
    q_entity = sum(1 for n in qual_names if ENTITY_TOKEN_RE.search(n))
    q_person = len(qual_names) - q_entity
    r_entity = sum(1 for n in raw_names if ENTITY_TOKEN_RE.search(n))
    r_person = len(raw_names) - r_entity
    print("\n[4] person/entity split — D1 §3 entity-token regex")
    print(f"    regex: \\b({ENTITY_TOKENS})\\b  (case-insensitive)")
    print(f"    qualified ({len(qual_names)}): person={q_person}  entity={q_entity}")
    print(f"    raw       ({len(raw_names)}): person={r_person}  entity={r_entity}")
    print("    cross-check — stored owner_type on qualified "
          "(engine classifier, app/score_absentee.py):")
    for r in stored_type:
        print(f"      {r['owner_type'] or 'NULL':<12} {r['n']}")

    # ---- verdict ---------------------------------------------------------------
    print()
    if failures:
        print("GATE P0: FAIL — discrepancies:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"GATE P0: PASS — {raw} raw / {qualified} qualified / "
          f"{not_traced_qual} not_traced; split printed above.")


if __name__ == "__main__":
    main()

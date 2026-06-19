"""
stamp_r5_govt.py — structural R5 recipe stamp for government-owned rows.

    python -m scripts.stamp_r5_govt

Canon (VA_RUNBOOK §2): "R5 is reserved for government/land-bank rows" — the
recipe for a government owner is decided by OWNERSHIP FORM alone, which the
engine already knows (owner_type='govt', the §3 classifier). Unlike R1 (which
asserts a liveness check + direct line) there is nothing for a resolver to
learn first, so stamping R5 engine-side fabricates nothing: official channels
only, never cold-dialed, primary_target = the public body itself.

WRITE SURFACE: landos.owner_leads.skip_trace_meta ONLY. MERGE, never clobber —
rows that already carry ANY outreach_recipe are skipped (a resolver's stamp
outranks a structural one). Idempotent: re-runs find nothing to do.

Provenance per stamp: source, basis, stamped_at.
"""
import sys

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.run_manifest import utc_now

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def main() -> int:
    stamped_at = utc_now().isoformat().replace("+00:00", "Z")
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """select lead_id, owner_name, skip_trace_meta
               from landos.owner_leads
               where tenant_id=%s and not discovery_seed and owner_type='govt'""",
            (TENANT,))
        rows = cur.fetchall()
        stamped = skipped = 0
        for r in rows:
            meta = r["skip_trace_meta"] or {}
            if meta.get("outreach_recipe"):
                skipped += 1
                continue
            merged = {**meta,
                      "outreach_recipe": "R5",
                      "primary_target": "owner",
                      "recipe_provenance": {
                          "source": "engine owner_type classifier (structural stamp)",
                          "basis": "owner_type=govt -> R5 per VA_RUNBOOK §2 / D1 v2 canon; "
                                   "official channels only, never cold-dialed",
                          "stamped_at": stamped_at,
                      }}
            cur.execute(
                """update landos.owner_leads set skip_trace_meta=%s, updated_at=now()
                   where tenant_id=%s and lead_id=%s""",
                (Jsonb(merged), TENANT, r["lead_id"]))
            stamped += 1
            print(f"  R5 -> {r['owner_name']}")
        conn.commit()
    print(f"\ngovt rows: {len(rows)}; stamped R5: {stamped}; "
          f"already-stamped skipped: {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

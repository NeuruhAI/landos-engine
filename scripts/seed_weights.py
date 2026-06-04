"""
seed_weights.py — insert the default weights for a tenant's scoring PROFILE.

The scoring service reads its weights from landos.scoring_weights so they can be
retrained later. This script puts a profile's starting weights in place and marks
them active. Weights are versioned + active per (tenant, profile), so seeding the
'benton_harbor_edge' profile never disturbs the 'default' model.

Usage (from project root, venv active):
    python -m scripts.seed_weights <TENANT_ID> [profile]

Examples:
    python -m scripts.seed_weights 1840ac90-54bd-4b3d-8baf-227c8d84ab5a
    python -m scripts.seed_weights 1840ac90-54bd-4b3d-8baf-227c8d84ab5a benton_harbor_edge
"""
import json
import sys

import psycopg

from app.config import DATABASE_URL
from app.scoring import DEFAULT_WEIGHTS, EDGE_DEFAULT_WEIGHTS, EDGE_PROFILE

# profile name -> (weights, seed note)
PROFILES = {
    "default": (DEFAULT_WEIGHTS, "Default distress/owner/arbitrage v1"),
    EDGE_PROFILE: (
        EDGE_DEFAULT_WEIGHTS,
        "benton_harbor_edge base weights (0.30/0.25/0.25/0.20; tax_delinquent near-zero)",
    ),
}


def main(tenant_id: str, profile: str = "default") -> None:
    if profile not in PROFILES:
        print(f"Unknown profile '{profile}'. Known: {', '.join(PROFILES)}")
        raise SystemExit(1)
    weights, note = PROFILES[profile]
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as cur:
            # Deactivate any previously-active set for THIS tenant+profile only.
            cur.execute(
                "update landos.scoring_weights set active = false "
                "where tenant_id = %s and profile = %s",
                (tenant_id, profile),
            )
            # Next version number for this tenant+profile.
            cur.execute(
                "select coalesce(max(version), 0) + 1 from landos.scoring_weights "
                "where tenant_id = %s and profile = %s",
                (tenant_id, profile),
            )
            version = cur.fetchone()[0]
            cur.execute(
                """
                insert into landos.scoring_weights
                    (tenant_id, profile, version, weights, active, notes)
                values (%s, %s, %s, %s, true, %s)
                """,
                (tenant_id, profile, version, json.dumps(weights), note),
            )
        conn.commit()
    print(f"Seeded '{profile}' weights v{version} (active) for tenant {tenant_id}")


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print("Usage: python -m scripts.seed_weights <TENANT_ID> [profile]")
        raise SystemExit(1)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) == 3 else "default")

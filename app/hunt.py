"""
hunt.py — HUNT-V2 §5: the CLI demo. One line proposes a territory hunt.

    python -m app.hunt "Travis County, TX" --signals taxsale
    python -m app.hunt "Mecosta County, MI" "Bay County, MI" --run

Creates the job through the SAME code path as POST /hunt (fips-resolved,
never guessed), prints it, and with --run advances it inline via the worker
(census -> stage -> score -> DRY-RUN mint -> awaiting_operator_mint | uncovered).
The mint is NOT here: that is the operator's typed-GO command,
python -m app.hunt_mint <job_id>.
"""
import argparse
import json
import sys

import psycopg
from psycopg.rows import dict_row

from .config import DATABASE_URL
from .fips import parse_territory_string
from . import hunt_core, hunt_worker


def main(argv=None):
    ap = argparse.ArgumentParser(description="HUNT-V2 CLI — propose a territory hunt")
    ap.add_argument("territory", nargs="+", help="'County Name, ST' (repeatable)")
    ap.add_argument("--signals", default="taxsale",
                    help="comma-separated signal types (default: taxsale)")
    ap.add_argument("--requested-by", default="cli")
    ap.add_argument("--run", action="store_true",
                    help="advance the job inline (worker pass) after creating it")
    a = ap.parse_args(argv)

    try:
        resolved = [parse_territory_string(t) for t in a.territory]
    except ValueError as e:
        raise SystemExit(f"territory error: {e}")
    recipe = {"signal_types": [s.strip() for s in a.signals.split(",") if s.strip()],
              "thresholds": {}}

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        job = hunt_core.create_job(cur, resolved, recipe, a.requested_by)
        conn.commit()
    print(f"hunt job {job['id']} QUEUED")
    print(json.dumps({"territory": resolved, "recipe": recipe}, indent=2))

    if a.run:
        rc = hunt_worker.main(job_id=str(job["id"]))
        print(f"\nstatus check: GET /hunt/{job['id']}  (or psql landos.hunt_jobs)")
        return rc
    print(f"\nadvance it: python -m app.hunt_worker --job {job['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Read-only request adapter for sole-writer visibility maintenance.

This module intentionally contains no database client, transaction, or SQL mutation.
The only executable CRM mutation surface is ``scripts.sync_to_crm``.
"""
from __future__ import annotations

import argparse
import json

from app.score_absentee import cold_call_lens

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
NONHUMAN_LENS_PIPELINE = {"institution": "entity", "govt": "govt"}


def requested_pipeline(owner_name: str, current_pipeline: str | None) -> str | None:
    """Pure planner used by the sole writer; never moves a seller queue assignment."""
    lens = cold_call_lens(owner_name)
    if lens == "seller":
        return "land" if current_pipeline is None else current_pipeline
    return NONHUMAN_LENS_PIPELINE[lens]


def build_request(tenant_id: str = TENANT) -> dict[str, object]:
    return {
        "schema": "landos.crm_maintenance_request.v1",
        "action": "visibility_backfill",
        "tenant_id": tenant_id,
        "apply": False,
        "writer": "scripts/sync_to_crm.py",
        "authority_required": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="emit a read-only visibility request")
    parser.add_argument("--backfill", action="store_true", required=True)
    parser.add_argument("--dry-run", action="store_true", help="retained compatibility flag")
    parser.parse_args()
    print(json.dumps(build_request(), sort_keys=True))
    print("REQUEST ONLY — no database connection or CRM write. Route through sync_to_crm.")


if __name__ == "__main__":
    main()

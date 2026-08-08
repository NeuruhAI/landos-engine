"""Read-only request adapter for restricted tax-sale retirement.

The legacy ``--apply`` spelling is rejected. CRM mutation belongs exclusively to
``scripts.sync_to_crm`` and requires its typed authority receipt.
"""
from __future__ import annotations

import argparse
import json

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def build_request(tenant_id: str = TENANT) -> dict[str, object]:
    return {
        "schema": "landos.crm_maintenance_request.v1",
        "action": "retire_restricted_taxsale",
        "tenant_id": tenant_id,
        "apply": False,
        "writer": "scripts/sync_to_crm.py",
        "authority_required": True,
        "selection": {
            "source_type": "taxsale",
            "source_page_contains": "tax-sale.info",
            "promoted": False,
        },
    }


def run(apply: bool = False) -> dict[str, object]:
    """Compatibility API: return a request, never execute the legacy mutation."""
    if apply:
        raise RuntimeError("apply removed: route the request through scripts.sync_to_crm")
    return build_request()


def main() -> None:
    parser = argparse.ArgumentParser(description="emit a read-only retirement request")
    parser.add_argument("--apply", action="store_true", help="legacy spelling; always blocked")
    args = parser.parse_args()
    if args.apply:
        parser.error("--apply removed: route a typed request through scripts.sync_to_crm")
    print(json.dumps(run(), sort_keys=True))
    print("REQUEST ONLY — no database connection or CRM write.")


if __name__ == "__main__":
    main()

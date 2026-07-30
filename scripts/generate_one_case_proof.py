#!/usr/bin/env python3
"""Generate deterministic SYN-BLACK-001 locally with no external side effects."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.closed_loop import CASE_ID, write_synthetic_one_case_proof

DEFAULT_OUTPUT = ROOT / "data" / "one-case-proof" / CASE_ID


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    bundle = write_synthetic_one_case_proof(args.output, overwrite=args.overwrite)
    manifest = bundle["one-case-proof-manifest-v1.json"]
    print(
        "ONE_CASE_PROOF_OK "
        f"run_id={CASE_ID} artifacts={len(bundle)} "
        f"chain_sha256={manifest['chain_sha256']} "
        "production_writes=0 external_actions=0",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

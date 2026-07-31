#!/usr/bin/env python3
"""Run the pinned BLACK court acceptance suite fully offline."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType


REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = REPO_ROOT / "contracts" / "black-v1" / "acceptance.py"


def _load_harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location("landos_black_acceptance", HARNESS_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load BLACK acceptance harness: {HARNESS_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit a machine-readable result array instead of text lines",
    )
    args = parser.parse_args(argv)
    harness = _load_harness()
    try:
        results = harness.run_acceptance_cases()
    except Exception as exc:
        print(f"FAIL BLACK-SOURCE-PIN {exc}")
        print("BLACK_ACCEPTANCE FAIL passed=0 failed=1")
        return 1
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "id": result.acceptance_id,
                        "status": result.status,
                        "detail": result.detail,
                    }
                    for result in results
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
        )
    else:
        for result in results:
            print(f"{result.status} {result.acceptance_id} {result.detail}")
    failures = [result for result in results if result.status != "PASS"]
    print(
        f"BLACK_ACCEPTANCE {'PASS' if not failures else 'FAIL'} "
        f"passed={len(results) - len(failures)} failed={len(failures)}",
        file=sys.stderr if args.json else sys.stdout,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

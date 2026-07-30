#!/usr/bin/env python3
"""Generate or validate a file-only UnderwritingResultV1 fixture."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.underwriting import (
    UnderwritingProductionError,
    UnderwritingResultV1,
    produce_underwriting_result,
    verify_receipt,
)
from app.underwriting.canonical import canonical_json


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("input JSON must be an object")
    return value


def _atomic_write(path: Path, text: str, *, overwrite: bool) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite {path}; pass --overwrite")
    fd, temp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def parser() -> argparse.ArgumentParser:
    out = argparse.ArgumentParser(
        description=(
            "Produce a deterministic fixture artifact. No database/network writes exist "
            "in this command."
        )
    )
    out.add_argument("--input", type=Path, help="UnderwritingCaseV1 JSON")
    out.add_argument("--output", type=Path, help="artifact path; stdout if omitted")
    out.add_argument("--overwrite", action="store_true")
    out.add_argument(
        "--expect-failure",
        action="store_true",
        help="succeed only when the input is rejected fail-closed",
    )
    out.add_argument(
        "--schema",
        choices=("input", "result"),
        help="print a JSON Schema and do not generate",
    )
    return out


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.schema:
        from app.underwriting.models import UnderwritingCaseV1

        model = (
            UnderwritingCaseV1
            if args.schema == "input"
            else UnderwritingResultV1
        )
        print(canonical_json(model.model_json_schema()))
        return 0
    if args.input is None:
        parser().error("--input is required unless --schema is used")
    try:
        raw = _read_json(args.input)
        result = produce_underwriting_result(raw)
    except (UnderwritingProductionError, ValueError) as exc:
        if args.expect_failure:
            print(f"EXPECTED_REJECTION: {exc}", file=sys.stderr)
            return 0
        print(f"UNDERWRITING_REJECTED: {exc}", file=sys.stderr)
        return 2
    if args.expect_failure:
        print("EXPECTED_REJECTION_MISSING: fixture unexpectedly passed", file=sys.stderr)
        return 3
    if not verify_receipt(result):
        print("RECEIPT_INVALID", file=sys.stderr)
        return 4
    output = canonical_json(result.model_dump(mode="json"))
    if args.output:
        _atomic_write(args.output, output, overwrite=args.overwrite)
    else:
        print(output)
    print(
        f"UNDERWRITING_FIXTURE_OK sha256={result.receipt.payload_sha256}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Static validation for the uninstalled LandOS LaunchAgent package."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import plistlib
import sys


CANONICAL_ROOT = Path("/Users/jeramiehicks/neuruh-repos/NeuruhAI/landos-engine")
CANONICAL_PYTHON = CANONICAL_ROOT / ".venv/bin/python"
CANONICAL_RUNNER = CANONICAL_ROOT / "deploy/landos_orchestrator_runner.py"
LOG_ROOT = Path("/Users/jeramiehicks/Library/Logs/Neuruh/LandOS")
EXPECTED_LABEL = "com.neuruh.landos.orchestrator"
FORBIDDEN_KEY_FRAGMENTS = ("secret", "token", "password", "credential", "key")


def validate(package_root: Path) -> list[str]:
    problems: list[str] = []
    plist_path = package_root / "deploy/com.neuruh.landos.orchestrator.plist"
    runner_path = package_root / "deploy/landos_orchestrator_runner.py"
    if not plist_path.is_file():
        return [f"plist missing: {plist_path}"]
    if not runner_path.is_file():
        problems.append(f"runner missing: {runner_path}")
    if not CANONICAL_PYTHON.is_file() or not os.access(CANONICAL_PYTHON, os.X_OK):
        problems.append(f"controlled Python is not executable: {CANONICAL_PYTHON}")

    try:
        with plist_path.open("rb") as handle:
            config = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        return [f"invalid plist: {exc}"]

    expected_args = [str(CANONICAL_PYTHON), str(CANONICAL_RUNNER), "--dry-run"]
    if config.get("Label") != EXPECTED_LABEL:
        problems.append("unexpected Label")
    if config.get("ProgramArguments") != expected_args:
        problems.append("ProgramArguments must use controlled Python, absolute runner, dry-run")
    if config.get("WorkingDirectory") != str(CANONICAL_ROOT):
        problems.append("WorkingDirectory must be the absolute canonical engine root")
    if config.get("RunAtLoad") is not False:
        problems.append("RunAtLoad must be false")
    if config.get("KeepAlive") is not False:
        problems.append("KeepAlive must be false")

    for key in ("StandardOutPath", "StandardErrorPath"):
        value = config.get(key)
        if not isinstance(value, str) or not Path(value).is_absolute():
            problems.append(f"{key} must be an absolute path")
        elif Path(value) != Path("/dev/null") and not Path(value).is_relative_to(LOG_ROOT):
            problems.append(f"{key} must be /dev/null or outside Git under {LOG_ROOT}")
        elif Path(value).is_relative_to(CANONICAL_ROOT):
            problems.append(f"{key} must not be under the Git checkout")

    arguments = config.get("ProgramArguments", [])
    if any(not Path(item).is_absolute() for item in arguments[:2]):
        problems.append("executable and runner paths must be absolute")
    if "--live" in arguments:
        problems.append("scheduler package must not enable live mode")
    if "--dry-run" not in arguments:
        problems.append("scheduler package must explicitly select dry-run")
    if "EnvironmentVariables" in config:
        problems.append("EnvironmentVariables are forbidden; scheduler carries no secrets")
    for key in config:
        lowered = str(key).lower()
        if any(fragment in lowered for fragment in FORBIDDEN_KEY_FRAGMENTS):
            problems.append(f"suspicious secret-bearing plist key: {key}")

    if runner_path.is_file():
        source = runner_path.read_text(encoding="utf-8")
        if "LOCK_NB" not in source:
            problems.append("runner lacks nonblocking overlap lock")
        if "RotatingFileHandler" not in source:
            problems.append("runner lacks bounded rotating logs")
        if '"--live"' in source or "'--live'" in source:
            problems.append("runner must not expose a live-mode path")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--package-root",
        type=Path,
        default=CANONICAL_ROOT,
        help="source tree containing deploy/ (canonical root by default)",
    )
    args = parser.parse_args(argv)
    root = args.package_root.resolve()
    problems = validate(root)
    if problems:
        for problem in problems:
            print(f"INVALID: {problem}", file=sys.stderr)
        return 1
    print(f"VALID: {root / 'deploy/com.neuruh.landos.orchestrator.plist'}")
    print("MODE: DRY-RUN ONLY")
    print("INSTALL/ACTIVATION: NOT PERFORMED")
    return 0


if __name__ == "__main__":
    sys.exit(main())

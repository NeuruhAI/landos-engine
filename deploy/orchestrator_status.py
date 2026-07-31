#!/usr/bin/env python3
"""Read-only scheduler package/install/runtime status (never calls launchctl)."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
import sys


PACKAGE = Path(
    "/Users/jeramiehicks/neuruh-repos/NeuruhAI/landos-engine/"
    "deploy/com.neuruh.landos.orchestrator.plist"
)
INSTALLED = Path(
    "/Users/jeramiehicks/Library/LaunchAgents/com.neuruh.landos.orchestrator.plist"
)
LOCK = Path(
    "/Users/jeramiehicks/Library/Application Support/Neuruh/LandOS/orchestrator.lock"
)
LOG = Path("/Users/jeramiehicks/Library/Logs/Neuruh/LandOS/orchestrator.log")


def _running() -> tuple[bool, str | None]:
    if not LOCK.exists():
        return False, None
    with LOCK.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            return True, handle.readline().strip() or None
        return False, None


def main() -> int:
    running, pid = _running()
    state = {
        "package_present": PACKAGE.is_file(),
        "installed_plist_present": INSTALLED.is_file(),
        "launchagent_loaded": "UNKNOWN_NOT_QUERIED",
        "runner_active": running,
        "runner_pid": pid,
        "runtime_log_present": LOG.is_file(),
        "runtime_log_bytes": LOG.stat().st_size if LOG.is_file() else 0,
        "mode": "dry-run-only",
    }
    print(json.dumps(state, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Fail-closed, dry-run-only launchd wrapper for the LandOS orchestrator.

The wrapper owns scheduler-level overlap prevention and bounded logs. It does
not contain a live-mode code path; later live activation must change the
orchestrator invocation through a separately reviewed package.
"""

from __future__ import annotations

import argparse
import fcntl
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys


ENGINE_ROOT = Path("/Users/jeramiehicks/neuruh-repos/NeuruhAI/landos-engine")
PYTHON = ENGINE_ROOT / ".venv/bin/python"
LOG_DIR = Path("/Users/jeramiehicks/Library/Logs/Neuruh/LandOS")
LOCK_FILE = Path(
    "/Users/jeramiehicks/Library/Application Support/Neuruh/LandOS/orchestrator.lock"
)
MAX_LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 4
LOCKED_EXIT = 75
CONFIG_EXIT = 78


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the canonical LandOS pipeline in dry-run mode"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicitly select the only scheduler-supported mode (default)",
    )
    parser.add_argument("--engine-root", type=_absolute_path, default=ENGINE_ROOT)
    parser.add_argument("--python", type=_absolute_path, default=PYTHON)
    parser.add_argument("--log-dir", type=_absolute_path, default=LOG_DIR)
    parser.add_argument("--lock-file", type=_absolute_path, default=LOCK_FILE)
    return parser


def _logger(log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(
        log_dir / "orchestrator.log",
        maxBytes=MAX_LOG_BYTES,
        backupCount=LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)sZ %(levelname)s %(message)s"))
    logger = logging.getLogger("landos.scheduler")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger


def _validate_runtime(engine_root: Path, python: Path) -> list[str]:
    problems: list[str] = []
    if not engine_root.is_dir():
        problems.append(f"engine root missing: {engine_root}")
    if not (engine_root / "scripts/orchestrate_pipeline.py").is_file():
        problems.append("canonical orchestrator module missing")
    if not python.is_file() or not os.access(python, os.X_OK):
        problems.append(f"controlled Python is not executable: {python}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    args.lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with args.lock_file.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return LOCKED_EXIT

        lock.seek(0)
        lock.truncate()
        lock.write(f"{os.getpid()}\n")
        lock.flush()
        os.fsync(lock.fileno())

        logger = _logger(args.log_dir)
        problems = _validate_runtime(args.engine_root, args.python)
        if problems:
            for problem in problems:
                logger.error("configuration failure: %s", problem)
            return CONFIG_EXIT

        command = [
            str(args.python),
            "-m",
            "scripts.orchestrate_pipeline",
            "--dry-run",
        ]
        logger.info(
            "scheduler_start pid=%s mode=dry-run engine_root=%s",
            os.getpid(),
            args.engine_root,
        )
        try:
            process = subprocess.Popen(
                command,
                cwd=args.engine_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=os.environ.copy(),
            )
            assert process.stdout is not None
            for line in process.stdout:
                logger.info("pipeline %s", line.rstrip())
            return_code = process.wait()
        except OSError as exc:
            logger.exception("scheduler_exec_failed error=%s", exc)
            return CONFIG_EXIT

        logger.info("scheduler_end pipeline_exit=%s", return_code)
        return return_code


if __name__ == "__main__":
    sys.exit(main())

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"
PLIST = DEPLOY / "com.neuruh.landos.orchestrator.plist"
RUNNER = DEPLOY / "landos_orchestrator_runner.py"
VALIDATOR = DEPLOY / "validate_orchestrator_package.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("landos_scheduler_runner", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fixture_engine(tmp_path: Path, sleep_seconds: float = 0) -> Path:
    engine = tmp_path / "engine"
    scripts = engine / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "__init__.py").write_text("", encoding="utf-8")
    (scripts / "orchestrate_pipeline.py").write_text(
        "import argparse,time\n"
        "p=argparse.ArgumentParser();p.add_argument('--dry-run',action='store_true')\n"
        "a=p.parse_args();assert a.dry_run\n"
        f"time.sleep({sleep_seconds!r})\n"
        "print('fixture dry-run complete')\n",
        encoding="utf-8",
    )
    return engine


def test_plist_is_dry_run_absolute_and_outside_git():
    with PLIST.open("rb") as handle:
        config = plistlib.load(handle)
    args = config["ProgramArguments"]
    assert all(Path(value).is_absolute() for value in args[:2])
    assert args[-1] == "--dry-run"
    assert "--live" not in args
    assert Path(config["WorkingDirectory"]).is_absolute()
    assert config["RunAtLoad"] is False
    assert config["KeepAlive"] is False
    assert "EnvironmentVariables" not in config
    for key in ("StandardOutPath", "StandardErrorPath"):
        path = Path(config[key])
        assert path.is_absolute()
        assert not path.is_relative_to(Path(config["WorkingDirectory"]))


def test_validator_accepts_source_package():
    completed = subprocess.run(
        [sys.executable, str(VALIDATOR), "--package-root", str(ROOT)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "MODE: DRY-RUN ONLY" in completed.stdout
    assert "NOT PERFORMED" in completed.stdout


def test_runner_defaults_to_dry_run_and_writes_bounded_log(tmp_path):
    module = _load_runner()
    engine = _fixture_engine(tmp_path)
    log_dir = tmp_path / "logs"
    result = module.main(
        [
            "--engine-root",
            str(engine),
            "--python",
            sys.executable,
            "--log-dir",
            str(log_dir),
            "--lock-file",
            str(tmp_path / "state/orchestrator.lock"),
        ]
    )
    assert result == 0
    log = (log_dir / "orchestrator.log").read_text(encoding="utf-8")
    assert "mode=dry-run" in log
    assert "fixture dry-run complete" in log
    assert module.MAX_LOG_BYTES == 5 * 1024 * 1024
    assert module.LOG_BACKUPS == 4


def test_runner_lock_rejects_overlap(tmp_path):
    engine = _fixture_engine(tmp_path, sleep_seconds=1.5)
    common = [
        str(RUNNER),
        "--engine-root",
        str(engine),
        "--python",
        sys.executable,
        "--log-dir",
        str(tmp_path / "logs"),
        "--lock-file",
        str(tmp_path / "state/orchestrator.lock"),
    ]
    first = subprocess.Popen([sys.executable, *common])
    lock_path = tmp_path / "state/orchestrator.lock"
    deadline = time.monotonic() + 3
    while (not lock_path.exists() or not lock_path.read_text().strip()) and time.monotonic() < deadline:
        time.sleep(0.02)
    second = subprocess.run([sys.executable, *common], check=False)
    assert second.returncode == 75
    assert first.wait(timeout=5) == 0


def test_install_uninstall_status_contracts_have_no_launchctl():
    install = (DEPLOY / "install_orchestrator.sh").read_text(encoding="utf-8")
    uninstall = (DEPLOY / "uninstall_orchestrator.sh").read_text(encoding="utf-8")
    status = (DEPLOY / "orchestrator_status.py").read_text(encoding="utf-8")
    assert install.index('"${CONTROLLED_PYTHON}" "${VALIDATOR}"') < install.index(
        "/usr/bin/install"
    )
    assert "--copy-only" in install
    assert "--confirm-not-loaded" in uninstall
    assert "ARCHIVED, NOT DELETED" in uninstall
    assert "launchagent_loaded" in status
    assert "UNKNOWN_NOT_QUERIED" in status
    for source in (install, uninstall, status):
        assert "/bin/launchctl" not in source
        assert "\nlaunchctl " not in source

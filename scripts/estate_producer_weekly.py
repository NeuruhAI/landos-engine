"""estate_producer_weekly.py — the scheduled consumer of the weekly source-factory run.

landos-ingest's source factory lands a run every Monday 08:45 local (launchd
com.neuruh.landos-ingest.source-factory-weekly). Nothing consumed it: the estate
producer ran only when someone ran it by hand, and a week without a hand run left
the estate a week staler than its own source. This is that consumer:

    launchd com.neuruh.landos-engine.estate-producer-weekly (Mon 09:30 local)
      -> python -m scripts.estate_producer_weekly run

  1. the newest factory run (run-YYYYMMDDTHHMMSSZ holding a run receipt); a run
     that already has a producer receipt is skipped (ALREADY_PRODUCED, exit 0);
  2. UNIVERSE_CURRENT.json names the ranking input behind the receipts production
     carries; its sha256 is verified or the run is refused;
  3. the recipe-engine and deedsonar inputs are DEDICATED checkouts, brought to
     their origin/main (detached) before the run and refused if dirty. The shared
     clones are not used: the recipe-engine runtime clone is dirtied by its own
     daily job, and the shared deedsonar clone carries whatever branch a lane left;
  4. estate_producer --universe into data/estate_producer/runs/<factory run id>/.

It WRITES NO DATABASE. The revise rows and the receipts projection it produces are
applied by the production promotion (a T5 / founder action). Only after production
carries the run does `promote-universe` advance UNIVERSE_CURRENT.json, so next
week's universe is always the one production ranks by, never a run nobody applied.

    python -m scripts.estate_producer_weekly run [--ingest-runs D] [--out-root D]
    python -m scripts.estate_producer_weekly promote-universe --producer-run <run dir>
    python -m scripts.estate_producer_weekly set-universe --csv <FULL_UNIVERSE_RANKING_INPUT.csv> \\
        --ranking-run-id <id>      (bootstrap: the universe behind receipts already live)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts import estate_producer as ep

HOME = Path.home()
DEFAULT_INGEST_RUNS = HOME / "neuruh-repos/NeuruhAI/landos-ingest/data/source_factory/runs"
DEFAULT_OUT_ROOT = ep.ENGINE_ROOT / "data" / "estate_producer"
DEFAULT_RECIPE_ENGINE = HOME / "neuruh-worktrees/estate-producer-recipe-engine"
DEFAULT_DEEDSONAR = HOME / "neuruh-worktrees/estate-producer-deedsonar"
POINTER_NAME = "UNIVERSE_CURRENT.json"
RUN_DIR_RE = re.compile(r"^run-\d{8}T\d{6}Z$")


def newest_factory_run(runs_root: Path) -> Path:
    runs = sorted(p for p in runs_root.iterdir()
                  if p.is_dir() and RUN_DIR_RE.match(p.name) and (p / "run_receipt.json").exists())
    if not runs:
        raise ep.ProducerError(f"no source-factory run with a receipt under {runs_root}")
    return runs[-1]


def already_produced(out_root: Path, factory_run_id: str) -> Path | None:
    for receipt in sorted((out_root / "runs").glob("*/PRODUCER_RECEIPT.json")):
        body = json.loads(receipt.read_text())
        if (body.get("factory_run") or {}).get("run_id") == factory_run_id:
            return receipt.parent
    return None


def read_universe(out_root: Path) -> dict[str, Any]:
    pointer = out_root / POINTER_NAME
    if not pointer.exists():
        raise ep.ProducerError(f"{pointer} missing: set-universe (bootstrap) or promote-universe first")
    body = json.loads(pointer.read_text())
    path = Path(body["path"])
    if not path.is_file():
        raise ep.ProducerError(f"universe {path} named by {pointer} does not exist")
    got = ep.sha256_file(path)
    if got != body.get("sha256"):
        raise ep.ProducerError(f"universe {path.name} sha256 {got[:16]} != pointer {str(body.get('sha256'))[:16]}")
    return body


def write_universe(out_root: Path, csv_path: Path, ranking_run_id: str, basis: str) -> dict[str, Any]:
    """Custody-copy the universe input under out_root/universe/ and point at it."""
    if not re.match(r"^ranking_run_\d{8}_[0-9a-f]{12}$", ranking_run_id):
        raise ep.ProducerError(f"not a ranking run id: {ranking_run_id!r}")
    digest = ep.sha256_file(csv_path)
    if not ranking_run_id.endswith(digest[:12]):
        raise ep.ProducerError(f"{ranking_run_id} was not ranked from {csv_path.name} (sha256 {digest[:12]})")
    custody = out_root / "universe" / f"{ranking_run_id}.csv"
    custody.parent.mkdir(parents=True, exist_ok=True)
    if not custody.exists():
        shutil.copyfile(csv_path, custody)
    if ep.sha256_file(custody) != digest:
        raise ep.ProducerError(f"custody copy {custody} does not match {csv_path}")
    body = {"path": str(custody), "sha256": digest, "ranking_run_id": ranking_run_id, "basis": basis,
            "set_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat()}
    (out_root / POINTER_NAME).write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")
    return body


def sync_checkout(path: Path) -> str:
    """A dedicated checkout, detached at its origin/main. Refused if absent or dirty."""
    def git(*args: str) -> str:
        proc = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ep.ProducerError(f"git {' '.join(args)} in {path}: {proc.stderr.strip()[-300:]}")
        return proc.stdout.strip()
    if not (path / ".git").exists():
        raise ep.ProducerError(f"{path} is not a checkout (git worktree add --detach {path} origin/main)")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ep.ProducerError(f"{path} has uncommitted changes; this job never runs dirty inputs")
    git("fetch", "--quiet", "origin", "main")
    git("checkout", "--quiet", "--detach", "origin/main")
    return git("rev-parse", "HEAD")


def run(args: argparse.Namespace) -> dict[str, Any]:
    factory = newest_factory_run(args.ingest_runs)
    done = already_produced(args.out_root, factory.name)
    if done is not None:
        return {"state": "ALREADY_PRODUCED", "factory_run": factory.name, "producer_run": str(done)}
    universe = read_universe(args.out_root)
    heads = {"recipe-engine": sync_checkout(args.recipe_engine), "deedsonar": sync_checkout(args.deedsonar)}
    recipe_python = next((str(p) for p in (args.recipe_engine / ".venv" / "bin" / "python",
                                           args.recipe_python_fallback) if p and Path(p).exists()), sys.executable)
    receipt = ep.produce(factory, args.out_root / "runs" / factory.name, recipe_engine=args.recipe_engine,
                         deedsonar=args.deedsonar, recipe_python=recipe_python, node=args.node,
                         mecklenburg_taxpayers=None, full_universe=False, allow_dirty=False,
                         universe=Path(universe["path"]))
    return {"state": "PRODUCED", "factory_run": factory.name, "producer_run_id": receipt["producer_run_id"],
            "prior_ranking_run_id": universe["ranking_run_id"],
            "ranking_run_id": receipt["receipts"]["ranking_run_id"], "checkouts": heads,
            "revise": receipt["revise"]["stats"],
            "receipt": str(args.out_root / "runs" / factory.name / "PRODUCER_RECEIPT.json"),
            "next": "production promotion (revise, then receipts) is a T5/founder action; "
                    "after it lands: promote-universe --producer-run <this run dir>"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--ingest-runs", type=Path, default=DEFAULT_INGEST_RUNS)
    r.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    r.add_argument("--recipe-engine", type=Path, default=Path(os.environ.get("ESTATE_PRODUCER_RECIPE_ENGINE",
                                                                             DEFAULT_RECIPE_ENGINE)))
    r.add_argument("--deedsonar", type=Path, default=Path(os.environ.get("ESTATE_PRODUCER_DEEDSONAR",
                                                                         DEFAULT_DEEDSONAR)))
    # the dedicated recipe-engine checkout has no venv of its own; the runtime clone's venv runs its code
    r.add_argument("--recipe-python-fallback", default=str(HOME / "neuruh-repos/NeuruhAI/recipe-engine/.venv/bin/python"))
    r.add_argument("--node", default=shutil.which("node") or "/opt/homebrew/bin/node")
    p = sub.add_parser("promote-universe")
    p.add_argument("--producer-run", required=True, type=Path)
    p.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    s = sub.add_parser("set-universe")
    s.add_argument("--csv", required=True, type=Path)
    s.add_argument("--ranking-run-id", required=True)
    s.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "run":
            out = run(args)
        elif args.cmd == "promote-universe":
            body = json.loads((args.producer_run / "PRODUCER_RECEIPT.json").read_text())
            if "receipts" not in body or "universe" not in body:
                raise ep.ProducerError(f"{args.producer_run} is not a full-universe producer run")
            out = {"state": "UNIVERSE_PROMOTED", **write_universe(
                args.out_root, Path(body["universe"]["output"]["path"]), body["receipts"]["ranking_run_id"],
                basis=f"producer run {body['producer_run_id']}")}
        else:
            out = {"state": "UNIVERSE_SET", **write_universe(args.out_root, args.csv, args.ranking_run_id,
                                                            basis="set-universe (bootstrap)")}
    except ep.ProducerError as exc:
        print(json.dumps({"state": "REFUSED", "reason": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

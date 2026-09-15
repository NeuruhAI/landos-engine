"""estate_producer_weekly: which factory run the schedule consumes, which universe
it ranks against, and when that universe may advance."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import estate_producer as ep
from scripts import estate_producer_weekly as wk


def _run_dir(root: Path, name: str, receipt: bool = True) -> Path:
    d = root / name
    d.mkdir(parents=True)
    if receipt:
        (d / "run_receipt.json").write_text("{}")
    return d


def test_the_newest_factory_run_with_a_receipt_is_the_one_consumed(tmp_path):
    _run_dir(tmp_path, "run-20260907T124500Z")
    _run_dir(tmp_path, "run-20260914T124500Z")
    _run_dir(tmp_path, "run-20260921T124500Z", receipt=False)      # still landing
    _run_dir(tmp_path, "run-t03-wake-live-20260925T203000Z")       # a hand-named lane run
    assert wk.newest_factory_run(tmp_path).name == "run-20260914T124500Z"
    with pytest.raises(ep.ProducerError, match="no source-factory run"):
        wk.newest_factory_run(_run_dir(tmp_path, "empty", receipt=False))


def test_a_factory_run_already_produced_is_found_by_its_run_id(tmp_path):
    out = tmp_path / "out"
    prod = out / "runs" / "run-20260914T124500Z"
    prod.mkdir(parents=True)
    (prod / "PRODUCER_RECEIPT.json").write_text(json.dumps({"factory_run": {"run_id": "run-20260914T124500Z"}}))
    assert wk.already_produced(out, "run-20260914T124500Z") == prod
    assert wk.already_produced(out, "run-20260921T124500Z") is None


def _universe_csv(tmp_path: Path) -> tuple[Path, str]:
    csv_path = tmp_path / "FULL_UNIVERSE_RANKING_INPUT.csv"
    csv_path.write_text("county_fips,parcel_apn\n37179,09222012\n")
    return csv_path, hashlib.sha256(csv_path.read_bytes()).hexdigest()


def test_the_universe_pointer_binds_the_run_id_to_the_file_it_was_ranked_from(tmp_path):
    csv_path, digest = _universe_csv(tmp_path)
    out = tmp_path / "out"
    with pytest.raises(ep.ProducerError, match="was not ranked from"):
        wk.write_universe(out, csv_path, "ranking_run_20260914_98c6e952f36d", basis="t")
    with pytest.raises(ep.ProducerError, match="not a ranking run id"):
        wk.write_universe(out, csv_path, "latest", basis="t")
    run_id = f"ranking_run_20260915_{digest[:12]}"
    body = wk.write_universe(out, csv_path, run_id, basis="t")
    assert Path(body["path"]) == out / "universe" / f"{run_id}.csv"      # custody copy, not the lane file
    assert wk.read_universe(out)["ranking_run_id"] == run_id
    Path(body["path"]).write_text("tampered\n")
    with pytest.raises(ep.ProducerError, match="sha256"):
        wk.read_universe(out)


def _args(tmp_path: Path, runs: Path) -> SimpleNamespace:
    return SimpleNamespace(ingest_runs=runs, out_root=tmp_path / "out", recipe_engine=tmp_path / "re",
                           deedsonar=tmp_path / "ds", recipe_python_fallback=None, node="node")


def test_without_a_universe_pointer_the_schedule_refuses_rather_than_rank_a_slice(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    _run_dir(runs, "run-20260914T124500Z")
    monkeypatch.setattr(wk, "sync_checkout", lambda p: pytest.fail("no checkout before the universe is known"))
    with pytest.raises(ep.ProducerError, match="UNIVERSE_CURRENT.json missing"):
        wk.run(_args(tmp_path, runs))


def test_a_produced_factory_run_is_not_produced_twice(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    _run_dir(runs, "run-20260914T124500Z")
    args = _args(tmp_path, runs)
    prod = args.out_root / "runs" / "run-20260914T124500Z"
    prod.mkdir(parents=True)
    (prod / "PRODUCER_RECEIPT.json").write_text(json.dumps({"factory_run": {"run_id": "run-20260914T124500Z"}}))
    monkeypatch.setattr(wk, "sync_checkout", lambda p: pytest.fail("skipped runs touch nothing"))
    monkeypatch.setattr(ep, "produce", lambda *a, **k: pytest.fail("skipped runs produce nothing"))
    assert wk.run(args)["state"] == "ALREADY_PRODUCED"


def test_the_schedule_ranks_against_the_pointer_universe_on_origin_main_checkouts(tmp_path, monkeypatch):
    runs = tmp_path / "runs"
    factory = _run_dir(runs, "run-20260914T124500Z")
    args = _args(tmp_path, runs)
    csv_path, digest = _universe_csv(tmp_path)
    wk.write_universe(args.out_root, csv_path, f"ranking_run_20260914_{digest[:12]}", basis="t")
    monkeypatch.setattr(wk, "sync_checkout", lambda p: f"head-of-{p.name}")
    seen = {}

    def produce(factory_run, output, **kw):
        seen.update(factory_run=factory_run, output=output, **kw)
        return {"producer_run_id": "estate-x", "receipts": {"ranking_run_id": "ranking_run_20260921_x"},
                "revise": {"stats": {"rows": 1}}}
    monkeypatch.setattr(ep, "produce", produce)
    got = wk.run(args)
    assert got["state"] == "PRODUCED" and got["prior_ranking_run_id"].endswith(digest[:12])
    assert seen["factory_run"] == factory and seen["output"] == args.out_root / "runs" / factory.name
    assert seen["universe"] == args.out_root / "universe" / f"ranking_run_20260914_{digest[:12]}.csv"
    assert seen["allow_dirty"] is False and seen["full_universe"] is False
    assert got["checkouts"] == {"recipe-engine": "head-of-re", "deedsonar": "head-of-ds"}


def test_promote_universe_refuses_a_partial_run_and_advances_on_a_full_one(tmp_path, capsys):
    out = tmp_path / "out"
    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "PRODUCER_RECEIPT.json").write_text(json.dumps({"producer_run_id": "p", "revise": {}}))
    assert wk.main(["promote-universe", "--producer-run", str(partial), "--out-root", str(out)]) == 2
    csv_path, digest = _universe_csv(tmp_path)
    full = tmp_path / "full"
    full.mkdir()
    (full / "PRODUCER_RECEIPT.json").write_text(json.dumps({
        "producer_run_id": "estate-full", "universe": {"output": {"path": str(csv_path)}},
        "receipts": {"ranking_run_id": f"ranking_run_20260915_{digest[:12]}"}}))
    assert wk.main(["promote-universe", "--producer-run", str(full), "--out-root", str(out)]) == 0
    assert wk.read_universe(out)["basis"] == "producer run estate-full"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def test_a_dirty_or_missing_checkout_is_refused(tmp_path):
    with pytest.raises(ep.ProducerError, match="not a checkout"):
        wk.sync_checkout(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "f.txt").write_text("a\n")
    _git(repo, "add", "f.txt")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c")
    (repo / "f.txt").write_text("b\n")
    with pytest.raises(ep.ProducerError, match="uncommitted changes"):
        wk.sync_checkout(repo)

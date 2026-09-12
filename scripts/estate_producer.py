"""estate_producer.py — one receipted pass from a source-factory run to the rows
DeedSonar's delta path takes (data_estate_revise_v1). It WRITES NO DATABASE.

Until now the chain from a landos-ingest source-factory run to the DeedSonar
estate was four CLIs run by hand in three repos, with no receipt tying one
step's output to the next step's input. This wrapper runs the EXISTING stages
unchanged, in order, and binds every artifact by sha256:

    0 verify   the factory run: receipt present, code CLEAN, JSONL digest ==
               the receipt's normalized_sha256
    1 enrich   landos-engine  scripts.build_nc_dps_public_enrichment_v3 (NC OneMap, network)
    2 rank     recipe-engine  scripts.rank_nc_curative_raw_v3
    3 export   landos-engine  scripts.export_opportunities (deedsonar_opportunity_csv.v1
               + facts_as_of / facts_as_of_basis)
    4 payload  deedsonar      scripts/build-data-estate-payload.mjs (reads facts_as_of only)
    5 revise   derive the data_estate_revise_v1 row array from the payload, in chunks

It stops at files. The revise call runs as service_role on the production
estate, and that is a T5 production action (never lvdi). It is never a
re-seal: open/append/seal would demote the current batch and orphan every
overlay keyed by import_row_id.

GLOBAL RANK IS WITHHELD BY DEFAULT. A factory run covers part of the estate
(Mecklenburg is not a factory source), and global_rank is a position in the
universe that was ranked. Revise UPDATE would overwrite estate-wide ranks
with ranks from that slice. Per-row score, tier, class and recipe do not
depend on the universe, so they pass. --full-universe keeps global_rank, and
the caller asserts the run covers the whole estate.

    python -m scripts.estate_producer --factory-run <landos-ingest run dir> \\
        --output <dir> --recipe-engine <path> --deedsonar <path> \\
        [--mecklenburg-taxpayers F] [--full-universe] [--allow-dirty]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PRODUCER_VERSION = "estate_producer.v1"
REVISE_SOURCE = "landos_estate_producer"
CHUNK_ROWS = 1000
ENGINE_ROOT = Path(__file__).resolve().parents[1]

CROSSWALK_HEADER = "property_key,reconciliation,action,prod_lead_id\n"
COORDS_HEADER = ("canonical_property_key,latitude,longitude,coordinate_class,coordinate_source,"
                 "coordinate_confidence,address_conflict\n")

# The row contract data_estate_revise_v1 reads (20260910170000 / 20260912120000).
REVISE_KEYS = ("property_key", "source_row_id", "county_fips", "parcel_apn", "candidate_class",
               "normalized", "source_as_of", "opportunity_score", "priority_tier",
               "primary_recipe", "signals", "recommended_action", "global_rank")


class ProducerError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_state(repo: Path) -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, check=False).stdout.strip()
    return {"path": str(repo), "sha": run("rev-parse", "HEAD") or None,
            "dirty": bool(run("status", "--porcelain", "--untracked-files=no"))}


def verify_factory_run(run_dir: Path, *, allow_dirty: bool) -> dict[str, Any]:
    receipt_path = run_dir / "run_receipt.json"
    jsonl = run_dir / "normalized_signals.jsonl"
    if not receipt_path.exists() or not jsonl.exists():
        raise ProducerError(f"{run_dir}: not a source-factory run (run_receipt.json + normalized_signals.jsonl)")
    receipt = json.loads(receipt_path.read_text())
    code = receipt.get("code_commit") or {}
    if code.get("state") != "CLEAN" and not allow_dirty:
        raise ProducerError(f"factory run code is {code.get('state')!r}, not CLEAN (--allow-dirty to override)")
    want = (receipt.get("rollup") or {}).get("normalized_sha256")
    got = sha256_file(jsonl)
    if want and want != got:
        raise ProducerError(f"normalized_signals.jsonl sha256 {got[:16]} != receipt {want[:16]}")
    return {"run_id": receipt.get("run_id"), "code_commit": code, "normalized_sha256": got,
            "receipt_sha256": sha256_file(receipt_path),
            "health_by_verdict": receipt.get("health_by_verdict"),
            "outcome_by_source": receipt.get("outcome_by_source")}


def revise_rows(payload: dict[str, Any], *, full_universe: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The payload's rows, cut to the revise contract. global_rank is nulled
    unless the caller asserts a full-estate universe (see module docstring)."""
    out: list[dict[str, Any]] = []
    stats = {"rows": 0, "global_rank_withheld": 0, "facts_as_of_known": 0, "facts_as_of_unknown": 0,
             "missing_identity": 0}
    for row in payload.get("rows") or []:
        norm = row.get("normalized") or {}
        r = {k: row.get(k) for k in REVISE_KEYS}
        r["county_fips"] = r["county_fips"] or norm.get("county_fips")
        r["parcel_apn"] = r["parcel_apn"] or norm.get("apn")
        if not (r["property_key"] and r["source_row_id"] and r["county_fips"] and r["parcel_apn"]):
            stats["missing_identity"] += 1
            continue
        if not full_universe and r.get("global_rank") is not None:
            r["global_rank"] = None
            stats["global_rank_withheld"] += 1
        stats["facts_as_of_known" if r.get("source_as_of") else "facts_as_of_unknown"] += 1
        out.append(r)
    stats["rows"] = len(out)
    return out, stats


def _step(receipt: dict[str, Any], name: str, cmd: list[str], cwd: Path, outputs: list[Path]) -> None:
    started = time.monotonic()
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, check=False)
    entry = {"step": name, "cmd": cmd, "cwd": str(cwd), "exit": proc.returncode,
             "seconds": round(time.monotonic() - started, 2),
             "stdout_tail": proc.stdout[-2000:], "stderr_tail": proc.stderr[-2000:]}
    receipt["steps"].append(entry)
    if proc.returncode != 0:
        raise ProducerError(f"step {name} exited {proc.returncode}: {proc.stderr[-400:]}")
    missing = [str(p) for p in outputs if not p.exists()]
    if missing:
        raise ProducerError(f"step {name} produced no {missing}")
    entry["outputs"] = {str(p): sha256_file(p) for p in outputs}


def produce(factory_run: Path, output: Path, *, recipe_engine: Path, deedsonar: Path,
            recipe_python: str, node: str, mecklenburg_taxpayers: Path | None,
            full_universe: bool, allow_dirty: bool, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=True)
    receipt: dict[str, Any] = {
        "producer_version": PRODUCER_VERSION, "started_at": now.isoformat(),
        "writes_database": False,
        "repos": {"landos-engine": git_state(ENGINE_ROOT), "recipe-engine": git_state(recipe_engine),
                  "deedsonar": git_state(deedsonar)},
        "global_rank_policy": "KEEP (caller asserts full universe)" if full_universe
                              else "WITHHELD (partial universe; ranking authority = a full recipe-engine re-rank)",
        "steps": [],
    }
    dirty = [k for k, v in receipt["repos"].items() if v["dirty"]]
    if dirty and not allow_dirty:
        raise ProducerError(f"uncommitted code in {dirty} (--allow-dirty to override)")

    receipt["factory_run"] = verify_factory_run(factory_run, allow_dirty=allow_dirty)
    run_id = f"estate-{receipt['factory_run']['run_id']}-{stamp}"
    receipt["producer_run_id"] = run_id

    enrich_dir, rank_dir, export_dir, payload_dir, revise_dir = (
        output / "01_enrichment", output / "02_rank", output / "03_export",
        output / "04_payload", output / "05_revise")
    enriched = enrich_dir / "LANDOS_ENRICHED_OPPORTUNITIES.csv"
    cmd = [sys.executable, "-m", "scripts.build_nc_dps_public_enrichment_v3",
           "--signals", str(factory_run / "normalized_signals.jsonl"), "--output", str(enrich_dir)]
    if mecklenburg_taxpayers:
        cmd += ["--mecklenburg-taxpayers", str(mecklenburg_taxpayers)]
    _step(receipt, "1_enrich", cmd, ENGINE_ROOT, [enriched])

    ranked = rank_dir / "NC_CURATIVE_ALL.csv"
    _step(receipt, "2_rank", [recipe_python, "-m", "scripts.rank_nc_curative_raw_v3",
                              "--input", str(enriched), "--output", str(rank_dir)], recipe_engine, [ranked])

    _step(receipt, "3_export", [sys.executable, "-m", "scripts.export_opportunities",
                                "--source", str(ranked), "--output", str(export_dir), "--scope", "full",
                                "--exported-at", now.isoformat(), "--export-run-id", f"exprun_{stamp}",
                                "--ranked-universe-date", now.date().isoformat()],
          ENGINE_ROOT, [])
    masters = sorted(export_dir.rglob("ALL_OPPORTUNITIES_MASTER.csv"))
    if len(masters) != 1:
        raise ProducerError(f"export produced {len(masters)} masters")
    receipt["steps"][-1]["outputs"] = {str(masters[0]): sha256_file(masters[0])}

    # The stage builder requires a crosswalk and a coordinates file. The revise
    # path uses neither (ADDs land GEOCODE_REQUIRED; coords go through
    # data_estate_coords_apply_v1), so both are passed EMPTY. The builder then
    # reports every row as no_crosswalk/no_coords, and never defaults it.
    payload_dir.mkdir(parents=True, exist_ok=True)
    crosswalk, coords = payload_dir / "EMPTY_CROSSWALK.csv", payload_dir / "EMPTY_COORDINATES.csv"
    crosswalk.write_text(CROSSWALK_HEADER)
    coords.write_text(COORDS_HEADER)
    payload_path = payload_dir / "stage_payload.json"
    _step(receipt, "4_payload", [node, str(deedsonar / "scripts" / "build-data-estate-payload.mjs"),
                                 str(masters[0]), str(crosswalk), str(coords), str(payload_path)],
          deedsonar, [payload_path])
    payload = json.loads(payload_path.read_text())

    rows, stats = revise_rows(payload, full_universe=full_universe)
    revise_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    for i in range(0, len(rows), CHUNK_ROWS):
        path = revise_dir / f"revise_rows_{i // CHUNK_ROWS + 1:03d}.json"
        path.write_text(json.dumps(rows[i:i + CHUNK_ROWS], separators=(",", ":")))
        chunks.append({"path": str(path), "rows": len(rows[i:i + CHUNK_ROWS]), "sha256": sha256_file(path)})
    receipt["revise"] = {
        "function": "public.data_estate_revise_v1(p_run_id text, p_source text, p_rows jsonb)",
        "run_id": run_id, "source": REVISE_SOURCE, "chunks": chunks, "stats": stats,
        "payload_counts": payload.get("counts"), "payload_problems": payload.get("problems"),
        "requires": ["deedsonar migration 20260912120000 applied on efxo (PR #70): facts-date revise",
                     "deedsonar migration 20260912130000 applied on efxo: missing facts date = UNKNOWN"],
        "t5_action": ("service_role on efxowqudmsfqjmbeidjc ONLY, per chunk: "
                      f"select * from public.data_estate_revise_v1('{run_id}', '{REVISE_SOURCE}', "
                      "<chunk file contents>::jsonb); then "
                      "select public.deedsonar_freshness_refresh_v1(current_date, null, '<current batch>')"),
    }
    receipt["finished_at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    (output / "PRODUCER_RECEIPT.json").write_text(json.dumps(receipt, indent=2, sort_keys=True))
    return receipt


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--factory-run", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--recipe-engine", required=True, type=Path)
    ap.add_argument("--deedsonar", required=True, type=Path)
    ap.add_argument("--recipe-python", default=None,
                    help="interpreter for recipe-engine (default: <recipe-engine>/.venv/bin/python, else this one)")
    ap.add_argument("--node", default="node")
    ap.add_argument("--mecklenburg-taxpayers", type=Path)
    ap.add_argument("--full-universe", action="store_true",
                    help="keep global_rank: asserts the factory run covers the whole estate")
    ap.add_argument("--allow-dirty", action="store_true")
    args = ap.parse_args(argv)
    recipe_python = args.recipe_python or next(
        (str(p) for p in (args.recipe_engine / ".venv" / "bin" / "python",) if p.exists()), sys.executable)
    try:
        receipt = produce(args.factory_run, args.output, recipe_engine=args.recipe_engine,
                          deedsonar=args.deedsonar, recipe_python=recipe_python, node=args.node,
                          mecklenburg_taxpayers=args.mecklenburg_taxpayers,
                          full_universe=args.full_universe, allow_dirty=args.allow_dirty)
    except ProducerError as exc:
        print(json.dumps({"state": "REFUSED", "reason": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"state": "PRODUCED", "producer_run_id": receipt["producer_run_id"],
                      "revise": receipt["revise"]["stats"],
                      "receipt": str(args.output / "PRODUCER_RECEIPT.json")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

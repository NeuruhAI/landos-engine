"""
orchestrate_pipeline.py — THE canonical producing-chain orchestrator (ORANGE JOB 3).

    python -m scripts.orchestrate_pipeline              # whole chain, per-step counts, ZERO writes
    # Live is fail-closed: --live + live acknowledgment + the independent
    # production-write flag and acknowledgment are all required; mint NEVER fires.

Root cause on the 14-day gap is settled: there was never an automated pipeline —
Jul 14 was a one-time manual mint_discovery fire. This module is the ONE canonical
conductor for the real producing chain:

    stage_arcgis [ingest-lane census]  ->  load_signals  ->  load_parcel_owner (county loaders)
      ->  pip_match_signals  ->  mint_discovery [OPERATOR-GATED, never fired here]
      ->  score_emergence  ->  sync_to_crm

DOCTRINE RAILS (each one is load-bearing — do not relax):
  * REPO FIREWALL: the engine never fetches raw sources. The STAGE step is a
    read-only census of landos-ingest's staged JSONL + the exact ingest-lane
    command to run; it never touches the network, in ANY mode.
  * HUMAN MINT GATE: mint_discovery is ALWAYS run as dry-run report here, even
    under --live. Conversion/mining sources never auto-fire. The orchestrator
    prepares and prints the operator fire commands; a human fires them.
    (Signal -> candidate -> gates pass -> lead. The clean-CRM doctrine, upstream.)
  * FIRST-WRITER-WINS: overlapping discovery sources are attributed to the FIRST
    source that mints a parcel (ON CONFLICT DO NOTHING). The mint report delegates
    to mint_discovery's own deliberate default ordering and never reorders it —
    reordering silently changes which signal claims a parcel.
  * FAIL-LOUD: a critical step failure aborts the remaining chain, every step's
    status (incl. NOT-REACHED) prints in the final summary, and the process exits
    non-zero. A silent partial run is the exact failure mode that cost 14 days.
  * RECEIPTS: every step writes a chain-hashed receipt (hunt_core.stage_receipt
    idiom) to data/run-logs/ in THIS canonical repo, plus an INDEX.log line with
    the run file's sha256.

Additive: axon_cycle.py (the taxsale daily cycle) is untouched; this is the
discovery/producing chain it never covered. The launchd plist for this module is
WRITTEN to deploy/ as a PROPOSAL only — installing it is an operator keystroke.
"""
import argparse
import contextlib
import fcntl
import glob
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

from app.run_manifest import sha256_file

ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ENGINE_ROOT, "data", "run-logs")
# Canonical ingest tree (load_signals.py's INGEST_ROOT; ~/repos/landos-ingest is a
# personal symlink to this — never depend on the symlink).
INGEST_SIGNALS = os.path.expanduser("~/neuruh-repos/NeuruhAI/landos-ingest/data/strategy/signals")
LOCK_PATH = os.path.join(LOG_DIR, "orchestrator.lock")
LIVE_ACK = "I_ACKNOWLEDGE_LIVE_RUN"
PRODUCTION_WRITE_ACK = "I_ACKNOWLEDGE_PRODUCTION_WRITES"
CHECKPOINT_SCHEMA_VERSION = "landos-orchestrator-checkpoint-v1"

# A deliberately secret-free description of the conductor.  This is both the
# stage matrix's machine-readable spine and the source of the config
# fingerprint recorded on every receipt.
STAGE_CONTRACTS = {
    "stage_arcgis": {
        "entrypoint": "scripts.orchestrate_pipeline.step_stage_census",
        "inputs": ["landos-ingest/data/strategy/signals/*/staged/*.jsonl"],
        "outputs": ["staged inventory counts"],
        "side_effects": "none",
        "idempotency_key": "run_id:stage_arcgis",
        "checkpoint": "stage exit receipt",
        "receipt": "start/end/exit",
        "failure_behavior": "abort chain",
        "spend_risk": "none",
        "production_write_risk": "none",
        "resume_behavior": "reuse completed exit receipt",
    },
    "load_signals": {
        "entrypoint": "scripts.load_signals.main",
        "inputs": ["staged signal JSONL", "landos.discovery_signals (dedupe read)"],
        "outputs": ["discovery_signals row counts"],
        "side_effects": "live: insert discovery_signals; dry-run: rollback",
        "idempotency_key": "source_type/source_ref ON CONFLICT",
        "checkpoint": "stage exit receipt",
        "receipt": "start/end/exit",
        "failure_behavior": "abort chain",
        "spend_risk": "none",
        "production_write_risk": "database writes in live mode",
        "resume_behavior": "reuse completed exit receipt",
    },
    "load_parcel_owner": {
        "entrypoint": "scripts.load_parcel_owner.main",
        "inputs": ["sealed county owner-roll files"],
        "outputs": ["landos.parcel_owner row counts"],
        "side_effects": "live: upsert parcel_owner; dry-run: rollback",
        "idempotency_key": "county_fips/parcel_apn ON CONFLICT",
        "checkpoint": "stage exit receipt",
        "receipt": "start/end/exit",
        "failure_behavior": "missing county explicitly skipped; other failure aborts",
        "spend_risk": "none",
        "production_write_risk": "database writes in live mode",
        "resume_behavior": "reuse completed exit receipt",
    },
    "pip_match_signals": {
        "entrypoint": "scripts.pip_match_signals.main",
        "inputs": ["discovery_signals", "parcel_geom", "owner_leads"],
        "outputs": ["resolver classification counts"],
        "side_effects": "live: stamp/match discovery_signals; dry-run: rollback",
        "idempotency_key": "signal_id",
        "checkpoint": "stage exit receipt",
        "receipt": "start/end/exit",
        "failure_behavior": "abort chain",
        "spend_risk": "none",
        "production_write_risk": "database writes in live mode",
        "resume_behavior": "reuse completed exit receipt",
    },
    "mint_discovery": {
        "entrypoint": "scripts.mint_discovery.dry_run",
        "inputs": ["eligible discovery_signals"],
        "outputs": ["operator-gated mint report counts"],
        "side_effects": "none; report-only in every mode",
        "idempotency_key": "run_id:mint_discovery",
        "checkpoint": "stage exit receipt",
        "receipt": "start/end/exit",
        "failure_behavior": "abort chain",
        "spend_risk": "none",
        "production_write_risk": "none",
        "resume_behavior": "reuse completed exit receipt",
    },
    "score_emergence": {
        "entrypoint": "scripts.score_emergence.main",
        "inputs": ["qualifying temporal discovery_signals", "owner_leads"],
        "outputs": ["scored/updated row counts"],
        "side_effects": "live: update owner_leads; dry-run: rollback",
        "idempotency_key": "lead_id/current qualifying signals",
        "checkpoint": "stage exit receipt",
        "receipt": "start/end/exit",
        "failure_behavior": "abort chain",
        "spend_risk": "none",
        "production_write_risk": "database writes in live mode",
        "resume_behavior": "reuse completed exit receipt",
    },
    "sync_to_crm": {
        "entrypoint": "scripts.sync_to_crm.main",
        "inputs": ["owner_leads and related LandOS evidence"],
        "outputs": ["projection/contact/push receipt row counts"],
        "side_effects": "live: acquisition_leads/contact projection; dry-run: rollback",
        "idempotency_key": "source_lead_id / lead_id",
        "checkpoint": "stage exit receipt",
        "receipt": "start/end/exit",
        "failure_behavior": "abort chain",
        "spend_risk": "none (no tracing/vendor call)",
        "production_write_risk": "CRM database writes in live mode",
        "resume_behavior": "reuse completed exit receipt",
    },
}


class GateError(RuntimeError):
    """A required operating gate was not explicitly satisfied."""


class ConcurrentRunError(RuntimeError):
    """Another orchestrator process owns the single-run lock."""


class _RunLock:
    """Cross-process, non-blocking lock held for the complete conductor run."""

    def __init__(self, path=LOCK_PATH):
        self.path = path
        self._fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._fh.close()
            self._fh = None
            raise ConcurrentRunError(
                f"another orchestrator run holds lock: {self.path}"
            ) from exc
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"pid={os.getpid()}\n")
        self._fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fh is not None:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            self._fh.close()
        return False


def stage_receipt(prev_chain_hash, stage, body):
    """Pure chained receipt helper; kept DB-free so the conductor can self-test."""
    body_hash = hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()
    chain_hash = hashlib.sha256(
        ((prev_chain_hash or "GENESIS") + body_hash).encode("utf-8")
    ).hexdigest()
    return {
        "stage": stage,
        "at": datetime.now(timezone.utc).isoformat(),
        "body": body,
        "body_hash": body_hash,
        "prev": prev_chain_hash,
        "chain_hash": chain_hash,
    }


class _Tee(io.TextIOBase):
    """Stream step output to the console AND capture it for the receipt."""

    def __init__(self, real):
        self.real = real
        self.buf = io.StringIO()

    def write(self, s):
        self.real.write(s)
        self.buf.write(s)
        return len(s)

    def flush(self):
        self.real.flush()


def _harvest(text, patterns):
    """Best-effort count harvest from a step's printed summary. The receipt keeps
    the raw output tail regardless, so a missed regex never loses information."""
    counts = {}
    for key, pat in patterns.items():
        last = None
        for m in re.finditer(pat, text):
            g = next((g for g in m.groups() if g is not None), None)
            if g is not None:
                last = int(g)   # totals print after per-file lines — keep the LAST match
        if last is not None:
            counts[key] = last
    return counts


def _canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _source_sha():
    """Return the full source SHA.  Unknown is an error, never a fabricated SHA."""
    proc = subprocess.run(
        ["git", "-C", ENGINE_ROOT, "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=15,
    )
    if proc.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", proc.stdout.strip()):
        raise RuntimeError("cannot establish the engine source SHA")
    return proc.stdout.strip()


def _safe_config(dry_run, fixture_mode):
    """Secret-free configuration snapshot used to detect unsafe resume drift."""
    return {
        "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
        "dry_run": bool(dry_run),
        "fixture_mode": bool(fixture_mode),
        "stage_order": [stage[0] for stage in CHAIN],
        "stage_contracts": STAGE_CONTRACTS,
        "tracing": "disabled",
    }


def _config_fingerprint(config):
    return hashlib.sha256(_canonical_json(config).encode("utf-8")).hexdigest()


def _atomic_json_write(path, value):
    """fsync + replace: a crash leaves either the old or complete new checkpoint."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".orchestrator-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, sort_keys=True, default=str)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def _load_checkpoint(path):
    with open(path, encoding="utf-8") as fh:
        value = json.load(fh)
    if value.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
        raise GateError(f"unsupported or missing checkpoint schema: {path}")
    _verify_receipt_chain(value.get("receipts") or [])
    return value


def _verify_receipt_chain(receipts):
    previous = None
    for index, receipt in enumerate(receipts):
        expected_body = hashlib.sha256(
            _canonical_json(receipt.get("body")).encode("utf-8")
        ).hexdigest()
        expected_chain = hashlib.sha256(
            ((previous or "GENESIS") + expected_body).encode("utf-8")
        ).hexdigest()
        if receipt.get("prev") != previous:
            raise GateError(f"receipt chain prev mismatch at index {index}")
        if receipt.get("body_hash") != expected_body:
            raise GateError(f"receipt body hash mismatch at index {index}")
        if receipt.get("chain_hash") != expected_chain:
            raise GateError(f"receipt chain hash mismatch at index {index}")
        previous = expected_chain
    return previous


def _new_run_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:12]}"


@contextlib.contextmanager
def _tracing_disabled(run_id):
    """Prevent inherited observability configuration from causing paid traces."""
    overrides = {
        "LANDOS_RUN_ID": run_id,
        "LANGCHAIN_TRACING_V2": "false",
        "LANGSMITH_TRACING": "false",
        "OTEL_SDK_DISABLED": "true",
    }
    previous = {key: os.environ.get(key) for key in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _validate_gates(
    *,
    dry_run,
    live_ack,
    allow_production_writes,
    production_write_ack,
    fixture_mode,
):
    if dry_run:
        if allow_production_writes or production_write_ack:
            raise GateError("production-write gates are invalid during dry-run")
        return
    if fixture_mode:
        raise GateError("fixture data is prohibited in live mode")
    if live_ack != LIVE_ACK:
        raise GateError(f"live mode requires --acknowledge-live {LIVE_ACK}")
    if not allow_production_writes:
        raise GateError("live mode requires the additional --allow-production-writes gate")
    if production_write_ack != PRODUCTION_WRITE_ACK:
        raise GateError(
            "production writes require --acknowledge-production-writes "
            f"{PRODUCTION_WRITE_ACK}"
        )


def _receipt_body(base, *, event, status, counts=None, seconds=None, error=None,
                  stdout_tail=None, exit_code=None):
    body = {
        **base,
        "event": event,
        "status": status,
        "counts": counts or {},
    }
    if seconds is not None:
        body["seconds"] = seconds
    if error is not None:
        body["error"] = error
    if stdout_tail is not None:
        body["stdout_tail"] = stdout_tail
    if exit_code is not None:
        body["exit_code"] = exit_code
    return body


def _assert_no_fixture_success(value, path="counts"):
    """Reject explicit fixture provenance before a live stage can be marked OK."""
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in {"fixture", "is_fixture", "fixture_mode"} and bool(child):
                raise RuntimeError(f"live stage returned fixture evidence at {path}.{key}")
            _assert_no_fixture_success(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_no_fixture_success(child, f"{path}[{index}]")


# ---------------------------------------------------------------------------
# Step implementations. Each returns a counts dict; raising = step FAILED.
# ---------------------------------------------------------------------------
def step_stage_census(dry_run):
    """STAGE (ingest-lane census — read-only in EVERY mode, repo firewall).

    Counts staged JSONL per lane so the chain's input surface is visible, and
    prints the ingest-lane command for a refresh. Never fetches."""
    if not os.path.isdir(INGEST_SIGNALS):
        raise RuntimeError(f"landos-ingest staged tree missing: {INGEST_SIGNALS}")
    lanes = {}
    total_files = total_rows = 0
    newest = None
    for staged in sorted(glob.glob(os.path.join(INGEST_SIGNALS, "*", "staged"))):
        lane = os.path.basename(os.path.dirname(staged))
        files = sorted(glob.glob(os.path.join(staged, "*.jsonl")))
        rows = 0
        for f in files:
            with open(f, "rb") as fh:
                rows += sum(1 for line in fh if line.strip())
            mt = os.path.getmtime(f)
            if newest is None or mt > newest:
                newest = mt
        if files:
            lanes[lane] = {"files": len(files), "rows": rows}
            total_files += len(files)
            total_rows += rows
            print(f"  [{lane}] {len(files)} staged file(s), {rows} row(s)")
    newest_iso = (datetime.fromtimestamp(newest, tz=timezone.utc).isoformat()
                  if newest else None)
    print(f"  staged inventory: {total_files} file(s), {total_rows} row(s), "
          f"newest {newest_iso}")
    print("  REPO FIREWALL: the engine never fetches. To refresh staging, run the "
          "ingest lane:\n    cd ~/neuruh-repos/NeuruhAI/landos-ingest && "
          "python strategy/stage_signals.py   # (or the per-lane stager)")
    return {"lanes": len(lanes), "staged_files": total_files,
            "staged_rows": total_rows, "newest_staged": newest_iso, "per_lane": lanes}


def step_load_signals(dry_run):
    from scripts import load_signals
    return load_signals.main(list(load_signals.SOURCES), dry_run)


def step_load_parcel_owner(dry_run):
    """County owner-roll loaders — every registry county. A county whose sealed
    CSV is absent on this host is an honest SKIP, never a chain abort."""
    from scripts import load_parcel_owner
    out = {"counties_loaded": 0, "counties_skipped": 0}
    for county in load_parcel_owner.COUNTIES:
        try:
            r = load_parcel_owner.main(county=county, dry_run=dry_run)
            out[county] = dict(r)
            out["counties_loaded"] += 1
        except SystemExit as e:
            print(f"  [{county}] SKIP — {e}")
            out["counties_skipped"] += 1
    return out


def step_pip_match(dry_run):
    from scripts import pip_match_signals
    r = pip_match_signals.main(None, dry_run)
    r.pop("dark_counties", None)
    return r


def step_mint_report(dry_run):
    """OPERATOR-GATED: always a dry-run report, even under --live. mint_discovery's
    own default source ordering is the deliberate first-writer-wins order — this
    step delegates to it and never reorders."""
    from scripts import mint_discovery
    rows = mint_discovery.dry_run(None)
    print("\n  MINT GATE: nothing fired. The commands above are the operator's "
          "keystroke — the orchestrator only prepares and reports.")
    return {"sources_reported": len(rows),
            "net_new_total": sum(n for _, _, n in rows),
            "per_source": {src: n for src, _, n in rows}}


def step_score(dry_run):
    from scripts import score_emergence
    return score_emergence.main(dry_run=dry_run)


def step_sync(dry_run):
    from scripts import sync_to_crm
    return sync_to_crm.main(dry_run=dry_run)


# (key, label, fn, harvest patterns for printed summaries)
CHAIN = [
    ("stage_arcgis", "STAGE census (landos-ingest, read-only)", step_stage_census, {}),
    ("load_signals", "load_signals (staged JSONL -> discovery_signals)", step_load_signals, {
        "read": r"read[= ](\d+)", "inserted": r"(?:would_insert|inserted)[= ](\d+)",
        "skipped_dup": r"skipped_dup[= ](\d+)", "rejected_stale": r"rejected_stale[= ](\d+)"}),
    ("load_parcel_owner", "load_parcel_owner (county owner rolls)", step_load_parcel_owner, {}),
    ("pip_match_signals", "pip_match_signals (spatial resolver)", step_pip_match, {}),
    ("mint_discovery", "mint_discovery [OPERATOR-GATED report]", step_mint_report, {}),
    ("score_emergence", "score_emergence (temporal decay scores)", step_score, {
        "leads_scored": r"leads_scored=(\d+)|(?:^|\s)(\d+) lead\(s\) would score",
        "signals": r"temporal_qualifying_signals=(\d+)"}),
    ("sync_to_crm", "sync_to_crm (SOLE writer -> acquisition_leads)", step_sync, {
        "enriched": r"enriched=(\d+)", "discovered_inserted": r"discovered_inserted=(\d+)",
        "phones_projected": r"phones_projected=(\d+)", "unmatched": r"unmatched=(\d+)",
        "briefs_composed": r"BRIEF DRY-RUN — (\d+) composed"}),
]


def main(
    dry_run=True,
    *,
    live_ack=None,
    allow_production_writes=False,
    production_write_ack=None,
    fixture_mode=False,
    resume_run_id=None,
    run_id=None,
    log_dir=None,
    lock_path=None,
):
    """Run the producing chain.

    ``dry_run=True`` is deliberately the API default as well as the CLI default.
    Tests/operators may inject ``log_dir`` and ``lock_path``; production command
    lines use the canonical paths above.
    """
    inherited_fixture = os.environ.get("LANDOS_FIXTURE_MODE", "").strip().lower()
    fixture_mode = bool(fixture_mode or inherited_fixture in {"1", "true", "yes", "on"})
    _validate_gates(
        dry_run=dry_run,
        live_ack=live_ack,
        allow_production_writes=allow_production_writes,
        production_write_ack=production_write_ack,
        fixture_mode=fixture_mode,
    )
    if resume_run_id and run_id:
        raise GateError("choose either run_id or resume_run_id, never both")

    output_dir = log_dir or LOG_DIR
    active_lock_path = lock_path or os.path.join(output_dir, "orchestrator.lock")
    os.makedirs(output_dir, exist_ok=True)
    source_sha = _source_sha()
    safe_config = _safe_config(dry_run, fixture_mode)
    config_fingerprint = _config_fingerprint(safe_config)
    active_run_id = resume_run_id or run_id or _new_run_id()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", active_run_id):
        raise GateError("run_id contains unsafe characters or is too long")
    run_path = os.path.join(output_dir, f"orchestrator_{active_run_id}.receipts.json")
    mode = "DRY-RUN (zero writes)" if dry_run else "LIVE (production writes armed)"

    with _RunLock(active_lock_path):
        if resume_run_id:
            if not os.path.isfile(run_path):
                raise GateError(f"resume checkpoint does not exist: {run_path}")
            checkpoint = _load_checkpoint(run_path)
            if checkpoint.get("source_sha") != source_sha:
                raise GateError("resume refused: source SHA changed")
            if checkpoint.get("config_fingerprint") != config_fingerprint:
                raise GateError("resume refused: config fingerprint changed")
            receipts = list(checkpoint.get("receipts") or [])
            old_results = {
                row["step"]: row for row in checkpoint.get("results") or []
                if row.get("status") in ("OK", "RESUMED")
            }
            print(f"RESUMING run {active_run_id} from atomic checkpoint")
        else:
            if os.path.exists(run_path):
                raise GateError(f"run checkpoint already exists: {run_path}")
            receipts, old_results = [], {}

        state = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "run_id": active_run_id,
            "dry_run": bool(dry_run),
            "fixture_mode": bool(fixture_mode),
            "source_sha": source_sha,
            "config": safe_config,
            "config_fingerprint": config_fingerprint,
            "aborted_at": None,
            "complete": False,
            "results": [],
            "receipts": receipts,
        }

        def checkpoint_write():
            _atomic_json_write(run_path, state)

        def append_event(stage, body):
            previous = state["receipts"][-1]["chain_hash"] if state["receipts"] else None
            state["receipts"].append(stage_receipt(previous, stage, body))
            checkpoint_write()

        print(
            f"{'='*74}\nPRODUCING-CHAIN ORCHESTRATOR — {active_run_id}  [{mode}]\n"
            f"source_sha={source_sha}\nconfig_fingerprint={config_fingerprint}\n{'='*74}"
        )

        aborted_at = None
        results = []
        with _tracing_disabled(active_run_id):
            for key, label, fn, patterns in CHAIN:
                base = {
                    "run_id": active_run_id,
                    "step": key,
                    "dry_run": bool(dry_run),
                    "fixture_mode": bool(fixture_mode),
                    "source_sha": source_sha,
                    "config_fingerprint": config_fingerprint,
                    "idempotency_key": STAGE_CONTRACTS[key]["idempotency_key"],
                    "spend_risk": STAGE_CONTRACTS[key]["spend_risk"],
                    "production_write_risk": STAGE_CONTRACTS[key]["production_write_risk"],
                }
                if aborted_at:
                    result = {"step": key, "status": "NOT-REACHED", "seconds": 0.0, "counts": {}}
                    results.append(result)
                    state["results"] = list(results)
                    append_event(
                        key,
                        _receipt_body(
                            base, event="exit", status="NOT-REACHED", exit_code=1,
                            error=f"upstream stage failed: {aborted_at}",
                        ),
                    )
                    continue

                previous_result = old_results.get(key)
                if previous_result:
                    counts = dict(previous_result.get("counts") or {})
                    result = {
                        "step": key, "status": "RESUMED", "seconds": 0.0,
                        "counts": counts,
                    }
                    results.append(result)
                    state["results"] = list(results)
                    append_event(
                        key,
                        _receipt_body(
                            base, event="exit", status="RESUMED", counts=counts,
                            seconds=0.0, exit_code=0,
                        ),
                    )
                    print(f">>> {label}\n<<< checkpoint OK; stage not repeated")
                    continue

                print(f"\n{'-'*74}\n>>> {label}\n{'-'*74}")
                tee = _Tee(sys.stdout)
                append_event(key, _receipt_body(base, event="start", status="RUNNING"))
                started = time.monotonic()
                try:
                    with contextlib.redirect_stdout(tee):
                        returned = fn(dry_run)
                    if not isinstance(returned, dict):
                        raise RuntimeError(
                            f"stage {key} returned {type(returned).__name__}, expected counts dict"
                        )
                    counts = dict(returned)
                    # Compatibility harvest supplements explicit counts, never replaces them.
                    for count_key, value in _harvest(tee.buf.getvalue(), patterns).items():
                        counts.setdefault(count_key, value)
                    if not dry_run:
                        _assert_no_fixture_success(counts)
                    seconds = round(time.monotonic() - started, 3)
                    status, exit_code, error = "OK", 0, None
                except BaseException as exc:  # SystemExit included: fail closed.
                    seconds = round(time.monotonic() - started, 3)
                    error = f"{type(exc).__name__}: {exc}"
                    counts = {}
                    status, exit_code = "FAILED", 1
                    aborted_at = key
                    print(f"<<< {label} FAILED after {seconds}s: {error}")

                tail = tee.buf.getvalue().splitlines()[-40:]
                append_event(
                    key,
                    _receipt_body(
                        base, event="end", status=status, counts=counts,
                        seconds=seconds, error=error, stdout_tail=tail,
                    ),
                )
                append_event(
                    key,
                    _receipt_body(
                        base, event="exit", status=status, counts=counts,
                        seconds=seconds, error=error, exit_code=exit_code,
                    ),
                )
                result = {
                    "step": key, "status": status, "seconds": seconds,
                    "counts": counts,
                }
                results.append(result)
                state["results"] = list(results)
                state["aborted_at"] = aborted_at
                checkpoint_write()
                if status == "OK":
                    print(f"<<< {label} OK ({seconds}s)")

        incomplete = [
            row["step"] for row in results
            if row["status"] not in ("OK", "RESUMED")
        ]
        state["results"] = results
        state["aborted_at"] = aborted_at
        state["complete"] = not incomplete
        checkpoint_write()

        print(f"\n{'='*74}\nRUN SUMMARY — {active_run_id}  [{mode}]\n{'='*74}")
        for row in results:
            shown = {
                k: v for k, v in row["counts"].items()
                if not isinstance(v, dict) and k != "newest_staged"
            }
            print(
                f"  {row['step']:<20} {row['status']:<12} "
                f"{row['seconds']:>7.3f}s  {shown}"
            )
        if dry_run:
            print("\n  ZERO WRITES: every reached step ran in dry-run; mint was report-only.")
        if incomplete:
            print(
                f"\n  CHAIN INCOMPLETE at {incomplete[0]} — nonzero exit required; "
                "resume only from the same source SHA and config fingerprint."
            )

        rc = 1 if incomplete else 0
        sha = sha256_file(run_path)
        index_path = os.path.join(output_dir, "INDEX.log")
        with open(index_path, "a", encoding="utf-8") as fh:
            fh.write(
                f"{active_run_id}  orchestrator rc={rc} sha256={sha} "
                f"receipts={run_path}\n"
            )
            fh.flush()
            os.fsync(fh.fileno())
        print(f"\n  receipts: {run_path}\n  sha256:   {sha}")
        return rc


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="canonical producing-chain orchestrator "
                                             "(dry-run by default; mint never auto-fires)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="whole chain, per-step counts, ZERO writes (default)")
    g.add_argument("--live", action="store_true",
                   help="arm live mode; also requires both acknowledgments and write gate")
    ap.add_argument("--acknowledge-live", metavar="TOKEN",
                    help=f"live acknowledgment; exact token: {LIVE_ACK}")
    ap.add_argument("--allow-production-writes", action="store_true",
                    help="additional gate required before any live database write")
    ap.add_argument("--acknowledge-production-writes", metavar="TOKEN",
                    help=f"production-write acknowledgment; exact token: {PRODUCTION_WRITE_ACK}")
    ap.add_argument("--fixture", action="store_true",
                    help="label inputs as fixtures (prohibited with --live)")
    ap.add_argument("--resume", metavar="RUN_ID",
                    help="resume an incomplete checkpoint with identical source/config")
    a = ap.parse_args()
    try:
        sys.exit(main(
            dry_run=not a.live,
            live_ack=a.acknowledge_live,
            allow_production_writes=a.allow_production_writes,
            production_write_ack=a.acknowledge_production_writes,
            fixture_mode=a.fixture,
            resume_run_id=a.resume,
        ))
    except (GateError, ConcurrentRunError) as exc:
        print(f"ORCHESTRATOR REFUSED: {exc}", file=sys.stderr)
        sys.exit(2)

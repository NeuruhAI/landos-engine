"""
orchestrate_pipeline.py — THE canonical producing-chain orchestrator (ORANGE JOB 3).

    python -m scripts.orchestrate_pipeline --dry-run    # whole chain, per-step counts, ZERO writes
    python -m scripts.orchestrate_pipeline --live       # loaders/resolver/score/sync write; mint NEVER fires

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
import glob
import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from app.hunt_core import stage_receipt
from app.run_manifest import sha256_file

ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(ENGINE_ROOT, "data", "run-logs")
# Canonical ingest tree (load_signals.py's INGEST_ROOT; ~/repos/landos-ingest is a
# personal symlink to this — never depend on the symlink).
INGEST_SIGNALS = os.path.expanduser("~/neuruh-repos/NeuruhAI/landos-ingest/data/strategy/signals")


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
    load_signals.main(list(load_signals.SOURCES), dry_run)
    return {}   # counts harvested from output


def step_load_parcel_owner(dry_run):
    """County owner-roll loaders — every registry county. A county whose sealed
    CSV is absent on this host is an honest SKIP, never a chain abort."""
    from scripts import load_parcel_owner
    out = {"counties_loaded": 0, "counties_skipped": 0}
    for county in load_parcel_owner.COUNTIES:
        try:
            r = load_parcel_owner.main(county=county, dry_run=dry_run)
            out[county] = {"before": r["before"], "after": r["after"],
                           "loadable": r["loadable"], "govt_flagged": r["govt_flagged"]}
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
    score_emergence.main(dry_run=dry_run)
    return {}


def step_sync(dry_run):
    from scripts import sync_to_crm
    sync_to_crm.main(dry_run=dry_run)
    return {}


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


def main(dry_run=True):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(LOG_DIR, exist_ok=True)
    mode = "DRY-RUN (zero writes)" if dry_run else "LIVE (mint still gated)"
    print(f"{'='*74}\nPRODUCING-CHAIN ORCHESTRATOR — {ts}  [{mode}]\n{'='*74}")

    receipts, results = [], []
    aborted_at = None
    for key, label, fn, patterns in CHAIN:
        if aborted_at:
            results.append((key, "NOT-REACHED", 0.0, {}))
            continue
        print(f"\n{'-'*74}\n>>> {label}\n{'-'*74}")
        tee = _Tee(sys.stdout)
        t0 = time.time()
        try:
            with contextlib.redirect_stdout(tee):
                counts = fn(dry_run) or {}
            secs = round(time.time() - t0, 1)
            counts.update(_harvest(tee.buf.getvalue(), patterns))
            status = "OK"
        except BaseException as e:  # noqa: BLE001 — SystemExit included: fail LOUD, not silent
            secs = round(time.time() - t0, 1)
            counts = {"error": f"{type(e).__name__}: {e}"}
            status = "FAILED"
            aborted_at = key
            print(f"<<< {label} FAILED after {secs}s: {counts['error']}")
        results.append((key, status, secs, counts))
        prev = receipts[-1]["chain_hash"] if receipts else None
        body = {"step": key, "status": status, "seconds": secs, "dry_run": dry_run,
                "counts": counts,
                "stdout_tail": tee.buf.getvalue().splitlines()[-40:]}
        receipts.append(stage_receipt(prev, key, body))
        if status == "OK":
            print(f"<<< {label} OK ({secs}s)")

    # ---- fail-loud final summary: EVERY step, always -------------------------
    failed = [k for k, s, _, _ in results if s == "FAILED"]
    print(f"\n{'='*74}\nRUN SUMMARY — {ts}  [{mode}]\n{'='*74}")
    for key, status, secs, counts in results:
        shown = {k: v for k, v in counts.items()
                 if not isinstance(v, dict) and k != "newest_staged"}
        print(f"  {'✅' if status == 'OK' else '🔴' if status == 'FAILED' else '⚪️'} "
              f"{key:<20} {status:<12} {secs:>7.1f}s  {shown}")
    if dry_run:
        print("\n  ZERO WRITES: every step ran in dry-run; mint was report-only.")
    if failed:
        print(f"\n  🔴 CHAIN ABORTED at {failed[0]} — downstream steps NOT-REACHED. "
              f"A partial run is a defect: fix and re-run; every step is idempotent.")

    run_path = os.path.join(LOG_DIR, f"orchestrator_{ts}.receipts.json")
    with open(run_path, "w") as fh:
        json.dump({"run": ts, "dry_run": dry_run, "aborted_at": aborted_at,
                   "results": [{"step": k, "status": s, "seconds": secs, "counts": c}
                               for k, s, secs, c in results],
                   "receipts": receipts}, fh, indent=2, default=str)
    sha = sha256_file(run_path)
    with open(os.path.join(LOG_DIR, "INDEX.log"), "a") as fh:
        fh.write(f"{ts}  orchestrator rc={1 if failed else 0} "
                 f"sha256={sha} receipts={run_path}\n")
    print(f"\n  receipts: {run_path}\n  sha256:   {sha}")
    return 1 if failed else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="canonical producing-chain orchestrator "
                                             "(dry-run by default; mint never auto-fires)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true",
                   help="whole chain, per-step counts, ZERO writes (default)")
    g.add_argument("--live", action="store_true",
                   help="loaders/resolver/score/sync write; mint_discovery is STILL report-only")
    a = ap.parse_args()
    sys.exit(main(dry_run=not a.live))

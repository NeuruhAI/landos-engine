#!/usr/bin/env bash
# daily_run.sh — the armed AXON daily cycle (additive). Chains the discovery cycle (B) and
# contactability (C) in ONE python process each, fail-loud, with per-step counts + a
# tamper-evident manifest sha256 written to data/run-logs/.
#
#   B  scripts.axon_cycle           load -> mint -> score -> geocode(multi) -> situs ->
#                                   reverse -> owner-mailing -> sync_to_crm   (one process)
#   C  scripts.skip_trace_tracerfy  contactability (LIVE vendor = Tracerfy, $0.02/match); NO-SPEND
#                                   unless TRACERFY_API_KEY is set (then capped + --confirm-spend).
#                                   DNC respected at the sync write boundary. (skip_trace_batchdata
#                                   stays dormant in the repo — not called here.)
#
# A (Belinda 8c661c22 geocode) was a one-time loose-end close, already done + idempotent — it is
# NOT in the daily cadence.
#
# NB: launchd fires only while this Mac is AWAKE. For always-on, this same script belongs on the
# VPS (hosted-API inference box) — flagged, not built here.
set -euo pipefail

ENGINE="/Users/jeramiehicks/Desktop/Neuruh Master/Bryan Franklin - Real Estate Partnered Builds:Projects/landos-engine"
PY="$ENGINE/.venv/bin/python"
LOGDIR="$ENGINE/data/run-logs"
mkdir -p "$LOGDIR"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
LOG="$LOGDIR/axon_run_${TS}.log"

rc=0
{
  echo "=== AXON daily run ${TS} (host=$(hostname)) ==="
  echo "--- step B: scripts.axon_cycle ---"
  ( cd "$ENGINE" && "$PY" -m scripts.axon_cycle ) || rc=$?
  echo "--- step C: scripts.skip_trace_tracerfy (individuals, limit 200) ---"
  ( cd "$ENGINE" && "$PY" -m scripts.skip_trace_tracerfy --limit 200 ) || rc=$?
  echo "=== run rc=${rc} ==="
} >>"$LOG" 2>&1

SHA="$(shasum -a 256 "$LOG" | awk '{print $1}')"
echo "manifest_sha256=${SHA}" >>"$LOG"
echo "${TS}  rc=${rc}  sha256=${SHA}  log=${LOG}" >>"$LOGDIR/INDEX.log"
# surface the receipt on stdout too (launchd captures it to the plist's StandardOut path)
echo "AXON daily run ${TS}: rc=${rc} sha256=${SHA} log=${LOG}"
exit "$rc"

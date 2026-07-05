#!/bin/bash
# backup_spine.sh — nightly logical backup of the LandOS spine (Supabase Postgres).
#
#   scripts/backup_spine.sh
#
# Dumps the ENTIRE database (all schemas: landos, public, territory, recipe, comms,
# auth, storage, …) as a plain-SQL dump, gzipped, to ~/models/provenance/db/. One
# file per calendar day: spine-YYYY-MM-DD.sql.gz.
#
# Doctrine:
#   * AI-NEVER-DELETES: NO auto-rotation. Old backups are the operator's monthly chore.
#     Every run prints the total backup-dir size + free disk so disk pressure is visible
#     (this is an 8 GB-class Mac — a silently growing backup dir would bite).
#   * ATOMIC: dump to a .partial file, integrity-check it, THEN rename into place — a
#     backup that fails or is interrupted never leaves a truncated file masquerading as
#     a good one.
#   * SELF-CONTAINED: reads DATABASE_URL straight from the engine .env (not ~/.neuruh_env)
#     and finds pg_dump even under cron's minimal PATH (Homebrew libpq is keg-only).
#   * VERIFIED: gzip integrity (gunzip -t) + content markers proving the spine is really
#     inside (plain-SQL dump, so pg_restore --list does not apply — gunzip -t is the check).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"
OUT_DIR="$HOME/models/provenance/db"

# --- locate pg_dump (Homebrew libpq is keg-only; cron PATH won't have it) --------------
for d in /opt/homebrew/opt/libpq/bin /usr/local/opt/libpq/bin /opt/homebrew/bin /usr/local/bin /usr/bin; do
  if [ -x "$d/pg_dump" ]; then PATH="$d:$PATH"; break; fi
done
PGDUMP="$(command -v pg_dump || true)"
if [ -z "$PGDUMP" ]; then
  echo "backup_spine: FATAL — pg_dump not found on PATH" >&2; exit 1
fi

# --- resolve DATABASE_URL from the engine .env, normalize the SQLAlchemy scheme --------
if [ ! -f "$ENV_FILE" ]; then
  echo "backup_spine: FATAL — no .env at $ENV_FILE" >&2; exit 1
fi
RAW="$(grep -E '^DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"'')"
# postgresql+psycopg://  ->  postgresql://   (pg_dump speaks libpq URIs, not SQLAlchemy)
DBURL="$(printf '%s' "$RAW" | sed -E 's#postgresql\+[a-z]+://#postgresql://#')"
if [ -z "$DBURL" ]; then
  echo "backup_spine: FATAL — DATABASE_URL empty in $ENV_FILE" >&2; exit 1
fi

mkdir -p "$OUT_DIR"
STAMP="$(date +%F)"
OUT="$OUT_DIR/spine-$STAMP.sql.gz"
TMP="$OUT.partial"

echo "backup_spine: dumping spine -> $OUT"
# Dump to a .partial first. `set -o pipefail` above makes a pg_dump failure fail the pipe.
if ! pg_dump "$DBURL" | gzip > "$TMP"; then
  echo "backup_spine: FATAL — pg_dump failed; removing partial" >&2
  rm -f "$TMP"; exit 1
fi

# --- verify BEFORE it is allowed to become the day's backup ----------------------------
if ! gzip -t "$TMP"; then
  echo "backup_spine: FATAL — gzip integrity check failed; removing partial" >&2
  rm -f "$TMP"; exit 1
fi
# content markers: the dump must actually contain the spine (guards against an empty/
# permission-starved dump that is valid gzip but hollow). Single-pass grep -c reads the
# WHOLE stream (no early exit), so gzip finishes cleanly — using grep -q here would send
# SIGPIPE to gzip and, under `set -o pipefail`, be misread as a dump failure.
MARKERS="$(gzip -dc "$TMP" | grep -cE '^(CREATE SCHEMA landos;|COPY landos\.owner_leads )' || true)"
if [ "${MARKERS:-0}" -lt 2 ]; then
  echo "backup_spine: FATAL — dump missing expected landos markers (found $MARKERS/2); removing partial" >&2
  rm -f "$TMP"; exit 1
fi

mv -f "$TMP" "$OUT"

# --- report: this file, the whole dir, and free disk (NO rotation — never deletes) -----
NEW_SIZE="$(ls -lh "$OUT" | awk '{print $5}')"
COUNT="$(ls -1 "$OUT_DIR"/spine-*.sql.gz 2>/dev/null | wc -l | tr -d ' ')"
DIR_SIZE="$(du -sh "$OUT_DIR" | awk '{print $1}')"
FREE="$(df -h "$OUT_DIR" | awk 'NR==2 {print $4}')"
echo "backup_spine: OK  file=$NEW_SIZE  backups=$COUNT  dir_total=$DIR_SIZE  disk_free=$FREE"
echo "backup_spine: NO auto-rotation (AI-never-deletes) — prune old spine-*.sql.gz by hand."

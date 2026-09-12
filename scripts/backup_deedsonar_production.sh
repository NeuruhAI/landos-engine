#!/bin/bash
# backup_deedsonar_production.sh: nightly logical backup of DeedSonar PRODUCTION.
#
#   scripts/backup_deedsonar_production.sh
#
# Production is Supabase project efxowqudmsfqjmbeidjc. It has been since the
# 2026-09-10 cutover. backup_spine.sh dumps the engine's DATABASE_URL, which is the
# legacy spine lvdimujkfheeyqchjzra. Before this script, nothing on this host backed
# up production.
#
# Doctrine (same as backup_spine.sh):
#   * ONE AUTHORITY: the DSN is DEEDSONAR_DATABASE_URL, the same variable the contact
#     writers use (app/contact_authority.py). There is no fallback to DATABASE_URL. The
#     legacy ref is refused, and any DSN that does not name efxo is refused.
#   * COLD CUSTODY, NOT THE SSD: dumps go to NEURUH-HD. If the volume is not mounted
#     the run FAILS LOUDLY. It never falls back to the internal disk, which has hit
#     ENOSPC.
#   * AI-NEVER-DELETES: no rotation. Pruning is the operator's chore.
#   * ATOMIC + VERIFIED: dump to .partial, check `pg_restore --list` for content
#     markers, write a sha256 sidecar, THEN rename into place.
#   * The DSN is never printed.
#
# Test hooks (never set in cron): BACKUP_ENV_FILE, DEEDSONAR_BACKUP_VOLUME,
# PG_DUMP_BIN, PG_RESTORE_BIN.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${BACKUP_ENV_FILE:-$SCRIPT_DIR/../.env}"
VOLUME="${DEEDSONAR_BACKUP_VOLUME:-/Volumes/NEURUH-HD}"
OUT_DIR="$VOLUME/NEURUH_BACKUPS/deedsonar-efxo"
CANONICAL_REF="efxowqudmsfqjmbeidjc"
LEGACY_REF="lvdimujkfheeyqchjzra"

die() { echo "backup_deedsonar_production: FATAL — $*" >&2; exit 1; }

# --- locate pg_dump / pg_restore (Homebrew libpq is keg-only; cron PATH lacks it) ----
for d in /opt/homebrew/opt/libpq/bin /usr/local/opt/libpq/bin /opt/homebrew/bin /usr/local/bin /usr/bin; do
  if [ -x "$d/pg_dump" ]; then PATH="$d:$PATH"; break; fi
done
PGDUMP="${PG_DUMP_BIN:-$(command -v pg_dump || true)}"
PGRESTORE="${PG_RESTORE_BIN:-$(command -v pg_restore || true)}"
[ -n "$PGDUMP" ] || die "pg_dump not found on PATH"
[ -n "$PGRESTORE" ] || die "pg_restore not found on PATH"

# --- the one production DSN: DEEDSONAR_DATABASE_URL, no fallback ---------------------
[ -f "$ENV_FILE" ] || die "no .env at $ENV_FILE"
RAW="$(grep -E '^DEEDSONAR_DATABASE_URL=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"'' || true)"
[ -n "$RAW" ] || die "DEEDSONAR_DATABASE_URL is not set in $ENV_FILE (there is no DATABASE_URL fallback)"
DBURL="$(printf '%s' "$RAW" | sed -E 's#postgresql\+[a-z]+://#postgresql://#')"
case "$DBURL" in
  *"$LEGACY_REF"*) die "DEEDSONAR_DATABASE_URL names the legacy spine $LEGACY_REF; production is $CANONICAL_REF" ;;
  *"$CANONICAL_REF"*) : ;;
  *) die "DEEDSONAR_DATABASE_URL does not name production $CANONICAL_REF; refusing to back up an unproven target" ;;
esac

# --- cold custody must be present; never fall back to the internal disk -------------
[ -d "$VOLUME" ] || die "backup volume not present: $VOLUME (is NEURUH-HD mounted?)"
mkdir -p "$OUT_DIR"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$OUT_DIR/deedsonar-${CANONICAL_REF}-${STAMP}.dump"
TMP="$OUT.partial"

echo "backup_deedsonar_production: dumping production ($CANONICAL_REF) -> $OUT"
if ! "$PGDUMP" --format=custom --no-password --dbname="$DBURL" --file="$TMP"; then
  rm -f "$TMP"; die "pg_dump failed; partial removed"
fi

# --- verify BEFORE it may become a backup: readable TOC + production content markers --
TOC="$("$PGRESTORE" --list "$TMP" 2>/dev/null || true)"
[ -n "$TOC" ] || { rm -f "$TMP"; die "pg_restore --list could not read the dump; partial removed"; }
# Here-strings, not pipes: `printf | grep -q` under `set -o pipefail` fails on a real
# TOC (grep exits on the first match, printf takes SIGPIPE, the pipe reads as false).
MARKERS=0
grep -qE 'TABLE DATA public lead_import_rows ' <<<"$TOC" && MARKERS=$((MARKERS+1))
grep -qE 'TABLE DATA supabase_migrations schema_migrations ' <<<"$TOC" && MARKERS=$((MARKERS+1))
grep -qE 'TABLE DATA public contact_evidence ' <<<"$TOC" && MARKERS=$((MARKERS+1))
if [ "$MARKERS" -lt 3 ]; then
  rm -f "$TMP"; die "dump missing expected production markers (found $MARKERS/3); partial removed"
fi

SHA="$(shasum -a 256 "$TMP" | awk '{print $1}')"
mv -f "$TMP" "$OUT"
printf '%s  %s\n' "$SHA" "$(basename "$OUT")" > "$OUT.sha256"

SIZE="$(ls -lh "$OUT" | awk '{print $5}')"
COUNT="$(ls -1 "$OUT_DIR"/deedsonar-*.dump 2>/dev/null | wc -l | tr -d ' ')"
FREE="$(df -h "$OUT_DIR" | awk 'NR==2 {print $4}')"
echo "backup_deedsonar_production: OK  file=$SIZE  sha256=$SHA  backups=$COUNT  volume_free=$FREE"
echo "backup_deedsonar_production: NO auto-rotation (AI-never-deletes) — prune old dumps by hand."

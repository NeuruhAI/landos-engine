#!/usr/bin/env bash
# field_pin_isolated_db.sh — throwaway PostGIS database, migrations from ZERO.
#
#   scripts/field_pin_isolated_db.sh up      # start container + bootstrap + migrate 001..NNN
#   scripts/field_pin_isolated_db.sh url     # print the connection URL
#   scripts/field_pin_isolated_db.sh down    # remove ONLY our own container
#
# SAFETY DOCTRINE (this script can never touch production):
#   * it only ever connects to the container IT started (fixed name + loopback
#     port); it never reads DATABASE_URL or .env;
#   * the container is postgis/postgis:16-3.4 on 127.0.0.1:${FIELD_PIN_PG_PORT:-55433}
#     with an ephemeral password — data dies with the container;
#   * `down` removes only the named container; nothing else in the docker
#     daemon (the P2-B qdrant/redis containers are never listed, never touched).
#
# The bootstrap creates what Supabase provides out-of-band so the repo's
# migrations apply verbatim from zero:
#   roles anon / authenticated / service_role (nologin), schema landos,
#   schema extensions, schema auth + auth.uid() shim, app_current_role() shim,
#   and the CRM-owned base table public.acquisition_leads (the engine's
#   migrations ALTER it and RPCs read it; the CRM repo owns its DDL).
set -euo pipefail

NAME="landos-fieldpin-isolated-pg"
PORT="${FIELD_PIN_PG_PORT:-55433}"
IMAGE="postgis/postgis:16-3.4"
PASS="fieldpin-isolated-$$"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
URL_FILE="/tmp/${NAME}.url"

url() { cat "$URL_FILE"; }

up() {
  if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    echo "refuse: container $NAME already exists (run down first)" >&2; exit 1
  fi
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "refuse: port $PORT is already in use" >&2; exit 1
  fi

  docker run -d --name "$NAME" \
    -e POSTGRES_PASSWORD="$PASS" -e POSTGRES_DB=landos_isolated_test \
    -p "127.0.0.1:${PORT}:5432" "$IMAGE" >/dev/null

  local dburl="postgresql://postgres:${PASS}@127.0.0.1:${PORT}/landos_isolated_test"
  echo "$dburl" > "$URL_FILE"; chmod 600 "$URL_FILE"

  # Readiness = the FINAL server answers over the HOST-published TCP port.
  # (The docker-entrypoint init phase runs a socket-only temp server that
  # pg_isready inside the container would happily report as ready; under
  # arm64->amd64 emulation that phase alone can take a couple of minutes.)
  echo "waiting for postgres (host TCP) ..."
  ready=0
  for _ in $(seq 1 180); do
    if psql "$dburl" -qAt -c 'select 1' >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
  done
  if [ "$ready" != 1 ]; then
    echo "refuse: postgres never became ready on 127.0.0.1:${PORT}" >&2
    docker logs --tail 20 "$NAME" >&2 || true
    exit 1
  fi

  echo "bootstrap (supabase-provided objects) ..."
  psql "$dburl" -v ON_ERROR_STOP=1 -q <<'SQL'
-- Roles Supabase pre-creates. nologin: they exist for GRANT/RLS targets only.
do $$ begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then create role anon nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then create role authenticated nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then create role service_role nologin bypassrls; end if;
  -- deployed engine writer role (038 grants to it explicitly)
  if not exists (select 1 from pg_roles where rolname = 'contabo_engine') then create role contabo_engine nologin; end if;
  if not exists (select 1 from pg_roles where rolname = 'contabo_engine_ro') then create role contabo_engine_ro nologin; end if;
end $$;

create schema if not exists landos;
create schema if not exists extensions;

-- Supabase auth shim: policies reference auth.uid(); in isolation it is null.
create schema if not exists auth;
create or replace function auth.uid() returns uuid language sql stable as $$ select null::uuid $$;

-- CRM-owned helpers referenced by RLS policies (CRM migrations define the real ones).
create or replace function public.app_current_role() returns text language sql stable as $$ select null::text $$;
create or replace function public.app_current_tenant() returns uuid language sql stable as $$ select null::uuid $$;

-- CRM-owned auth profile table (010 FKs acknowledged_by to it).
create table if not exists public.profiles (
  id         uuid primary key default gen_random_uuid(),
  role       text,
  tenant_id  uuid,
  created_at timestamptz not null default now()
);

-- CRM-owned base table: the engine's migrations ALTER it (006/013/014/015/037)
-- and fn_resolve_field_pin reads it. Base shape per the CRM's DDL — every
-- column below is CRM-owned (columns the ENGINE adds are left to migrations).
create table if not exists public.acquisition_leads (
  lead_id          uuid primary key default gen_random_uuid(),
  source_lead_id   uuid,
  tenant_id        uuid,
  county_fips      text,
  parcel_apn       text,
  owner_name       text,
  situs_address    text,
  final_lead_score numeric,
  status           text default 'new',
  stage            text,
  card_state       text,
  assigned_to      uuid,
  assigned_at      timestamptz,
  assigned_by      uuid,
  dnc              boolean,
  call_status      text,
  last_call_at     timestamptz,
  call_notes       text,
  asset_class      text,
  pipeline_slug    text,
  signal_tags      text[],
  source           text,
  market_value_est numeric,
  equity_pct       numeric,
  taxes_annual     numeric,
  created_at       timestamptz not null default now()
);

-- CRM-owned comms compliance tables (037's callable_leads view reads them).
create schema if not exists comms;
create table if not exists comms.opt_outs (
  id         uuid primary key default gen_random_uuid(),
  lead_id    uuid,
  channel    text,
  created_at timestamptz not null default now()
);
create table if not exists comms.consent (
  consent_id     uuid primary key default gen_random_uuid(),
  lead_id        uuid,
  channel        text,
  consent_status text,
  captured_at    timestamptz,
  created_at     timestamptz not null default now()
);

-- Supabase sets this per-database; migrations rely on unqualified ST_*/geometry.
alter database landos_isolated_test set search_path = "$user", public, extensions;
SQL

  echo "migrations from zero ..."
  for f in "$ROOT"/migrations/*.sql; do
    echo "  apply $(basename "$f")"
    psql "$dburl" -v ON_ERROR_STOP=1 -q -1 -f "$f"
  done
  echo "up: $dburl"
}

down() {
  if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    docker rm -f "$NAME" >/dev/null
    echo "down: removed $NAME"
  else
    echo "down: $NAME not present"
  fi
  rm -f "$URL_FILE"
}

case "${1:-}" in
  up) up ;;
  url) url ;;
  down) down ;;
  *) echo "usage: $0 up|url|down" >&2; exit 2 ;;
esac

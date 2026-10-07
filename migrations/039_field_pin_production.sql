-- 039_field_pin_production.sql — FIELD PIN PRODUCTION V1 (additive only).
--
-- Productionizes the 024/025 field-pin capture + ray resolver WITHOUT touching
-- either reviewed object: fn_resolve_field_pin's ladder (ray_intersect 0.90 ->
-- gated nearest_parcel 0.50 -> honest none) is unchanged; this migration adds
-- the capture-side production contract around it:
--
--   1. idempotency_key       — one capture per (tenant, key); replay returns the
--                              SAME pin, never a duplicate row.
--   2. duplicate_of_pin_id   — near-duplicate captures (same tenant+device+
--                              category, close in time and space, DIFFERENT key)
--                              are STORED (never dropped) but linked to the
--                              original so downstream review sees one sighting.
--   3. landos.shoreline_geom — the authoritative-shoreline layer for the Lake
--                              Michigan proximity/corridor lane. EMPTY at apply
--                              time: proximity is computable ONLY when an
--                              authoritative source is loaded here with
--                              provenance. No table rows -> the lane reports
--                              UNWIRED. Never approximated from parcel data.
--   4. landos.fn_field_pin_lake_michigan — proximity/corridor read RPC that is
--                              honest about wiring: UNWIRED when no authoritative
--                              shoreline geometry exists, UNAVAILABLE when it
--                              exists but the pin's basis geometry cannot be
--                              established, OK (with basis + provenance) only
--                              when the distance is genuinely computable.
--
-- NO lead minting anywhere in this migration: nothing here writes
-- acquisition_leads / owner_leads, and the new function is read-only over
-- shoreline_geom + field_pins + parcel_geom.
--
-- RLS posture identical to 020/022/024: deny-all (no policies), service_role
-- bypasses; functions EXECUTE service_role only.

-- 1) idempotency ---------------------------------------------------------------
alter table landos.field_pins add column if not exists idempotency_key text;
alter table landos.field_pins add column if not exists duplicate_of_pin_id uuid
  references landos.field_pins(pin_id);

create unique index if not exists field_pins_idem_uq
  on landos.field_pins (tenant_id, idempotency_key)
  where idempotency_key is not null;

-- duplicate-capture window scan (same tenant+device+category, recent first)
create index if not exists field_pins_dupe_scan_idx
  on landos.field_pins (tenant_id, device_id, category, observed_at desc);

-- 2) authoritative shoreline layer --------------------------------------------
create table if not exists landos.shoreline_geom (
  id            uuid primary key default gen_random_uuid(),
  waterbody     text not null,                          -- 'lake_michigan' etc.
  geom          geometry(MultiLineString, 4326) not null,
  source        text not null,                          -- e.g. 'noaa:medium_res_shoreline'
  source_ref    text,                                   -- dataset id / file sha — which pull
  authoritative boolean not null default false,         -- ONLY true rows feed proximity
  ingested_at   timestamptz not null default now()
);

create index if not exists shoreline_geom_gix on landos.shoreline_geom using gist (geom);
create index if not exists shoreline_geom_wb_idx on landos.shoreline_geom (waterbody, authoritative);

alter table landos.shoreline_geom enable row level security;
-- RLS deny-all: no policy (same posture as parcel_geom / parcel_owner / field_pins).

-- 3) Lake Michigan proximity/corridor ------------------------------------------
-- Corridor bands (documented contract, one place):
--   shore_0_500m      dist <= 500
--   corridor_500m_1mi dist <= 1609.344
--   corridor_1mi_5mi  dist <= 8046.72
--   outside_corridor  otherwise
create or replace function landos.fn_field_pin_lake_michigan(p_pin_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = landos, public, extensions, pg_temp
as $func$
declare
  pin        landos.field_pins%rowtype;
  basis_geom geometry;
  basis      text;
  dist_m     numeric;
  src        text;
begin
  select * into pin from landos.field_pins where pin_id = p_pin_id;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'pin_not_found');
  end if;

  -- Wiring gate: proximity is only ever computed against AUTHORITATIVE shoreline
  -- geometry. No such rows -> UNWIRED, with the reason spelled out. Non-
  -- authoritative rows never count (an unreviewed trace is not a shoreline).
  if not exists (
    select 1 from landos.shoreline_geom s
    where s.waterbody = 'lake_michigan' and s.authoritative
  ) then
    return jsonb_build_object(
      'ok', true, 'status', 'UNWIRED',
      'reason', 'no authoritative lake_michigan shoreline geometry loaded');
  end if;

  -- Basis: the resolved parcel's boundary when the pin resolved and its geometry
  -- exists; otherwise the pin's own observation origin. Both are stated, never
  -- silently substituted.
  if pin.resolved_apn is not null and pin.resolved_county_fips is not null then
    select g.geom into basis_geom
    from landos.parcel_geom g
    where g.county_fips = pin.resolved_county_fips
      and g.parcel_apn = pin.resolved_apn;
    if found then
      basis := 'resolved_parcel';
    end if;
  end if;
  if basis_geom is null then
    basis_geom := pin.origin;
    basis := 'pin_origin';
  end if;
  if basis_geom is null then
    -- origin is NOT NULL by schema, so this is defensive honesty only.
    return jsonb_build_object(
      'ok', true, 'status', 'UNAVAILABLE',
      'reason', 'no basis geometry for this pin');
  end if;

  -- nearest authoritative segment: its distance AND its provenance travel together
  select st_distance(s.geom::geography, basis_geom::geography), s.source
    into dist_m, src
  from landos.shoreline_geom s
  where s.waterbody = 'lake_michigan' and s.authoritative
  order by st_distance(s.geom::geography, basis_geom::geography) asc
  limit 1;

  if dist_m is null then
    return jsonb_build_object(
      'ok', true, 'status', 'UNAVAILABLE',
      'reason', 'authoritative shoreline present but distance not computable');
  end if;

  return jsonb_build_object(
    'ok', true, 'status', 'OK',
    'waterbody', 'lake_michigan',
    'distance_m', round(dist_m, 1),
    'corridor', case
      when dist_m <= 500      then 'shore_0_500m'
      when dist_m <= 1609.344 then 'corridor_500m_1mi'
      when dist_m <= 8046.72  then 'corridor_1mi_5mi'
      else 'outside_corridor'
    end,
    'basis', basis,
    'source', src);
end;
$func$;

revoke all     on function landos.fn_field_pin_lake_michigan(uuid) from public;
revoke all     on function landos.fn_field_pin_lake_michigan(uuid) from anon;
revoke all     on function landos.fn_field_pin_lake_michigan(uuid) from authenticated;
grant  execute on function landos.fn_field_pin_lake_michigan(uuid) to service_role;

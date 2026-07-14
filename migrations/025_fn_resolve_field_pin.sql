-- 025_fn_resolve_field_pin.sql — SIGNAL-IQ FIELD: ray -> parcel resolver (P3, §5.2).
--
-- Resolves a stored field pin's RAY against landos.parcel_geom, joins
-- landos.parcel_owner for the name, checks public.acquisition_leads for "already
-- in the book", STAMPS the resolution back onto the pin (idempotent — re-running
-- re-resolves, which is how a dark-county pin resolves retroactively once its
-- geometry lands), and returns the jsonb the phone draws.
--
-- Resolution ladder (honest, per §5.2):
--   ray_intersect  (conf 0.90) — first parcel the ray crosses, past a 3 m origin
--                                 buffer (road ROW / own footprint), nearest first.
--   nearest_parcel (conf 0.50) — ONLY when side='unknown' or the ray missed but
--                                 the area HAS geometry: nearest parcel within 60 m
--                                 of `projected`.
--   none           (conf null) — no geometry near the origin (dark county). A
--                                 VALID terminal state, not a failure. Waits for
--                                 coverage.
-- vintage_flag='govt' owners return the flag, NEVER a seller name (vintage law).
--
-- SECURITY DEFINER (reads across RLS'd landos.* + public.acquisition_leads),
-- EXECUTE service_role only. Parcel + owner are public record — sanctioned field
-- read path, not moat. The scoring recipe never crosses it.

create or replace function landos.fn_resolve_field_pin(p_pin_id uuid)
returns jsonb
language plpgsql
volatile
security definer
set search_path = landos, public, extensions, pg_temp
as $func$
declare
  pin           landos.field_pins%rowtype;
  hit_apn       text;
  hit_fips      text;
  hit_geom      geometry;
  method        text;
  conf          numeric;
  near_any      boolean;
  owner_row     landos.parcel_owner%rowtype;
  owner_name    text;
  owner_flag    text;
  acres         numeric;
  crm           jsonb;
begin
  select * into pin from landos.field_pins where pin_id = p_pin_id;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'pin_not_found');
  end if;

  -- 1) RAY INTERSECT — first parcel the ray crosses, past the 3 m origin buffer.
  if pin.ray is not null then
    select g.parcel_apn, g.county_fips, g.geom
      into hit_apn, hit_fips, hit_geom
    from landos.parcel_geom g
    where st_intersects(g.geom, pin.ray)
      and st_distance(g.geom::geography, pin.origin::geography) > 3
    order by st_distance(g.geom::geography, pin.origin::geography) asc
    limit 1;
    if found then
      method := 'ray_intersect';
      conf := 0.90;
    end if;
  end if;

  -- 2) NEAREST fallback — only when side='unknown' or the ray missed, and only
  --    within 60 m of the display projection (so it never guesses wildly).
  if hit_apn is null and (pin.side = 'unknown' or pin.ray is null or method is null)
     and pin.projected is not null then
    select g.parcel_apn, g.county_fips, g.geom
      into hit_apn, hit_fips, hit_geom
    from landos.parcel_geom g
    where st_dwithin(g.geom::geography, pin.projected::geography, 60)
    order by st_distance(g.geom::geography, pin.projected::geography) asc
    limit 1;
    if found then
      method := 'nearest_parcel';
      conf := 0.50;
    end if;
  end if;

  -- 3) NONE — no parcel resolved. Distinguish a dark county (no geometry near the
  --    origin at all) from a genuine miss; both are method 'none' but the note differs.
  if hit_apn is null then
    select exists (
      select 1 from landos.parcel_geom g
      where st_dwithin(g.geom::geography, pin.origin::geography, 2000)
    ) into near_any;
    method := 'none';
    conf := null;
    update landos.field_pins
       set resolved_apn = null, resolved_county_fips = null,
           resolution_method = 'none', resolution_confidence = null,
           resolved_at = now()
     where pin_id = p_pin_id;
    return jsonb_build_object(
      'ok', true, 'apn', null, 'county_fips', null,
      'owner', null, 'owner_flag', null, 'acres', null, 'geometry', null,
      'resolution_method', 'none', 'confidence', null,
      'coverage', case when near_any then 'covered_no_parcel' else 'dark_county' end,
      'in_crm', jsonb_build_object('present', false));
  end if;

  -- resolved: owner (honest-null + vintage law)
  select * into owner_row from landos.parcel_owner
   where county_fips = hit_fips and parcel_apn = hit_apn;
  if not found or coalesce(trim(owner_row.owner_name), '') = '' then
    owner_name := null; owner_flag := 'not_on_file';
  elsif owner_row.vintage_flag = 'govt' then
    owner_name := owner_row.owner_name; owner_flag := 'govt';
  else
    owner_name := owner_row.owner_name; owner_flag := 'private';
  end if;

  acres := round((st_area(hit_geom::geography) * 0.000247105)::numeric, 3);

  -- "in the book" — resolved parcel checked against the CRM board
  select jsonb_build_object(
           'present', true, 'lead_id', a.lead_id, 'stage', a.stage,
           'card_state', a.card_state)
    into crm
  from public.acquisition_leads a
  where a.county_fips = hit_fips and a.parcel_apn = hit_apn
  limit 1;
  if crm is null then
    crm := jsonb_build_object('present', false);
  end if;

  update landos.field_pins
     set resolved_apn = hit_apn, resolved_county_fips = hit_fips,
         resolution_method = method, resolution_confidence = conf,
         resolved_at = now()
   where pin_id = p_pin_id;

  return jsonb_build_object(
    'ok', true,
    'apn', hit_apn,
    'county_fips', hit_fips,
    'owner', coalesce(owner_name, 'not on file'),
    'owner_flag', owner_flag,
    'acres', acres,
    'geometry', st_asgeojson(hit_geom, 6)::jsonb,
    'resolution_method', method,
    'confidence', conf,
    'in_crm', crm);
end;
$func$;

revoke all     on function landos.fn_resolve_field_pin(uuid) from public;
revoke all     on function landos.fn_resolve_field_pin(uuid) from anon;
revoke all     on function landos.fn_resolve_field_pin(uuid) from authenticated;
grant  execute on function landos.fn_resolve_field_pin(uuid) to service_role;

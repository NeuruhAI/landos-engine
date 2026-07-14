-- 023_fn_parcel_plat_owner_join.sql — repoint the plat RPC owner seam (JOB 5.6)
--
-- Supersedes migration 021's owner-less RPC. Lands the operator's plat-fixtures
-- draft VERBATIM (subject Feature + edges/bearings + ST_Touches neighbors cap 12
-- + meta; parcel_not_found -> honest Error, no fabricated features) with exactly
-- one change: the neighbor owner seam joins landos.parcel_owner (the full owner
-- roll, JOB 5.6) instead of the sparse owner_leads, and owner_flag reads the
-- STORED vintage_flag rather than re-deriving the govt pattern (single source of
-- truth = the loader). Shape unchanged; honest-null "not on file" preserved for
-- APNs with no owner row.
--
-- SECURITY: security invoker; EXECUTE service_role ONLY (bypasses RLS).

create or replace function landos.fn_parcel_plat(p_county text, p_apn text)
returns jsonb
language sql
stable
security invoker
-- PostGIS is installed in the `extensions` schema (migration 020) — it MUST be
-- on the pinned search_path or ST_* resolution fails. landos first (our tables),
-- then public, extensions (PostGIS), pg_temp last.
set search_path = landos, public, extensions, pg_temp
as $func$
with s as (
  select g.parcel_apn,
         g.county_fips,
         g.geom,
         coalesce(g.centroid, st_centroid(g.geom)) as centroid
  from landos.parcel_geom g
  where g.county_fips = p_county
    and g.parcel_apn  = p_apn
  limit 1
),
edges as (
  select jsonb_agg(
           jsonb_build_object(
             'len_ft',      round((st_length(seg.geom::geography) * 3.28084)::numeric, 1),
             'bearing_deg', round(degrees(st_azimuth(st_startpoint(seg.geom), st_endpoint(seg.geom)))::numeric, 1)
           ) order by seg.path
         ) as arr
  from s,
       lateral st_dumpsegments(st_exteriorring(st_geometryn(st_multi(s.geom), 1))) seg
),
nbr as (
  select n.parcel_apn, n.geom, po.owner_name, po.vintage_flag
  from s
  join landos.parcel_geom n
    on  n.county_fips = s.county_fips
    and n.parcel_apn <> s.parcel_apn
    and st_touches(s.geom, n.geom)
  left join landos.parcel_owner po
    on  po.county_fips = n.county_fips
    and po.parcel_apn  = n.parcel_apn
  order by n.parcel_apn
  limit 12
),
nbr_feat as (
  select jsonb_agg(
           jsonb_build_object(
             'type', 'Feature',
             'geometry', st_asgeojson(n.geom, 6)::jsonb,
             'properties', jsonb_build_object(
               'kind',  'neighbor',
               'apn',   n.parcel_apn,
               'owner', coalesce(nullif(trim(n.owner_name), ''), 'not on file'),
               -- owner_flag reads the STORED vintage_flag (loader owns the pattern):
               --   govt        -> flagged public entity, never asserted as seller
               --   private     -> on-file private owner
               --   not_on_file -> no owner row (honest-null)
               'owner_flag', case
                 when n.owner_name is null or trim(n.owner_name) = '' then 'not_on_file'
                 when n.vintage_flag = 'govt' then 'govt'
                 else 'private'
               end
             )
           ) order by n.parcel_apn
         ) as arr
  from nbr n
)
select case
  when not exists (select 1 from s) then
    jsonb_build_object(
      'type', 'Error',
      'error', 'parcel_not_found',
      'county_fips', p_county,
      'apn', p_apn
    )
  else
    jsonb_build_object(
      'type', 'FeatureCollection',
      'features',
        jsonb_build_array(
          jsonb_build_object(
            'type', 'Feature',
            'geometry', (select st_asgeojson(s.geom, 6)::jsonb from s),
            'properties', jsonb_build_object(
              'kind',        'subject',
              'apn',         (select parcel_apn from s),
              'county_fips', (select county_fips from s),
              'acres',       (select round((st_area(s.geom::geography) * 0.000247105)::numeric, 3) from s),
              'edges',       coalesce((select arr from edges), '[]'::jsonb)
            )
          )
        ) || coalesce((select arr from nbr_feat), '[]'::jsonb),
      'meta', jsonb_build_object(
        'subject_apn',       p_apn,
        'county_fips',       p_county,
        'srid',              4326,
        'source',            'landos.parcel_geom (geometry) LEFT JOIN landos.parcel_owner (owner + vintage_flag)',
        'neighbor_relation', 'ST_Touches (cap 12)',
        'centroid',          (select st_asgeojson(s.centroid, 6)::jsonb from s),
        'bbox',              (select jsonb_build_array(
                                round(st_xmin(s.geom)::numeric, 6), round(st_ymin(s.geom)::numeric, 6),
                                round(st_xmax(s.geom)::numeric, 6), round(st_ymax(s.geom)::numeric, 6)) from s),
        'dist_to_lake_m',    null  -- ingest-lane overlay; not resolvable from parcel_geom
      )
    )
end;
$func$;

revoke all     on function landos.fn_parcel_plat(text, text) from public;
revoke all     on function landos.fn_parcel_plat(text, text) from anon;
revoke all     on function landos.fn_parcel_plat(text, text) from authenticated;
grant  execute on function landos.fn_parcel_plat(text, text) to service_role;

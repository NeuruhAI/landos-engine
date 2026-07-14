-- 021_fn_parcel_plat.sql — parcel plat read RPC: subject + ST_Touches neighbors
--
-- The sanctioned CRM read path for parcel geometry (public record, not moat):
-- one call returns the subject parcel and every parcel whose boundary touches
-- it, as a GeoJSON FeatureCollection in jsonb. Unknown (county, apn) returns an
-- honest-empty collection — never an error, never a guess.
--
-- ADDITIVE ONLY. Security: EXECUTE revoked from public/anon/authenticated,
-- granted to service_role alone. SECURITY INVOKER (default) on purpose:
-- parcel_geom is RLS-enabled deny-all (020) and service_role carries
-- bypassrls, so no definer escalation is needed or wanted.
--
-- Provenance: operator GO 2026-07-13 (JOB 5.5). The referenced draft file
-- (~/Downloads/plat-fixtures/fn_parcel_plat.sql) was absent at apply time;
-- authored verbatim from the GO spec: subject + ST_Touches neighbors,
-- jsonb FeatureCollection, service-role-only EXECUTE, honest-empty misses.

create or replace function landos.fn_parcel_plat(p_county text, p_apn text)
returns jsonb
language sql
stable
as $$
  with subject as (
    select g.county_fips, g.parcel_apn, g.geom, g.source
    from landos.parcel_geom g
    where g.county_fips = p_county
      and g.parcel_apn = p_apn
  ),
  neighbors as (
    -- && rides the GIST index; ST_Touches confirms a shared boundary
    -- (interiors disjoint) — the plat-book notion of "adjacent".
    select n.county_fips, n.parcel_apn, n.geom, n.source
    from landos.parcel_geom n
    join subject s
      on n.county_fips = s.county_fips
     and n.parcel_apn <> s.parcel_apn
     and n.geom && s.geom
     and ST_Touches(n.geom, s.geom)
  ),
  feats as (
    select jsonb_build_object(
             'type', 'Feature',
             'geometry', ST_AsGeoJSON(u.geom)::jsonb,
             'properties', jsonb_build_object(
               'county_fips', u.county_fips,
               'parcel_apn',  u.parcel_apn,
               'role',        u.role,
               'source',      u.source)
           ) as f
    from (
      select county_fips, parcel_apn, geom, source, 'subject'  as role from subject
      union all
      select county_fips, parcel_apn, geom, source, 'neighbor' as role from neighbors
    ) u
  )
  select jsonb_build_object(
           'type', 'FeatureCollection',
           'features', coalesce(jsonb_agg(f), '[]'::jsonb)
         )
  from feats
$$;

revoke all on function landos.fn_parcel_plat(text, text) from public;
revoke all on function landos.fn_parcel_plat(text, text) from anon;
revoke all on function landos.fn_parcel_plat(text, text) from authenticated;
grant execute on function landos.fn_parcel_plat(text, text) to service_role;

-- 020_geometry_infra.sql — JOB 7 GEOMETRY (2026-07-10): PostGIS + parcel boundary layer.
--
-- Implements docs/geometry_infra_spec.md Parts 1-2. The build trigger fired via
-- operator directive (2026-07-10 mission: clear the infra gate blocking the staged
-- fleet — indiana_warn 1,163 / ground_lease 224 / flood_remap 45, held pending PIP).
-- The spec marked the extension enable operator-fired; this mission IS the operator
-- firing it ("enable postgis via additive migration if absent").
--
-- PostGIS 3.3.7 ships in the Supabase image; `create extension` is the whole install.
-- Installed into the `extensions` schema (Supabase convention) — the database
-- search_path already includes it ("$user", public, extensions), so unqualified
-- geometry types and ST_* resolve everywhere.
--
-- landos.parcel_geom is the spec's handoff contract, verbatim:
--   * join key (county_fips, parcel_apn) — canonical APN form, same as owner_leads
--   * CRS 4326 at rest, MultiPolygon, centroid = ST_PointOnSurface (inside-guaranteed)
--   * provenance (source, source_ref) mandatory; idempotent upsert on the join key
--   * this table EXISTING is the readiness marker the ingest lane detects
--
-- RLS enabled with no policies (deny-all to anon/authenticated) — same posture as
-- 019: all legitimate access is service-role; never re-open the advisory 019 closed.
--
-- Additive-only: new extension + new table, nothing existing touched. Idempotent.

create extension if not exists postgis with schema extensions;

create table if not exists landos.parcel_geom (
  id          uuid primary key default gen_random_uuid(),
  county_fips text not null,                       -- join key half #1 (matches owner_leads)
  parcel_apn  text not null,                       -- join key half #2 (canonical, 71-form etc.)
  geom        geometry(MultiPolygon, 4326) not null,
  centroid    geometry(Point, 4326),               -- ST_PointOnSurface(geom); cheap PIP prefilter
  source      text not null,                       -- 'arcgis:stjoseph_parcels' etc. (provenance)
  source_ref  text,                                -- county OBJECTID / GlobalID — idempotency
  ingested_at timestamptz not null default now(),
  unique (county_fips, parcel_apn)                 -- one boundary per parcel; re-run upserts
);

create index if not exists parcel_geom_gix        on landos.parcel_geom using gist (geom);
create index if not exists parcel_geom_centroidix on landos.parcel_geom using gist (centroid);
create index if not exists parcel_geom_apn_idx    on landos.parcel_geom (county_fips, parcel_apn);

alter table landos.parcel_geom enable row level security;

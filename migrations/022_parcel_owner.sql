-- 022_parcel_owner.sql — parcel owner-of-record layer (additive, JOB 5.6)
--
-- Owner names + mailings for every parcel we can source, keyed identically to
-- landos.parcel_geom so the plat RPC joins geometry -> owner with zero fuzzy
-- matching. This is the table that moves the owner set OUT of a loose CSV and
-- INTO the database (the bug 5.6 fixes): when St. Joseph and future counties
-- land geometry, their owners load here through the same idiom.
--
-- VINTAGE-CAPTURE LAW: government / treasurer / public-entity owner names carry
-- vintage_flag='govt' — surfaced as a flag, NEVER asserted as a seller. A
-- private on-file owner has vintage_flag NULL. The flag is computed once at load
-- (scripts/load_parcel_owner.py) so the pattern lives in exactly one place.
--
-- Firewall: landos.* own-schema, additive, idempotent on the PK. RLS enabled
-- deny-all (no policy) mirroring parcel_geom; service_role bypasses RLS.

create table if not exists landos.parcel_owner (
  county_fips   text not null,                 -- join key half #1 (matches parcel_geom / owner_leads)
  parcel_apn    text not null,                 -- join key half #2 (canonical county APN)
  owner_name    text,                          -- owner of record (may be blank -> honest not-on-file)
  owner_mailing text,                          -- assembled mailing (addr, city, state zip)
  source        text not null,                 -- provenance ('assemblage:laporte_scored.csv' etc.)
  source_ref    text,                          -- batch ref (source CSV sha256) — which pull
  vintage_flag  text,                          -- 'govt' = treasurer/public-entity pattern (flagged, never seller); null = private
  ingested_at   timestamptz not null default now(),
  primary key (county_fips, parcel_apn)        -- the PK btree IS the (county_fips, parcel_apn) index
);

alter table landos.parcel_owner enable row level security;
-- RLS deny-all: no policy created (same posture as landos.parcel_geom).
-- service_role carries bypassrls, so the engine + fn_parcel_plat read freely.

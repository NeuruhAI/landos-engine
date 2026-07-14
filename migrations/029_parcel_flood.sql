-- 029_parcel_flood.sql — W0 JOB 4: FLOOD is a LENS, not an attribute (reversed).
--
-- Persist the SFHA-crossed parcels (the ~8,225 from cross_flood_sfha) as a
-- per-parcel FLOOD-lens candidate set. flood_remap is DISCOVERY/FLOOD in the
-- registry (mig 027); this is the parcel surface the flood mint reads. Its exits
-- are its own: HMGP/FMA buyout (a FUNDED buyer), elevate-resell, land-value,
-- conservation — never the same recipe as DISTRESS.
--
-- Additive, landos.* own-schema, PK (county_fips, parcel_apn), RLS deny-all.
-- Populated by scripts/load_parcel_flood.py (parcel_geom x flood_sfha overlays).

create table if not exists landos.parcel_flood (
  county_fips  text not null,
  parcel_apn   text not null,
  in_sfha      boolean not null default true,
  flood_zone   text,                    -- FEMA FLD_ZONE(s) the parcel intersects (AE, A, X, ...)
  source       text not null,
  computed_at  timestamptz not null default now(),
  primary key (county_fips, parcel_apn)
);

alter table landos.parcel_flood enable row level security;
-- RLS deny-all; service_role bypasses.

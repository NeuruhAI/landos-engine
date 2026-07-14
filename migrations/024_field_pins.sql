-- 024_field_pins.sql — SIGNAL-IQ FIELD: the windshield-sensor capture table (P3).
--
-- A field pin is a RAY, not a point: "from HERE, on THIS heading, at THIS speed,
-- I saw something on my RIGHT, at THIS moment." The phone stores intent + honesty;
-- the spine (fn_resolve_field_pin) resolves the ray to a parcel. The phone works
-- with zero parcel data — an unresolved ray is a VALID terminal state that
-- resolves retroactively when geometry lands. A field pin is a SIGNAL (not a
-- candidate, not a lead) until it mints and a human validates it.
--
-- Additive, landos.* own-schema. RLS on (the lead_outcomes lesson): deny-all,
-- service_role bypasses. Two columns exist purely to keep the system honest and
-- may never be dropped: heading_source (a compass pin is never mistaken for a
-- gps_course pin) and resolution_method (a nearest_parcel guess is never mistaken
-- for a ray_intersect hit).

create table if not exists landos.field_pins (
  pin_id                uuid primary key default gen_random_uuid(),
  tenant_id             uuid not null,
  device_id             text not null,                       -- which phone; VA attribution later
  operator_label        text,
  -- what was observed
  recorded_at           timestamptz not null default now(),  -- utterance/tap fired
  observed_at           timestamptz not null,                -- back-dated sighting moment (§3.3)
  origin                geometry(Point, 4326) not null,      -- interpolated fix at observed_at
  origin_accuracy_m     numeric,
  heading_deg           numeric,
  heading_source        text not null check (heading_source in ('gps_course','fix_delta','compass','unknown')),
  speed_mps             numeric,
  side                  text not null check (side in ('left','right','ahead','at','unknown')),
  assumed_offset_m      numeric not null default 25,
  projected             geometry(Point, 4326),               -- phone-side dot (display only)
  ray                   geometry(LineString, 4326),          -- origin -> 150 m; THIS is what resolves
  -- what the human said
  category              text not null,
  spoken_text           text,
  audio_url             text,
  photo_url             text,
  -- what the spine concluded
  resolved_county_fips  text,
  resolved_apn          text,
  resolution_method     text check (resolution_method in ('ray_intersect','nearest_parcel','none')),
  resolution_confidence numeric,
  resolved_at           timestamptz,
  created_at            timestamptz not null default now()
);

create index if not exists field_pins_origin_gix on landos.field_pins using gist (origin);
create index if not exists field_pins_ray_gix    on landos.field_pins using gist (ray);
create index if not exists field_pins_apn_idx    on landos.field_pins (resolved_county_fips, resolved_apn);
create index if not exists field_pins_unresolved on landos.field_pins (created_at) where resolved_apn is null;

alter table landos.field_pins enable row level security;
-- RLS deny-all: no policy (same posture as parcel_geom / parcel_owner / lead_outcomes).
-- service_role carries bypassrls; fn_resolve_field_pin is SECURITY DEFINER.

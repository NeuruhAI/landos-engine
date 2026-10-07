-- 035_parcel_sale.sql — VALUATION LAB: the arms-length SALES pool that grounds land comps.
--
-- WHY a new table (not landos.comps): landos.comps is subject_apn-keyed (each row is a comp
-- ATTACHED to a specific subject) — a county sales POOL has no subject, which is exactly why
-- a county+acreage-band selector over landos.comps reads empty. parcel_sale is the pool: one
-- row per recorded sale, selected at query time by (county_fips, acreage band, land class).
--
-- Public record, tenant-agnostic (MI assessment/sales are public). RLS deny-all posture, same
-- as landos.parcel_geom / parcel_owner (service_role bypasses; the engine is the only reader).
-- MISSING stays NULL, never 0 — a wrong number is worse than no number.

create table if not exists landos.parcel_sale (
  id            bigint generated always as identity primary key,
  tenant_id     uuid,                       -- NULL = public record (tenant-agnostic)
  county_fips   text        not null,
  parcel_apn    text        not null,       -- the SOLD parcel (county-native form)
  sale_price    numeric,
  sale_date     date,
  acreage       numeric,
  class_code    text,                       -- MI STC property class (x01 improved / x02 vacant)
  sev           numeric,                    -- State Equalized Value (MI assesses ~50% of TCV)
  taxable_value numeric,
  deed_liber    text,
  deed_page     text,
  owner_name    text,
  source        text        not null,
  provenance    jsonb,
  created_at    timestamptz not null default now(),
  -- idempotent load key: one row per (parcel, date, price). NULLS NOT DISTINCT so a
  -- re-run with a null date/price still upserts to the same row rather than duplicating.
  constraint parcel_sale_natural_key
    unique nulls not distinct (county_fips, parcel_apn, sale_date, sale_price)
);

-- the selector's access path: pool by county, band by acreage, filter by land class.
create index if not exists parcel_sale_county_acre_idx
  on landos.parcel_sale (county_fips, acreage);
create index if not exists parcel_sale_county_class_idx
  on landos.parcel_sale (county_fips, class_code);

alter table landos.parcel_sale enable row level security;  -- deny-all; service_role bypasses

comment on table landos.parcel_sale is
  'Arms-length recorded-sale pool for land comps (Valuation Lab). Selected by '
  '(county_fips, acreage band, vacant land class) — NOT by subject_apn. Public record, '
  'tenant-agnostic. Loaded by scripts/load_parcel_sales.py. See migration 035.';

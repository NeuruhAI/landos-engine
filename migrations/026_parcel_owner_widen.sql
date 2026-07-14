-- 026_parcel_owner_widen.sql — widen parcel_owner for the owner-roll mines (W0).
--
-- Zero new data source: the columns below already exist in the SEALED assemblage
-- CSV (laporte_scored.csv, sha 48b3b0b2) that seeded parcel_owner — JOB 5.6 only
-- loaded owner_name + owner_mailing. This backfills the rest so the mine views
-- (absentee / entity / exempt / same-owner-adjacent) run as data, not app logic.
-- Additive columns only; the loader reloads with ON CONFLICT DO UPDATE.
--
-- HARD GAPS (named, not faked): year_built and assessed_value are NOT in the
-- source CSV at all — so the teardown mine (old + low value + acreage) stays
-- BLOCKED, and class-code plays are blind (property_class here is only 529/530/0
-- = residential/unknown, no commercial subtypes). Both are honest gaps.

alter table landos.parcel_owner add column if not exists situs_address        text;
alter table landos.parcel_owner add column if not exists owner_mailing_city    text;
alter table landos.parcel_owner add column if not exists owner_mailing_state   text;
alter table landos.parcel_owner add column if not exists owner_mailing_zip     text;
alter table landos.parcel_owner add column if not exists property_class        text;   -- DLGF-ish class (this CSV: 529/530/0)
alter table landos.parcel_owner add column if not exists acres                 numeric;
alter table landos.parcel_owner add column if not exists owner_kind            text;    -- human | entity (recipe's entity sweep)
alter table landos.parcel_owner add column if not exists owner_occupied_proxy  integer;
alter table landos.parcel_owner add column if not exists last_sale_date        date;
alter table landos.parcel_owner add column if not exists tenure_floor          numeric;

create index if not exists parcel_owner_mailstate_idx on landos.parcel_owner (county_fips, owner_mailing_state);
create index if not exists parcel_owner_kind_idx      on landos.parcel_owner (county_fips, owner_kind);
create index if not exists parcel_owner_class_idx     on landos.parcel_owner (county_fips, property_class);

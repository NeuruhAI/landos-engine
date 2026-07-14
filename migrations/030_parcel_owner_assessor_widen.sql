-- 030_parcel_owner_assessor_widen.sql — widen parcel_owner with the assessor detail the
-- multi-county rolls carry and LaPorte did not: year_built + land/improvement/total
-- assessed + last_sale_price. THIS is what unblocks the two mines mig 026 named as HARD
-- GAPS:
--   * teardown   — old year_built + improvement_assessed ~0 relative to land (the structure
--                  is tear-down grade; the value is the dirt).
--   * class-code — real DLGF commercial subtypes now land (Vanderburgh property_class is
--                  the true 3-digit code, not LaPorte's 529/530/0), so lodging / self-storage
--                  / golf (the motel play) become keyable.
--
-- Additive only; the loader reloads ON CONFLICT DO UPDATE. Honest NULL where a county's roll
-- omits a field — LaPorte stays year_built/assessed NULL (its source never had them),
-- Madison/Hendricks are owner-dark entirely. Nothing is fabricated.

alter table landos.parcel_owner add column if not exists year_built            integer;
alter table landos.parcel_owner add column if not exists land_assessed         numeric;
alter table landos.parcel_owner add column if not exists improvement_assessed  numeric;
alter table landos.parcel_owner add column if not exists total_assessed        numeric;
alter table landos.parcel_owner add column if not exists last_sale_price        numeric;

create index if not exists parcel_owner_yearbuilt_idx on landos.parcel_owner (county_fips, year_built);

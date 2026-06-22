-- 014_acquisition_leads_parcel_meta.sql — AXON Emergence Loop, step 4c (county deal-fields on the card).
--
-- Carries the curated county tax-sale parcel data (assessed value, tax owed, prop type, legal
-- desc, township, status, county URLs) from landos.owner_leads.raw_meta onto the CRM so the
-- /map card shows the actual deal instead of just a score + "address pending". One jsonb column;
-- sync_to_crm.py fills it for the 'discovered' tier only (the curated 136 stay NULL).
--
-- ADDITIVE + REVERSIBLE. Nullable, NO default — touches no existing row, no other column.
-- (amount_owed + assessed_total could later be promoted to TYPED columns if discovered leads
-- need DB-side ranking/sorting by the arbitrage spread; jsonb gets it on the card fastest now.)
--
-- DOWN-MIGRATION:
--   alter table public.acquisition_leads drop column if exists parcel_meta;

alter table public.acquisition_leads
    add column if not exists parcel_meta jsonb;

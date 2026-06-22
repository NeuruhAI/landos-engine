-- 014_acquisition_leads_parcel_meta.sql — AXON Emergence Loop, step 4c (county deal-fields on the card).
--
-- Carries the curated county tax-sale parcel data (assessed value, tax owed, prop type, legal
-- desc, township, status, county URLs) from landos.owner_leads.raw_meta onto the CRM so the
-- /map card shows the actual deal instead of just a score + "address pending". One jsonb column;
-- sync_to_crm.py fills it for the 'discovered' tier only (the curated 136 stay NULL).
--
-- ADDITIVE + REVERSIBLE. Nullable, NO default — touches no existing row, no other column.
-- parcel_meta (jsonb) carries the full curated payload for the card render; amount_owed and
-- assessed_total are ALSO promoted to TYPED numeric columns so the discovered tier can be
-- ORDER BY'd in-DB by arbitrage spread (assessed_total - amount_owed). Both stay inside
-- parcel_meta too (the card reads one object).
--
-- DOWN-MIGRATION (drops all three):
--   alter table public.acquisition_leads drop column if exists amount_owed;
--   alter table public.acquisition_leads drop column if exists assessed_total;
--   alter table public.acquisition_leads drop column if exists parcel_meta;

alter table public.acquisition_leads
    add column if not exists parcel_meta jsonb;
alter table public.acquisition_leads
    add column if not exists amount_owed numeric;
alter table public.acquisition_leads
    add column if not exists assessed_total numeric;

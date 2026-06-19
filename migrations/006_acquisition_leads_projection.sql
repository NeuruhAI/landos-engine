-- 006_acquisition_leads_projection.sql — P4 projection columns (D1 v2 §6 + follow-up).
--
-- ADDITIVE ONLY, on public.acquisition_leads — the dialer's table, defined
-- minimally by the CRM's 010_auth_profiles_rls.sql ("full landos->acquisition
-- sync is a separate task" — this is that task). The engine's sync_to_crm.py
-- is the SOLE writer of every column added here (Q2 single-writer guarantee);
-- the CRM only reads them. No existing column is altered; RLS policies are
-- row-level, so existing CRM queries and the dialer.test account keep working
-- unchanged.
--
-- Contact layer (only ever populated for CALLABLE rows:
--   skip_trace_status in traced_web|traced_paid AND confidence >= 0.6):
--   phone_1 / phone_1_type / phone_2 / contact_confidence
--
-- Pierce/recipe layer (populated for any row the resolver has enriched):
--   outreach_recipe       R1..R5 (the Outreach Recipe Framework stamp)
--   controlling_human     who actually controls an entity owner (SOP-2 pierce)
--   commercial_agent_flag registry agent is CT Corp/CSC/etc -> pierce needed
--   portfolio_hub         "<Name> — hub: N parcels" when one controlling human
--                         spans 2+ parcels — the caller sees the portfolio play
--
-- Map layer (§8 lane, recommended path: projection carries coordinates so the
-- CRM /map page never needs engine credentials):
--   situs_lat / situs_lng

alter table public.acquisition_leads
    add column if not exists phone_1               text,
    add column if not exists phone_1_type          text,
    add column if not exists phone_2               text,
    add column if not exists contact_confidence    numeric,
    add column if not exists outreach_recipe       text,
    add column if not exists controlling_human     text,
    add column if not exists commercial_agent_flag boolean,
    add column if not exists portfolio_hub         text,
    add column if not exists situs_lat             numeric,
    add column if not exists situs_lng             numeric;

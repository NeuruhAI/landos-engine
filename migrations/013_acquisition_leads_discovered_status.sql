-- 013_acquisition_leads_discovered_status.sql — AXON Emergence Loop, step 4 (the 'discovered' tier).
--
-- The no-spend minter (scripts/mint_taxsale_leads.py) lands fresh county tax-sale leads in
-- public.acquisition_leads under status='discovered' so they read as NEW INTAKE, visibly
-- separate from the curated set — not blended into 'new'. The existing status CHECK only
-- allowed (new, contacted, qualified, dead); this ADDS 'discovered'.
--
-- ADDITIVE + REVERSIBLE. Extends an allowed-value set only — touches no existing row, no other
-- column, no other status. (NB: the CRM frontend must learn 'discovered' to surface the tier;
-- the rows are valid + queryable regardless.)
--
-- DOWN-MIGRATION (retire the discovered rows FIRST, or the narrower CHECK won't validate):
--   delete from public.acquisition_leads where status='discovered';   -- or retier them
--   alter table public.acquisition_leads drop constraint if exists acquisition_leads_status_chk;
--   alter table public.acquisition_leads add constraint acquisition_leads_status_chk
--     check (status = any (array['new','contacted','qualified','dead']));

alter table public.acquisition_leads
    drop constraint if exists acquisition_leads_status_chk;
alter table public.acquisition_leads
    add constraint acquisition_leads_status_chk
    check (status = any (array['new','contacted','qualified','dead','discovered']));

-- 008_acquisition_leads_jarvis_brief.sql — AXON Lane 1: the Jarvis brief column.
--
-- ADDITIVE, public.acquisition_leads. An engine-COMPOSED per-lead structured brief
-- (jsonb) — the story behind the score (controlling human, recipe, the composite
-- axes labelled AS the composite, contamination/BEA, alt_use_fitness, kin, a
-- template headline; narrative=null until the on-demand LLM step). Projected by
-- scripts/sync_to_crm.py (the SOLE writer into acquisition_leads); the CRM only
-- displays it. Parity record — the live column may already exist (idempotent).
-- final_lead_score (absentee/reachability rank) is untouched and never conflated.

alter table public.acquisition_leads
    add column if not exists jarvis_brief jsonb;

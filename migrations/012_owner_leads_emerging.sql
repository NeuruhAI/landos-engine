-- 012_owner_leads_emerging.sql — AXON Emergence Loop, step 2 (the emergence scorer's columns).
--
-- Adds the emergence axis to landos.owner_leads so scripts/score_emergence.py can write a
-- decayed, auditable emerging_score from TEMPORAL discovery_signals. Mirrors the columns
-- that already exist on public.acquisition_leads (the projection target), so sync_to_crm.py
-- carries them across with no shape change.
--
-- ADDITIVE + REVERSIBLE. landos.* only. Two nullable columns, NO default.
--   emerging_score   numeric  — bounded 0-100 emergence rank, an INDEPENDENT axis. It does
--                               NOT read, write, or relabel final_lead_score (the absentee /
--                               reachability rank) or the composite.
--   emerging_signals jsonb    — the per-signal "show your work": one object per contributing
--                               temporal signal (weight, freshness, confidence, contribution),
--                               so a human can see exactly why a parcel is emerging.
--
-- FAIL-SAFE: NULL = "not computed", never a fabricated 0. A lead with no qualifying temporal
-- signal stays NULL. Touches no other column, not the composite, not the 136's scores, not
-- any structural signal.
--
-- DOWN-MIGRATION:
--   alter table landos.owner_leads drop column if exists emerging_signals;
--   alter table landos.owner_leads drop column if exists emerging_score;

alter table landos.owner_leads
    add column if not exists emerging_score numeric;
alter table landos.owner_leads
    add column if not exists emerging_signals jsonb;

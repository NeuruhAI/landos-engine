-- 019_lead_outcomes_rls.sql — RLS HARDENING (2026-07-10): close the anon surface.
--
-- Supabase advisory: landos.lead_outcomes was the ONLY table in the project with
-- RLS disabled — the anon key could read AND write the disposition learning loop
-- that feeds the Job 8 scorer modifier.
--
-- Posture: enable RLS with NO policies = deny-all to anon/authenticated. This is
-- deliberate, not an omission. Every legitimate reader/writer of this table runs
-- service-role (scripts/read_lead_outcomes.py, scripts/score_composite_batch.py),
-- which bypasses RLS. The CRM never touches landos.* directly (firewall doctrine,
-- re-proven 2026-07-10: zero client-side references across apg-land-crm @041fbb2).
--
-- Additive-only: no data touched, no roles altered, idempotent on re-apply.

alter table landos.lead_outcomes enable row level security;

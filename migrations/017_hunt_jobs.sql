-- 017_hunt_jobs.sql — HUNT-V2 §1 (2026-07-05): net-new territory hunt jobs.
--
-- The national-expansion doctrine (census -> stage -> dry-run -> HUMAN-FIRED
-- mint) as a job record. AUTONOMY DOCTRINE HOLDS ABSOLUTELY: the system
-- proposes (worker runs census/stage/score/dry-run and parks the job at
-- awaiting_operator_mint); the OPERATOR fires every mint (app.hunt_mint,
-- typed GO). $0 sources only in this version.
--
-- Statuses (lowercase in storage; the directive's AWAITING_OPERATOR_MINT is
-- awaiting_operator_mint here, matching every other status domain in landos):
--   queued -> censusing -> staging -> scoring -> awaiting_operator_mint
--          -> minting -> synced          (happy path)
--   -> uncovered (no $0 source anywhere in the territory; reason per county)
--   -> failed    (error text preserved)
--
-- counts jsonb:   per-stage per-county numbers, e.g.
--                 {"census": {...}, "staged": {...}, "would_mint": {...}}
-- receipts jsonb: append-only array of per-stage receipts, each sha256-hashed
--                 over its canonical body and CHAINED on the previous hash
--                 (the /lead-receipt idiom, app/receipt.py section_hash).

create table if not exists landos.hunt_jobs (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null,
    requested_by text,
    territory    jsonb not null,                       -- [{state, county, fips}]
    recipe       jsonb not null default '{}'::jsonb,   -- {signal_types, thresholds}
    status       text  not null default 'queued'
        check (status in ('queued','censusing','staging','scoring',
                          'awaiting_operator_mint','minting','synced',
                          'uncovered','failed')),
    counts       jsonb not null default '{}'::jsonb,
    receipts     jsonb not null default '[]'::jsonb,
    error        text,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now()
);

create index if not exists hunt_jobs_status_idx
    on landos.hunt_jobs (status, created_at);

-- Engine-internal: NEVER reachable over the CRM's PostgREST (the landos<->CRM
-- firewall, same pattern as 009). Engine connects as table owner (bypasses RLS).
alter table landos.hunt_jobs enable row level security;
drop policy if exists hunt_jobs_service_only on landos.hunt_jobs;
create policy hunt_jobs_service_only on landos.hunt_jobs
    as permissive for all to service_role using (true) with check (true);

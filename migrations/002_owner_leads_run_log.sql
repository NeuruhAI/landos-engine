-- 002_owner_leads_run_log.sql — append-only audit trail of lead-list runs.
--
-- One row per scripts/owner_leads.py run: ties each produced ranked CSV to the
-- exact code/config version and a sha256 of the file bytes. NEVER overwritten —
-- append-only (no unique upsert key on run content; run_id is just identity).
-- landos schema only. RLS on, authenticated policy mirrors overlay_polygons.

create table if not exists landos.owner_leads_run_log (
    id                  uuid primary key default gen_random_uuid(),
    tenant_id           uuid,
    run_id              uuid not null,
    generated_at_utc    timestamptz not null,
    county_fips         text,
    git_commit          text,
    scorer_version      text,
    row_count           integer not null default 0,
    csv_sha256          text,
    manifest_path       text,
    created_at          timestamptz not null default now()
);

create index if not exists owner_leads_run_log_county_time_idx
    on landos.owner_leads_run_log (county_fips, generated_at_utc desc);

alter table landos.owner_leads_run_log enable row level security;

drop policy if exists owner_leads_run_log_authenticated_policy on landos.owner_leads_run_log;
create policy owner_leads_run_log_authenticated_policy
    on landos.owner_leads_run_log
    as permissive
    for all
    to authenticated
    using (true)
    with check (true);

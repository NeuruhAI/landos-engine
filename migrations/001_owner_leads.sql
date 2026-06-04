-- 001_owner_leads.sql — Engine 1 (Owner-Outreach) storage layer.
--
-- landos schema only. Multi-county by design: county_fips is a stored column,
-- never hardcoded (Berrien = 26021 is merely the first value). RLS is enabled and
-- an authenticated ALL-policy mirrors the existing overlay_polygons pattern.
--
-- Idempotency: upsert key is (tenant_id, county_fips, parcel_apn) so re-running the
-- ReportAll normalizer overwrites a lead in place rather than duplicating it.

create table if not exists landos.owner_leads (
    lead_id                 uuid primary key default gen_random_uuid(),
    tenant_id               uuid,
    county_fips             text not null,
    parcel_apn              text not null,
    situs_address           text,
    situs_lat               numeric,
    situs_lng               numeric,
    owner_name              text,
    owner_mailing_address   text,
    owner_mailing_city      text,
    owner_mailing_state     text,
    owner_mailing_zip       text,
    land_use_class          text,
    acreage                 numeric,
    absentee_flag           boolean default false,
    absentee_tier           text,                       -- out_of_state|out_of_county|out_of_town|owner_occupied|unknown
    owner_type              text,                       -- individual|entity|govt
    absentee_score          numeric,
    signal_overlap          jsonb,                      -- which existing signals this parcel also carries
    final_lead_score        numeric,
    skip_trace_status       text default 'not_traced',
    skip_trace_meta         jsonb,
    push_status             text default 'not_pushed',
    pushed_at               timestamptz,
    push_target             text,
    source                  text default 'reportall',
    provenance              text,
    raw_meta                jsonb,
    created_at              timestamptz not null default now(),
    updated_at              timestamptz not null default now()
);

-- idempotent upsert key
create unique index if not exists owner_leads_tenant_county_apn_uidx
    on landos.owner_leads (tenant_id, county_fips, parcel_apn);

-- fast county-scoped ranking reads
create index if not exists owner_leads_county_score_idx
    on landos.owner_leads (county_fips, final_lead_score desc);

alter table landos.owner_leads enable row level security;

drop policy if exists owner_leads_authenticated_policy on landos.owner_leads;
create policy owner_leads_authenticated_policy
    on landos.owner_leads
    as permissive
    for all
    to authenticated
    using (true)
    with check (true);


-- compliance audit trail: one row per normalizer run
create table if not exists landos.owner_leads_import_log (
    id              uuid primary key default gen_random_uuid(),
    tenant_id       uuid,
    source          text,
    county_fips     text,
    run_at          timestamptz not null default now(),
    row_count       integer not null default 0,
    status          text not null default 'ok',
    notes           text
);

alter table landos.owner_leads_import_log enable row level security;

drop policy if exists owner_leads_import_log_authenticated_policy on landos.owner_leads_import_log;
create policy owner_leads_import_log_authenticated_policy
    on landos.owner_leads_import_log
    as permissive
    for all
    to authenticated
    using (true)
    with check (true);

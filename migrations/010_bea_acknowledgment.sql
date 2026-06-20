-- 010_bea_acknowledgment.sql — Part 201 BEA acknowledgment (append-only compliance record).
--
-- One acknowledgment per acquisition lead that a Baseline Environmental Assessment is
-- required (the requires_bea badge). APPEND-ONLY, mirroring public.lead_activity:
--   * RLS on, tenant + RBAC policies (manager / owner_admin / tenant_admin WRITE;
--     per-RBAC READ incl. dialer/va on their assigned leads).
--   * at the GRANT layer authenticated gets INSERT + SELECT only (UPDATE/DELETE revoked),
--     so an acknowledgment can never be edited or erased by a CRM user.
-- The engine (owner postgres) and service_role bypass RLS. Lead FK matches lead_activity:
-- acquisition_leads(lead_id), the table PK.

create table if not exists public.bea_acknowledgment (
    id               uuid primary key default gen_random_uuid(),
    lead_id          uuid not null references public.acquisition_leads(lead_id) on delete cascade,
    tenant_id        uuid not null default '1840ac90-54bd-4b3d-8baf-227c8d84ab5a'::uuid,
    acknowledged_by  uuid not null references public.profiles(id),
    acknowledged_at  timestamptz not null default now(),
    note             text,
    unique (lead_id)
);

alter table public.bea_acknowledgment enable row level security;

-- WRITE (insert): manager / owner_admin / tenant_admin only; acknowledged_by must be self.
drop policy if exists bea_admin_insert on public.bea_acknowledgment;
create policy bea_admin_insert on public.bea_acknowledgment for insert to authenticated
    with check ((app_current_role() = any (array['owner_admin','tenant_admin']))
                and acknowledged_by = auth.uid());

drop policy if exists bea_manager_insert on public.bea_acknowledgment;
create policy bea_manager_insert on public.bea_acknowledgment for insert to authenticated
    with check ((app_current_role() = 'manager') and acknowledged_by = auth.uid()
                and tenant_id = app_current_tenant());

-- READ (select): per-RBAC, mirror lead_activity.
drop policy if exists bea_admin_select on public.bea_acknowledgment;
create policy bea_admin_select on public.bea_acknowledgment for select to authenticated
    using (app_current_role() = any (array['owner_admin','tenant_admin']));

drop policy if exists bea_manager_select on public.bea_acknowledgment;
create policy bea_manager_select on public.bea_acknowledgment for select to authenticated
    using ((app_current_role() = 'manager') and tenant_id = app_current_tenant());

drop policy if exists bea_caller_select on public.bea_acknowledgment;
create policy bea_caller_select on public.bea_acknowledgment for select to authenticated
    using ((app_current_role() = any (array['dialer','va'])) and exists (
        select 1 from public.acquisition_leads al
        where al.lead_id = bea_acknowledgment.lead_id and al.assigned_to = auth.uid()));

-- APPEND-ONLY at the GRANT layer (mirror lead_activity EXACTLY): authenticated = INSERT +
-- SELECT only; anon = nothing. The public schema's DEFAULT PRIVILEGES grant anon + authenticated
-- a full set (incl. UPDATE / DELETE / TRUNCATE). TRUNCATE is NOT gated by RLS, so it must be
-- revoked or the table is not truly append-only. Revoke all, then re-grant the minimal set.
revoke all on public.bea_acknowledgment from anon;
revoke all on public.bea_acknowledgment from authenticated;
grant select, insert on public.bea_acknowledgment to authenticated;
grant all on public.bea_acknowledgment to service_role;

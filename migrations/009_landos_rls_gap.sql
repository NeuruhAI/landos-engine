-- 009_landos_rls_gap.sql — close the RLS gap on the two engine-internal PII/signal tables.
--
-- landos.contact_relations (kin / heirs / phones — PII) and landos.discovery_signals
-- (motivated-seller signals) were created RLS-OFF (migrations 005 / 007). They are
-- ENGINE-INTERNAL and must NEVER be reachable over the CRM's PostgREST (anon /
-- authenticated) — the landos<->CRM firewall. Enable RLS and grant ONLY the service
-- role. Anon and authenticated get NO permissive policy, so RLS denies them (0 rows).
-- The engine connects as the table OWNER (postgres), which BYPASSES RLS (no FORCE),
-- so the scoring / sync read paths (kin + discovery_signals) are unaffected.

alter table landos.contact_relations enable row level security;
alter table landos.discovery_signals enable row level security;

drop policy if exists contact_relations_service_only on landos.contact_relations;
create policy contact_relations_service_only on landos.contact_relations
    as permissive for all to service_role using (true) with check (true);

drop policy if exists discovery_signals_service_only on landos.discovery_signals;
create policy discovery_signals_service_only on landos.discovery_signals
    as permissive for all to service_role using (true) with check (true);

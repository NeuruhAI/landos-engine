-- 042_efxo_landos_constraints_parity.sql — T6 supporting-system convergence (2026-09-13).
--
-- WHY: the 2026-09-08 cutover copied landos.* to efxowqudmsfqjmbeidjc as DATA ONLY. Every
-- primary key, unique key, foreign key and index of the legacy spine is absent on the
-- production authority (0/31 tables carry a PK; 0 indexes; verified 2026-09-13 14:5xZ).
-- Since landos-engine #37 the live engine resolves efxo, so every `insert ... on conflict`
-- writer fails:
--   psycopg.errors.InvalidColumnReference: there is no unique or exclusion constraint
--   matching the ON CONFLICT specification      (POST /evaluate 500 x14, 2026-09-13 10:00Z)
-- The data copy also triple-loaded two tables (audit_log 27,636 = 3 x 9,212 identical rows;
-- comps 1,371 = 3 x 457), which blocks their primary keys.
--
-- WHAT: (1) remove exact-duplicate rows (identical in every column) from audit_log and
-- comps, keeping one physical row each; (2) recreate every constraint, index and trigger of
-- the legacy landos schema exactly as `pg_dump --section=post-data` emits it, guarded so the
-- file is idempotent and a no-op where they already exist (lvdi, local courts). RLS state and
-- policies are NOT touched (already present on efxo: 31 tables / 13 policies).
-- Source: pg_dump 17.10 --schema-only --section=post-data -n landos of the legacy spine
-- (skipped from that dump: 31 RLS enables, 13 policies).
-- Emits: constraints 61, indexes 47, triggers 1.
-- Apply (one transaction): psql "$DSN" -X -v ON_ERROR_STOP=1 -1 -f migrations/042_efxo_landos_constraints_parity.sql
-- Court first with the same file wrapped in begin/rollback (scripts/court_042_landos_parity.sh).

set local statement_timeout = '15min';   -- index builds on parcel_geom (573k rows) exceed the 120 s role default

-- (1) exact-duplicate physical rows (every column equal; all columns are comparable types).
--     Keeps the first physical row of each group. Idempotent: deletes 0 on a clean table.
delete from landos.audit_log where ctid in (
  select ctid from (select ctid, row_number() over (
    partition by id, tenant_id, actor, operation, target_table, target_id, metadata, created_at
    order by ctid) rn from landos.audit_log) d where rn > 1);
delete from landos.comps where ctid in (
  select ctid from (select ctid, row_number() over (
    partition by id, tenant_id, subject_apn, comp_address, comp_apn, sale_date, sale_price, lot_size, acreage,
                 property_type, distance_mi, source, provenance, notes, created_at, updated_at
    order by ctid) rn from landos.comps) d where rn > 1);

-- (2) constraints, indexes, trigger — post-data parity with the legacy spine
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'address_apn_cache_pkey' and conrelid = 'landos.address_apn_cache'::regclass) then
    alter table only landos.address_apn_cache add constraint address_apn_cache_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'audit_log_pkey' and conrelid = 'landos.audit_log'::regclass) then
    alter table only landos.audit_log add constraint audit_log_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'claims_pkey' and conrelid = 'landos.claims'::regclass) then
    alter table only landos.claims add constraint claims_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'comps_import_log_pkey' and conrelid = 'landos.comps_import_log'::regclass) then
    alter table only landos.comps_import_log add constraint comps_import_log_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'comps_pkey' and conrelid = 'landos.comps'::regclass) then
    alter table only landos.comps add constraint comps_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'contact_relations_pkey' and conrelid = 'landos.contact_relations'::regclass) then
    alter table only landos.contact_relations add constraint contact_relations_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'discovery_signals_pkey' and conrelid = 'landos.discovery_signals'::regclass) then
    alter table only landos.discovery_signals add constraint discovery_signals_pkey PRIMARY KEY (signal_id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'event_ledger_pkey' and conrelid = 'landos.event_ledger'::regclass) then
    alter table only landos.event_ledger add constraint event_ledger_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'event_ledger_tenant_id_idempotency_key_key' and conrelid = 'landos.event_ledger'::regclass) then
    alter table only landos.event_ledger add constraint event_ledger_tenant_id_idempotency_key_key UNIQUE (tenant_id, idempotency_key);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'feedback_events_pkey' and conrelid = 'landos.feedback_events'::regclass) then
    alter table only landos.feedback_events add constraint feedback_events_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'field_observation_evidence_pkey' and conrelid = 'landos.field_observation_evidence'::regclass) then
    alter table only landos.field_observation_evidence add constraint field_observation_evidence_pkey PRIMARY KEY (pin_id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'field_pins_pkey' and conrelid = 'landos.field_pins'::regclass) then
    alter table only landos.field_pins add constraint field_pins_pkey PRIMARY KEY (pin_id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'hunt_jobs_pkey' and conrelid = 'landos.hunt_jobs'::regclass) then
    alter table only landos.hunt_jobs add constraint hunt_jobs_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'identity_nodes_pkey' and conrelid = 'landos.identity_nodes'::regclass) then
    alter table only landos.identity_nodes add constraint identity_nodes_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'identity_nodes_tenant_id_parcel_apn_owner_hash_key' and conrelid = 'landos.identity_nodes'::regclass) then
    alter table only landos.identity_nodes add constraint identity_nodes_tenant_id_parcel_apn_owner_hash_key UNIQUE (tenant_id, parcel_apn, owner_hash);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'insight_cache_pkey' and conrelid = 'landos.insight_cache'::regclass) then
    alter table only landos.insight_cache add constraint insight_cache_pkey PRIMARY KEY (lead_id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'insight_spend_pkey' and conrelid = 'landos.insight_spend'::regclass) then
    alter table only landos.insight_spend add constraint insight_spend_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'lead_outcomes_pkey' and conrelid = 'landos.lead_outcomes'::regclass) then
    alter table only landos.lead_outcomes add constraint lead_outcomes_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'lead_outcomes_source_source_ref_key' and conrelid = 'landos.lead_outcomes'::regclass) then
    alter table only landos.lead_outcomes add constraint lead_outcomes_source_source_ref_key UNIQUE (source, source_ref);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'overlay_polygons_pkey' and conrelid = 'landos.overlay_polygons'::regclass) then
    alter table only landos.overlay_polygons add constraint overlay_polygons_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'owner_leads_import_log_pkey' and conrelid = 'landos.owner_leads_import_log'::regclass) then
    alter table only landos.owner_leads_import_log add constraint owner_leads_import_log_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'owner_leads_pkey' and conrelid = 'landos.owner_leads'::regclass) then
    alter table only landos.owner_leads add constraint owner_leads_pkey PRIMARY KEY (lead_id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'owner_leads_run_log_pkey' and conrelid = 'landos.owner_leads_run_log'::regclass) then
    alter table only landos.owner_leads_run_log add constraint owner_leads_run_log_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'parcel_flood_pkey' and conrelid = 'landos.parcel_flood'::regclass) then
    alter table only landos.parcel_flood add constraint parcel_flood_pkey PRIMARY KEY (county_fips, parcel_apn);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'parcel_geom_county_fips_parcel_apn_key' and conrelid = 'landos.parcel_geom'::regclass) then
    alter table only landos.parcel_geom add constraint parcel_geom_county_fips_parcel_apn_key UNIQUE (county_fips, parcel_apn);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'parcel_geom_pkey' and conrelid = 'landos.parcel_geom'::regclass) then
    alter table only landos.parcel_geom add constraint parcel_geom_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'parcel_owner_pkey' and conrelid = 'landos.parcel_owner'::regclass) then
    alter table only landos.parcel_owner add constraint parcel_owner_pkey PRIMARY KEY (county_fips, parcel_apn);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'parcel_sale_natural_key' and conrelid = 'landos.parcel_sale'::regclass) then
    alter table only landos.parcel_sale add constraint parcel_sale_natural_key UNIQUE NULLS NOT DISTINCT (county_fips, parcel_apn, sale_date, sale_price);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'parcel_sale_pkey' and conrelid = 'landos.parcel_sale'::regclass) then
    alter table only landos.parcel_sale add constraint parcel_sale_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'scores_pkey' and conrelid = 'landos.scores'::regclass) then
    alter table only landos.scores add constraint scores_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'scoring_weights_pkey' and conrelid = 'landos.scoring_weights'::regclass) then
    alter table only landos.scoring_weights add constraint scoring_weights_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'scoring_weights_tenant_profile_version_key' and conrelid = 'landos.scoring_weights'::regclass) then
    alter table only landos.scoring_weights add constraint scoring_weights_tenant_profile_version_key UNIQUE (tenant_id, profile, version);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'signal_ingest_log_pkey' and conrelid = 'landos.signal_ingest_log'::regclass) then
    alter table only landos.signal_ingest_log add constraint signal_ingest_log_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'signal_sources_pkey' and conrelid = 'landos.signal_sources'::regclass) then
    alter table only landos.signal_sources add constraint signal_sources_pkey PRIMARY KEY (source_id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'signal_type_registry_pkey' and conrelid = 'landos.signal_type_registry'::regclass) then
    alter table only landos.signal_type_registry add constraint signal_type_registry_pkey PRIMARY KEY (source_type);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'signals_pkey' and conrelid = 'landos.signals'::regclass) then
    alter table only landos.signals add constraint signals_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'tenants_pkey' and conrelid = 'landos.tenants'::regclass) then
    alter table only landos.tenants add constraint tenants_pkey PRIMARY KEY (id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'tenants_slug_key' and conrelid = 'landos.tenants'::regclass) then
    alter table only landos.tenants add constraint tenants_slug_key UNIQUE (slug);
  end if;
end $$;
create UNIQUE index if not exists address_apn_cache_key on landos.address_apn_cache USING btree (tenant_id, cache_key);
create UNIQUE index if not exists comps_natural_key on landos.comps USING btree (tenant_id, subject_apn, comp_apn, comp_address) NULLS NOT DISTINCT;
create index if not exists discovery_signals_taxsale_promoted_idx on landos.discovery_signals USING btree (source_type, promoted) WHERE (source_type = 'taxsale'::text);
create index if not exists field_observation_evidence_apn_idx on landos.field_observation_evidence USING btree (parcel_apn, county_fips);
create index if not exists field_observation_evidence_observed_idx on landos.field_observation_evidence USING btree (observed_at);
create index if not exists field_observation_evidence_tenant_idx on landos.field_observation_evidence USING btree (tenant_id);
create index if not exists field_pins_apn_idx on landos.field_pins USING btree (resolved_county_fips, resolved_apn);
create index if not exists field_pins_origin_gix on landos.field_pins USING gist (origin);
create index if not exists field_pins_ray_gix on landos.field_pins USING gist (ray);
create index if not exists field_pins_unresolved on landos.field_pins USING btree (created_at) WHERE (resolved_apn IS NULL);
create index if not exists hunt_jobs_status_idx on landos.hunt_jobs USING btree (status, created_at);
create index if not exists idx_audit_time on landos.audit_log USING btree (created_at DESC);
create index if not exists idx_comps_subject on landos.comps USING btree (tenant_id, subject_apn);
create index if not exists idx_discovery_signals_county on landos.discovery_signals USING btree (county_fips, source_type);
create index if not exists idx_discovery_signals_status on landos.discovery_signals USING btree (status);
create index if not exists idx_event_node on landos.event_ledger USING btree (identity_node_id, created_at DESC);
create index if not exists idx_feedback_node on landos.feedback_events USING btree (identity_node_id);
create index if not exists idx_idnode_lookup on landos.identity_nodes USING btree (tenant_id, parcel_apn);
create index if not exists idx_idnode_owner on landos.identity_nodes USING btree (tenant_id, owner_hash);
create UNIQUE index if not exists idx_one_active_claim on landos.claims USING btree (identity_node_id) WHERE (status = 'active'::text);
create UNIQUE index if not exists idx_one_active_weights_per_profile on landos.scoring_weights USING btree (tenant_id, profile) WHERE (active = true);
create index if not exists idx_overlay_polygons_bbox on landos.overlay_polygons USING btree (minx, miny, maxx, maxy);
create index if not exists idx_overlay_polygons_type on landos.overlay_polygons USING btree (overlay_type);
create index if not exists idx_relations_lead on landos.contact_relations USING btree (owner_lead_id);
create index if not exists idx_score_node on landos.scores USING btree (identity_node_id, computed_at DESC);
create index if not exists idx_score_rank on landos.scores USING btree (tenant_id, score DESC);
create index if not exists idx_signal_ingest_log_source on landos.signal_ingest_log USING btree (source_id, run_at DESC);
create index if not exists idx_signals_apn on landos.signals USING btree (parcel_apn);
create index if not exists idx_signals_tenant_type on landos.signals USING btree (tenant_id, signal_type);
create index if not exists insight_cache_hash_idx on landos.insight_cache USING btree (grounding_hash);
create index if not exists insight_spend_month_idx on landos.insight_spend USING btree (spent_at);
create index if not exists lead_outcomes_lead_idx on landos.lead_outcomes USING btree (lead_id, noted_at DESC);
create UNIQUE index if not exists overlay_polygons_natural_key on landos.overlay_polygons USING btree (tenant_id, overlay_type, name) NULLS NOT DISTINCT;
create index if not exists owner_leads_county_score_idx on landos.owner_leads USING btree (county_fips, final_lead_score DESC);
create index if not exists owner_leads_run_log_county_time_idx on landos.owner_leads_run_log USING btree (county_fips, generated_at_utc DESC);
create UNIQUE index if not exists owner_leads_tenant_county_apn_uidx on landos.owner_leads USING btree (tenant_id, county_fips, parcel_apn);
create index if not exists parcel_geom_apn_idx on landos.parcel_geom USING btree (county_fips, parcel_apn);
create index if not exists parcel_geom_centroidix on landos.parcel_geom USING gist (centroid);
create index if not exists parcel_geom_gix on landos.parcel_geom USING gist (geom);
create index if not exists parcel_owner_class_idx on landos.parcel_owner USING btree (county_fips, property_class);
create index if not exists parcel_owner_kind_idx on landos.parcel_owner USING btree (county_fips, owner_kind);
create index if not exists parcel_owner_mailstate_idx on landos.parcel_owner USING btree (county_fips, owner_mailing_state);
create index if not exists parcel_owner_yearbuilt_idx on landos.parcel_owner USING btree (county_fips, year_built);
create index if not exists parcel_sale_county_acre_idx on landos.parcel_sale USING btree (county_fips, acreage);
create index if not exists parcel_sale_county_class_idx on landos.parcel_sale USING btree (county_fips, class_code);
create UNIQUE index if not exists signals_natural_key on landos.signals USING btree (tenant_id, source_id, parcel_apn, signal_type) NULLS NOT DISTINCT;
create UNIQUE index if not exists uq_discovery_signal_natural on landos.discovery_signals USING btree (source_type, source_ref);
drop trigger if exists trg_idnode_touch on landos.identity_nodes;
create trigger trg_idnode_touch BEFORE UPDATE ON landos.identity_nodes FOR EACH ROW EXECUTE FUNCTION landos.touch_updated_at();
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'address_apn_cache_tenant_id_fkey' and conrelid = 'landos.address_apn_cache'::regclass) then
    alter table only landos.address_apn_cache add constraint address_apn_cache_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'audit_log_tenant_id_fkey' and conrelid = 'landos.audit_log'::regclass) then
    alter table only landos.audit_log add constraint audit_log_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE SET NULL;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'claims_identity_node_id_fkey' and conrelid = 'landos.claims'::regclass) then
    alter table only landos.claims add constraint claims_identity_node_id_fkey FOREIGN KEY (identity_node_id) REFERENCES landos.identity_nodes(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'claims_tenant_id_fkey' and conrelid = 'landos.claims'::regclass) then
    alter table only landos.claims add constraint claims_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'comps_import_log_tenant_id_fkey' and conrelid = 'landos.comps_import_log'::regclass) then
    alter table only landos.comps_import_log add constraint comps_import_log_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'comps_tenant_id_fkey' and conrelid = 'landos.comps'::regclass) then
    alter table only landos.comps add constraint comps_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'contact_relations_owner_lead_id_fkey' and conrelid = 'landos.contact_relations'::regclass) then
    alter table only landos.contact_relations add constraint contact_relations_owner_lead_id_fkey FOREIGN KEY (owner_lead_id) REFERENCES landos.owner_leads(lead_id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'discovery_signals_matched_lead_id_fkey' and conrelid = 'landos.discovery_signals'::regclass) then
    alter table only landos.discovery_signals add constraint discovery_signals_matched_lead_id_fkey FOREIGN KEY (matched_lead_id) REFERENCES landos.owner_leads(lead_id);
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'event_ledger_identity_node_id_fkey' and conrelid = 'landos.event_ledger'::regclass) then
    alter table only landos.event_ledger add constraint event_ledger_identity_node_id_fkey FOREIGN KEY (identity_node_id) REFERENCES landos.identity_nodes(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'event_ledger_tenant_id_fkey' and conrelid = 'landos.event_ledger'::regclass) then
    alter table only landos.event_ledger add constraint event_ledger_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'feedback_events_identity_node_id_fkey' and conrelid = 'landos.feedback_events'::regclass) then
    alter table only landos.feedback_events add constraint feedback_events_identity_node_id_fkey FOREIGN KEY (identity_node_id) REFERENCES landos.identity_nodes(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'feedback_events_tenant_id_fkey' and conrelid = 'landos.feedback_events'::regclass) then
    alter table only landos.feedback_events add constraint feedback_events_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'field_observation_evidence_pin_id_fkey' and conrelid = 'landos.field_observation_evidence'::regclass) then
    alter table only landos.field_observation_evidence add constraint field_observation_evidence_pin_id_fkey FOREIGN KEY (pin_id) REFERENCES landos.field_pins(pin_id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'identity_nodes_tenant_id_fkey' and conrelid = 'landos.identity_nodes'::regclass) then
    alter table only landos.identity_nodes add constraint identity_nodes_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'overlay_polygons_tenant_id_fkey' and conrelid = 'landos.overlay_polygons'::regclass) then
    alter table only landos.overlay_polygons add constraint overlay_polygons_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'scores_identity_node_id_fkey' and conrelid = 'landos.scores'::regclass) then
    alter table only landos.scores add constraint scores_identity_node_id_fkey FOREIGN KEY (identity_node_id) REFERENCES landos.identity_nodes(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'scores_tenant_id_fkey' and conrelid = 'landos.scores'::regclass) then
    alter table only landos.scores add constraint scores_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'scoring_weights_tenant_id_fkey' and conrelid = 'landos.scoring_weights'::regclass) then
    alter table only landos.scoring_weights add constraint scoring_weights_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'signal_ingest_log_source_id_fkey' and conrelid = 'landos.signal_ingest_log'::regclass) then
    alter table only landos.signal_ingest_log add constraint signal_ingest_log_source_id_fkey FOREIGN KEY (source_id) REFERENCES landos.signal_sources(source_id) ON DELETE SET NULL;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'signal_ingest_log_tenant_id_fkey' and conrelid = 'landos.signal_ingest_log'::regclass) then
    alter table only landos.signal_ingest_log add constraint signal_ingest_log_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'signal_sources_tenant_id_fkey' and conrelid = 'landos.signal_sources'::regclass) then
    alter table only landos.signal_sources add constraint signal_sources_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'signals_source_id_fkey' and conrelid = 'landos.signals'::regclass) then
    alter table only landos.signals add constraint signals_source_id_fkey FOREIGN KEY (source_id) REFERENCES landos.signal_sources(source_id) ON DELETE CASCADE;
  end if;
end $$;
do $$ begin
  if not exists (select 1 from pg_constraint where conname = 'signals_tenant_id_fkey' and conrelid = 'landos.signals'::regclass) then
    alter table only landos.signals add constraint signals_tenant_id_fkey FOREIGN KEY (tenant_id) REFERENCES landos.tenants(id) ON DELETE CASCADE;
  end if;
end $$;

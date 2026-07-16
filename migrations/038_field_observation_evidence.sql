-- 038_field_observation_evidence.sql — LANDOS-P1.1: the P6 field-observation
-- evidence table (Bayesian ground-truth from the windshield).
--
-- One row per field pin that validated an existing lead score. The aegis
-- field-to-spine loop (feat/field-p6-observation-weight @11ebfcf) resolves a
-- pin to a parcel (P3), reads the parcel's top final_lead_score as the Bayesian
-- prior (read-only), and records the operator's on-the-ground observation here
-- (P6): W = p6_weight · resolution_confidence · ln(likelihood_ratio), folded in
-- log-odds space into a posterior. Until this table exists the aegis writer
-- soft-fails by design (reason: "spine_error") and the P3 loop is unaffected —
-- this migration turns those soft-fails into live evidence rows, nothing else.
--
-- Single-writer firewall preserved: NO trigger or function here writes
-- public.acquisition_leads. sync_to_crm.py remains the SOLE writer of the
-- projection and is the intended reader that folds this evidence back into the
-- score on its own pass (retro-fold join: parcel_apn, county_fips).
--
-- Contract pinned by the aegis writer (RECORD_OBSERVATION_SQL):
--   * ON CONFLICT (pin_id) — pin_id MUST be the PK; a replayed offline queue
--     re-states an observation, it never stacks a second row.
--   * RETURNING pin_id, posterior_score, score_delta — names must not change.
--   * category is TEXT with no enum/CHECK: the field terminal vocabulary grows
--     ahead of the spine; an unknown category is neutral evidence, not an error.
--   * p6_weight stored per-row on purpose: retro-audit of scores written under
--     an older weight after the env-tunable default moves.
--   * prior_score NOT NULL: the honest-null path (unscored parcel) skips the
--     insert entirely — no row is ever written without a real prior.
--
-- Additive, landos.* own-schema, idempotent (IF NOT EXISTS throughout).
-- RLS deny-all, zero table grants — the EXACT posture of 024_field_pins, which
-- the same aegis role already INSERTs live today.

create table if not exists landos.field_observation_evidence (
  pin_id                uuid primary key
                        references landos.field_pins(pin_id) on delete cascade,
  tenant_id             uuid not null,
  parcel_apn            text not null,
  county_fips           text not null,
  -- what was observed (field-terminal vocabulary, deliberately un-enumerated)
  category              text not null,
  sign                  text,
  -- the Bayesian update, fully materialized for audit
  prior_score           numeric(5,1) not null,
  likelihood_ratio      numeric      not null check (likelihood_ratio > 0),
  p6_weight             numeric      not null check (p6_weight > 0),
  resolution_confidence numeric(4,3) not null
                        check (resolution_confidence >= 0 and resolution_confidence <= 1),
  posterior_score       numeric(5,1) not null,
  score_delta           numeric(5,1) not null,
  observed_at           timestamptz  not null default now()
);

-- retro-fold join used by sync_to_crm.py
create index if not exists field_observation_evidence_apn_idx
  on landos.field_observation_evidence (parcel_apn, county_fips);
create index if not exists field_observation_evidence_tenant_idx
  on landos.field_observation_evidence (tenant_id);
-- freshness windows (recency-weighted folds)
create index if not exists field_observation_evidence_observed_idx
  on landos.field_observation_evidence (observed_at);

alter table landos.field_observation_evidence enable row level security;
-- RLS deny-all: no policy (same posture as field_pins / parcel_geom /
-- parcel_owner / lead_outcomes). service_role carries bypassrls; the aegis
-- FIELD_PINS_DATABASE_URL role holds its privileges at the schema level from
-- engine bootstrap — 024 field_pins ships zero table grants and its INSERTs
-- work live, so this table ships zero table grants too.

comment on table landos.field_observation_evidence is
  'P6 field-observation evidence: one row per field pin that validated a scored '
  'lead. Bayesian update materialized per-row (prior, LR, weight, confidence, '
  'posterior, delta). Writer: aegis field loop (upsert by pin_id). Reader: '
  'sync_to_crm.py retro-fold (sole acquisition_leads writer). 038, LANDOS-P1.1.';

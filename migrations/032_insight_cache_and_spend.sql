-- 032_insight_cache_and_spend.sql — DS-07 AI Insights: the brief cache + the spend meter's
-- ledger. Additive; RLS deny-all (service_role bypasses; the engine is the only reader).

-- One cached brief per board lead, keyed by the grounding hash. Regenerate ONLY when a
-- grounded input changes (hash differs) — never merely because the model was re-invoked.
create table if not exists landos.insight_cache (
  lead_id        uuid primary key,           -- public.acquisition_leads.lead_id
  grounding_hash text        not null,
  brief          jsonb       not null,       -- {brief, angle, do_not_say, grounding}
  model          text        not null,
  cost_usd       numeric     not null default 0,
  generated_at   timestamptz not null default now()
);
create index if not exists insight_cache_hash_idx on landos.insight_cache (grounding_hash);

-- The meter's ledger. Every PAID generation appends a row; the monthly cap is enforced by
-- summing cost_usd over the current calendar month. $0 local (Ollama) rows are recorded too
-- (cost_usd=0) so the ledger is a complete, visible accounting — the meter is the feature.
create table if not exists landos.insight_spend (
  id         bigint generated always as identity primary key,
  lead_id    uuid,
  model      text        not null,
  cost_usd   numeric     not null,
  tokens_in  integer,
  tokens_out integer,
  spent_at   timestamptz not null default now()
);
create index if not exists insight_spend_month_idx on landos.insight_spend (spent_at);

alter table landos.insight_cache enable row level security;
alter table landos.insight_spend enable row level security;

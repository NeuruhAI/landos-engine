-- 005_contact_resolver_graph.sql — contact-resolver graph schema (D1 v2 §4, P1).
--
-- ADDITIVE ONLY: no existing column is altered or dropped.
--
-- 1. owner_leads.skip_trace_meta (jsonb) — already exists from 001; re-asserted
--    idempotently here so this migration is self-contained. It holds the
--    resolver's per-lead resolved summary the export reads:
--      { "phones":[{"number","type","confidence","source","verified_2x"}],
--        "emails":[], "deceased":false, "deceased_source":null,
--        "controlling_human":{"name","role","source"},
--        "outreach_recipe":"R1..R5", "primary_target":"owner|member|trustee|heirs|PR",
--        "verified_via":[...], "resolved_at":"..." }
--
-- 2. owner_leads.confidence_score (numeric) — NEW: top resolved-contact
--    confidence, written by the P3 importer; the callable cutoff compares
--    against it (threshold 0.6, tuned after blind-50).
--
-- 3. landos.contact_relations — NEW: the durable kin/heir graph (the moat).
--    One row per (lead, relative) edge. Outreach Recipe R4 reads this when the
--    owner is deceased and heirs become the contact target. FK cascades so
--    deleting a lead removes its edges.

alter table landos.owner_leads
    add column if not exists skip_trace_meta jsonb;

alter table landos.owner_leads
    add column if not exists confidence_score numeric;

create table if not exists landos.contact_relations (
  id uuid primary key default gen_random_uuid(),
  owner_lead_id uuid not null references landos.owner_leads(lead_id) on delete cascade,
  relative_name text not null,
  relationship text,                 -- 'spouse'|'child'|'sibling'|'heir'|'associate'|'unknown'
  phone text, phone_type text,
  is_heir boolean default false,     -- true when owner deceased → this person is a contact target
  confidence numeric default 0.5,
  source text,                       -- which people-search / obit / probate source
  added_at timestamptz default now()
);

create index if not exists idx_relations_lead on landos.contact_relations(owner_lead_id);

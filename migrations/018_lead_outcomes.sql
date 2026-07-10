-- 018_lead_outcomes.sql — JOB 8 (2026-07-09): additive feedback-facts table.
--
-- The deterministic feedback loop: real call/outreach dispositions flow back into
-- the scorer as a weight modifier next run. This table is the DURABLE FACT store —
-- one row per observed disposition event, idempotent on its CRM source_ref.
--
-- FIREWALL: landos-owned DDL (own schema). The nightly reader (scripts/read_lead_outcomes.py)
-- lifts dispositions from public.lead_activity / public.communications (CRM, read-only)
-- into here; the scorer (scripts/score_composite_batch.py) reads the LATEST disposition
-- per lead as a deterministic modifier. No public.* writes from this lane.
--
-- disposition domain (lowercase, matches the CRM outcome vocabulary it's mapped from):
--   interested    — a positive connect (raises the lead)
--   callback      — reached but deferred (voicemail / call-back requested); neutral-positive
--   wrong_number  — bad contact data (lowers reachability, not the parcel)
--   dnc           — do-not-call asserted (suppress dialing; mail-only)
--   dead          — owner deceased / parcel gone / hard no (drops the lead)

create table if not exists landos.lead_outcomes (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid,
    lead_id      uuid not null,
    disposition  text not null
        check (disposition in ('interested','callback','wrong_number','dnc','dead')),
    source       text not null,                 -- 'lead_activity' | 'communications'
    source_ref   text,                          -- activity_id / comm_id — idempotency key
    noted_at     timestamptz not null,          -- when the disposition was observed
    raw          jsonb not null default '{}'::jsonb,
    created_at   timestamptz not null default now(),
    unique (source, source_ref)                 -- idempotent ingest; re-runs never duplicate
);

create index if not exists lead_outcomes_lead_idx
    on landos.lead_outcomes (lead_id, noted_at desc);

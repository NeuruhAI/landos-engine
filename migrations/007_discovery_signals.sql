-- 007_discovery_signals.sql — DISCOVERY-1: the signal-driven discovery layer.
--
-- Top-of-funnel pivot: stop ingesting surname/owner-occupant lists, start
-- ingesting motivated-seller SIGNALS (probate, obituary, estate sale, permit,
-- tax-delinquent, code-violation). A signal is NOT a lead: it becomes a
-- candidate owner_lead only after it resolves to a parcel (Stage 1 feed).
--
-- ADDITIVE ONLY. landos.* only.
--
-- Spec additions beyond the directive's column list (flagged at the gate):
--   tenant_id        — engine is multi-tenant everywhere else; signals follow.
--   source_ref       — the source's natural key (probate case number, obit URL
--                      slug). Idempotency anchor: re-ingest updates, never dupes.
--   matched_lead_id  — closes the signal -> lead loop for audit/measurement
--                      (which signal produced which lead, and the match rate).
--
-- parcel_apn rule (learned in the pierce batch, St. Joseph IN): always the
-- CANONICAL county tax APN — for IN the 71-form (ReportAll alt_id_1), NEVER
-- the 018-form ReportAll-internal id (that lives only in
-- owner_leads.reportall_parcel_id).

create table if not exists landos.discovery_signals (
    signal_id       uuid primary key default gen_random_uuid(),
    tenant_id       uuid,
    source_type     text not null,              -- 'probate_in' | 'obituary' | 'estate_sale' | ...
    source_ref      text not null,              -- natural key in the source (e.g. case number)
    source_url      text,
    county_fips     text not null,
    raw_payload     jsonb,
    parcel_apn      text,                       -- canonical county APN once matched (IN: 71-form)
    owner_name      text,                       -- decedent / owner as the source names them
    signal_date     date,                       -- when the signal happened (filing date etc.)
    confidence      numeric default 0.5,        -- signal->parcel match confidence
    ingested_at     timestamptz not null default now(),
    status          text not null default 'new',-- new|matched|no_match|va_pending|rejected
    matched_lead_id uuid references landos.owner_leads(lead_id)
);

create unique index if not exists uq_discovery_signal_natural
    on landos.discovery_signals(source_type, source_ref);
create index if not exists idx_discovery_signals_status
    on landos.discovery_signals(status);
create index if not exists idx_discovery_signals_county
    on landos.discovery_signals(county_fips, source_type);

-- 036_taxsale_promote_gate.sql — the human diligence GATE for tax-sale signals.
-- (Contract: landos-ingest docs/tax_sale_promote_contract.md. The doc says "mig 034";
--  034 + 035 were already taken this session by the hunt-lens + parcel_sale work, so this
--  is 036 — same gate, next free number.)
--
-- WHY: scripts/mint_taxsale_leads.py minted every taxsale temporal signal with only a
-- freshness gate — NO promote predicate. tax-sale.info's ToU prohibits AUTOMATED aggregation
-- for outreach + contacting former owners; the ToU line runs through a HUMAN keystroke. This
-- makes "promoted" a first-class, index-friendly boolean so the mint filter is gate-in-DATA,
-- and adds the audit fields. Additive; discovery_signals already has RLS enabled (mig 019 posture).
--
-- INVARIANT: nothing may auto-set promoted=true — no cron, no adapter, no bulk update. Only a
-- human promote (scripts/promote_taxsale.py diligence-sheet import, or the CRM promote control).

alter table landos.discovery_signals
  add column if not exists promoted     boolean     not null default false,
  add column if not exists promoted_by  uuid,
  add column if not exists promoted_at  timestamptz;

-- initialize from the staged gate field (ingest writes raw_payload.promoted=false for the
-- gated rows; the legacy ungated firehose has no such key -> stays false). Never sets true.
update landos.discovery_signals
   set promoted = true
 where source_type = 'taxsale'
   and coalesce(raw_payload->>'promoted', 'false') = 'true'
   and promoted = false;

-- the mint filter's access path: taxsale candidates that a human has promoted.
create index if not exists discovery_signals_taxsale_promoted_idx
  on landos.discovery_signals (source_type, promoted)
  where source_type = 'taxsale';

comment on column landos.discovery_signals.promoted is
  'Human diligence gate (mig 036 / tax_sale_promote_contract). A taxsale signal mints ONLY '
  'when promoted=true — flipped by a person (promote_taxsale.py / CRM control), never by '
  'automation. tax-sale.info ToU: no automated aggregation for outreach. See mint_taxsale_leads.';

-- 016_owner_leads_vintage_needed.sql — ENRICH-1 P3 (2026-07-05)
--
-- THE VINTAGE-CAPTURE FLAG (doctrine as schema): when a $0 county-roll owner
-- pull returns a GOVERNMENT name (TREASURER / COUNTY OF / CITY OF / STATE OF /
-- LAND BANK / FORFEIT), title has (or may have) vested post-forfeiture and the
-- county layer no longer names the delinquent owner we want to reach. That
-- result is NEVER written to owner_name — the row is flagged vintage_needed
-- instead: it waits for a paid/archival "vintage" capture (pre-vesting roll),
-- and the vested name is stowed in raw_meta->'vintage' for audit.
--
-- Additive only; default false keeps every existing row untouched.

alter table landos.owner_leads
  add column if not exists vintage_needed boolean not null default false;

comment on column landos.owner_leads.vintage_needed is
  'ENRICH-1: county roll returned a government/vested owner (post-forfeiture); '
  'owner_name deliberately left blank — row awaits paid vintage (pre-vesting) capture. '
  'Vested name preserved in raw_meta->''vintage''.';

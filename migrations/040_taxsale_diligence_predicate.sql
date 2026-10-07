-- 040_taxsale_diligence_predicate.sql — G1: the FULL tax-sale mint predicate (additive).
--
-- Mig 036 made "promoted" (the human keystroke) gate-in-data. This completes the predicate:
-- a taxsale signal mints ONLY when ALL of:
--   * fresh            — upcoming_taxsale() passes (app/freshness.py; defense-in-depth in Python)
--   * promoted         = true   (the human keystroke, mig 036)
--   * needs_diligence  = false  (diligence review COMPLETE — fail-closed: default true)
--   * diligence_flag   is null  (a flagged record NEVER mints — terminal until a human clears it)
--
-- FAIL-CLOSED DEFAULTS: every new taxsale signal lands needs_diligence=true. Nothing automated
-- may set needs_diligence=false or clear diligence_flag — both are human keystrokes through
-- scripts/promote_taxsale.py (same invariant as mig 036's promoted). Automation may only set
-- diligence_flag (flagging is the SAFE direction: it can only prevent a mint, never cause one).
--
-- NOTE (deploy order): scripts/mint_taxsale_leads.py references these columns as of the same
-- change — the mint FAILS LOUDLY against an un-migrated spine rather than minting without the
-- diligence predicate. Apply this migration before the next mint run (operator keystroke).

alter table landos.discovery_signals
  add column if not exists needs_diligence boolean     not null default true,
  add column if not exists diligence_flag  text,
  add column if not exists flagged_by      uuid,
  add column if not exists flagged_at      timestamptz;

-- Backfill: a promoted row already went through the human diligence-sheet keystroke (mig 036) —
-- its review is complete. Everything else stays needs_diligence=true (fail-closed). Never sets
-- promoted, never clears a flag.
update landos.discovery_signals
   set needs_diligence = false
 where source_type = 'taxsale' and promoted = true and needs_diligence = true;

-- The mint filter's access path: the full predicate, index-friendly.
create index if not exists discovery_signals_taxsale_mintable_idx
  on landos.discovery_signals (source_type, promoted, needs_diligence)
  where source_type = 'taxsale' and diligence_flag is null;

comment on column landos.discovery_signals.needs_diligence is
  'G1 diligence predicate (mig 040). Fail-closed: true until a human diligence review clears it '
  '(promote_taxsale.py keystroke). A taxsale signal with needs_diligence=true NEVER mints.';
comment on column landos.discovery_signals.diligence_flag is
  'G1 flag (mig 040). Non-null = a diligence problem was found (reason text) — the record NEVER '
  'mints and cannot be promoted while flagged. Clearing a flag is a human keystroke only.';

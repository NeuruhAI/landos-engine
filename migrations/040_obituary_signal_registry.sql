-- 040_obituary_signal_registry.sql — P-23: register the obituary signal (additive).
--
-- Gate-in-data (lens doctrine, mig 027): obituary is ENRICHMENT, mints=false — an
-- obituary is name-only (no property link, docs/probate_feasibility.md), so it can
-- corroborate a property-linked match but can NEVER mint a lead. Any obituary→parcel
-- identity is fuzzy and flows through app/discovery/obituary_correlation.py, which is
-- structurally review-only. This row is what lets the mint/recipe routers refuse it
-- by DATA, not by application logic.

insert into landos.signal_type_registry (source_type, signal_class, lens, mints, note) values
  ('obituary', 'enrichment', 'DISTRESS', false,
   'death notice — name-only, NO property link; fuzzy identity => review candidates only '
   '(P-23); corroborates probate_in, never mints')
on conflict (source_type) do update set
  signal_class = excluded.signal_class, lens = excluded.lens,
  mints = excluded.mints, note = excluded.note;

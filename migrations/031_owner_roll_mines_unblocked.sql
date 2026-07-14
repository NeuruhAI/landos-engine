-- 031_owner_roll_mines_unblocked.sql — register the two mines mig 026/028 named as HARD
-- GAPS, now UNBLOCKED by the assessor widen (mig 030) + the multi-county rolls (Vanderburgh
-- 18163 carries year_built 83% / assessed 100% / real DLGF class codes).
--
--   mine_teardown   — old structure (year_built <= 1960) worth < half the land: the value is
--                     the dirt, the building is disposable -> CONVERSION (redevelop).
--   mine_class_play — DLGF commercial subtypes lodging (410 motel, 411 hotel), self-storage
--                     (481 mini-warehouse), golf (462 range, 463 course): the motel play,
--                     an operating asset ripe to reposition -> CONVERSION.
--
-- HELD, not fired: these mint=true so the dry-run counts them, but the operator ruling stands
-- (fire only when a lens names a motivation and its spot-check passes). Class codes are the
-- state-standard DLGF set, confirmed from Delaware's PropertyClassDesc (same codes statewide).

insert into landos.signal_type_registry (source_type, signal_class, lens, mints, note) values
  ('mine_teardown',   'discovery', 'CONVERSION', true,
   'owner-roll: year_built<=1960 AND improvement_assessed < 0.5*land_assessed — teardown/redevelop; unblocked by mig 030'),
  ('mine_class_play', 'discovery', 'CONVERSION', true,
   'owner-roll: DLGF class lodging(410/411)/self-storage(481)/golf(462/463) — the motel play; unblocked by real class codes')
on conflict (source_type) do update set
  signal_class = excluded.signal_class, lens = excluded.lens,
  mints = excluded.mints, note = excluded.note;

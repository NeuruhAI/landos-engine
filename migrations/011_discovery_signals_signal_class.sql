-- 011_discovery_signals_signal_class.sql — AXON Emergence Loop, step 1.
--
-- Disambiguate the two semantic families that currently share landos.discovery_signals
-- so the emergence scorer (step 2, a separate directive) only time-decays REAL dated
-- events. The table today conflates:
--   * TEMPORAL signals  — things that happened on a date (probate_in today; future
--                         taxsale / taxdelinquent / estate_sale / permit / rezoning).
--   * STRUCTURAL signals — graph derivations inferred from the contact graph, with no
--                         event date (graph_hub / graph_heir_network / graph_shared_mailing).
-- A structural row must NEVER score as "emerging." This column makes that explicit
-- instead of inferring it from signal_date being NULL (a coincidence of today's data,
-- not the rule).
--
-- ADDITIVE + REVERSIBLE. landos.* only. Adds one nullable column and backfills the 14
-- existing rows by source_type family. Touches no other column, not the 136, not the
-- scoring engine.
--
-- FAIL-SAFE BY DESIGN — NO DEFAULT. NULL is intentional: the step-2 scorer reads ONLY
-- signal_class = 'temporal', so an unlabeled row simply does not emerge (safe) rather
-- than a structural row risking emergence (unsafe). Backfill is SEMANTIC (source_type
-- family), NOT by signal_date presence, so future dated signals that arrive without a
-- date still classify correctly.
--
-- WRITER CONTRACT (see DISCOVERY_ADAPTERS.md): going forward every writer sets
-- signal_class explicitly — the national discovery adapters (CC3) write 'temporal';
-- the graph-signal writer writes 'structural'.
--
-- Index: intentionally SKIPPED (14 rows). A partial/btree index on (signal_class, status)
-- is a later optimization, not this migration.
--
-- DOWN-MIGRATION:
--   alter table landos.discovery_signals drop constraint if exists discovery_signals_signal_class_chk;
--   alter table landos.discovery_signals drop column if exists signal_class;

alter table landos.discovery_signals
    add column if not exists signal_class text;

alter table landos.discovery_signals
    drop constraint if exists discovery_signals_signal_class_chk;
alter table landos.discovery_signals
    add constraint discovery_signals_signal_class_chk
    check (signal_class is null or signal_class in ('temporal','structural'));

-- Backfill the 14 by SEMANTIC family (source_type), NOT by signal_date.
-- graph_* = structural derivations; everything else (probate_in, + future
-- taxsale/taxdelinquent/estate_sale/permit/rezoning) = temporal events.
update landos.discovery_signals
    set signal_class = case
        when source_type like 'graph\_%' escape '\' then 'structural'
        else 'temporal'
    end
    where signal_class is null;

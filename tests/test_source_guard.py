"""
test_source_guard.py — the recipe-sync fix (mission FIX THE RECIPE SYNC):
  JOB 1  recipe_source maps to the CRM's controlled vocabulary ('engine_sync'),
  JOB 2  assert_source_allowed guards acquisition_leads_source_check BEFORE any insert,
         and the discovered-tier INSERT never sends a NULL source (the column is NOT NULL),
  JOB 3  --check-constraints round-trips the EXACT DISCOVERED_INSERT_SQL (shape pinned here).

Pure: constant + guard + SQL-shape invariants, and a source-text check that the guard is
actually wired into main(). The live round-trip (real txn + rollback vs. the spine) and the
624-row census are pinned in the commit receipt.
"""
import inspect
import re

import pytest

import scripts.sync_to_crm as sync
from scripts.sync_to_crm import (ALLOWED_SOURCES, assert_source_allowed,
                                 recipe_source, DISCOVERED_INSERT_SQL)


# ---- JOB 1: the mapping (recipe provenance -> CRM's controlled vocabulary) ----------

def test_recipe_source_maps_to_engine_sync():
    # the old value 'recipe:<id>' is exactly what tripped acquisition_leads_source_check.
    assert recipe_source({"recipe_id": "senior_lakefront_assemblage_v1"}) == "engine_sync"


# ---- JOB 2: the allowed set matches the LIVE CHECK, verbatim -------------------------

def test_allowed_set_is_the_live_check_vocabulary():
    # acquisition_leads_source_check (verified live 2026-07-14):
    #   source = ANY (ARRAY['engine_sync','manual','public_found','inbound_form','cold_caller'])
    assert set(ALLOWED_SOURCES) == {
        "engine_sync", "manual", "public_found", "inbound_form", "cold_caller"}


def test_guard_passes_every_allowed_value_and_null():
    for s in ALLOWED_SOURCES:
        assert assert_source_allowed(s) == s
    # None is legal: UPDATE => coalesce keeps the existing value; bare INSERT => DEFAULT.
    assert assert_source_allowed(None) is None


def test_guard_rejects_the_recipe_prefix_readably():
    with pytest.raises(ValueError) as e:
        assert_source_allowed("recipe:senior_lakefront_assemblage_v1")
    msg = str(e.value)
    assert "recipe:senior_lakefront_assemblage_v1" in msg   # names the offending value
    assert "engine_sync" in msg                             # names the fix / allowed set
    assert "acquisition_leads_source_check" in msg          # names the constraint


def test_guard_rejects_any_stray_value():
    for bad in ("Recipe", "ENGINE_SYNC", "cold-caller", "", "sync"):
        with pytest.raises(ValueError):
            assert_source_allowed(bad)


# ---- JOB 2/3: the discovered-tier INSERT shape (source is NOT NULL) ------------------

def test_discovered_insert_lists_source_and_has_21_placeholders():
    s = re.sub(r"\s+", " ", DISCOVERED_INSERT_SQL.lower())
    assert "insert into public.acquisition_leads" in s
    assert " source)" in s                                  # source IS written by this path
    assert "'discovered'" in s                              # status literal, not a placeholder
    assert s.count("%s") == 21                              # the value-tuple contract


def test_guard_is_wired_into_main_and_insert_uses_engine_sync_fallback():
    src = inspect.getsource(sync.main)
    # the INSERT source can never be NULL (NOT NULL column): defaults to 'engine_sync'.
    assert 'insert_source = lead_source or "engine_sync"' in src
    # both write paths are guarded before any statement runs.
    assert "assert_source_allowed(lead_source)" in src
    assert "assert_source_allowed(insert_source)" in src
    # the INSERT binds insert_source (never the possibly-NULL lead_source) to source.
    assert "sig_tags, insert_source))" in src
    assert "DISCOVERED_INSERT_SQL" in src                   # main() runs the shared SQL


def test_check_constraints_round_trips_the_real_insert():
    # JOB 3 proves DB-acceptance against the live table, not a phantom: it must run the
    # EXACT projection SQL and roll back.
    src = inspect.getsource(sync.check_constraints)
    assert "DISCOVERED_INSERT_SQL" in src
    assert "conn.transaction()" in src and "Rollback" in src
    assert "acquisition_leads_source_check" in src          # asserts the CHECK actually fires

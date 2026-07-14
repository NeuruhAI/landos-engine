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
                                 recipe_source, DISCOVERED_INSERT_SQL,
                                 DISCOVERED_INSERT_COLS, ACQ_NOT_NULL_COLS,
                                 assert_insert_row_valid, default_signal_tags,
                                 discovered_signal_tags)


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
    # the INSERT binds insert_source (never the possibly-NULL lead_source) to source, and
    # insert_sig_tags (never a null) to signal_tags — guarded before the execute runs.
    assert "insert_sig_tags, insert_source)" in src
    assert "insert_sig_tags = sig_tags or default_signal_tags(" in src
    assert "assert_insert_row_valid(insert_values)" in src
    assert "DISCOVERED_INSERT_SQL" in src                   # main() runs the shared SQL


def test_check_constraints_round_trips_the_real_insert():
    # The probe proves DB-acceptance against the live table, not a phantom: it must run the
    # EXACT projection SQL and roll back, and assert BOTH the source CHECK and the
    # signal_tags NOT NULL fire.
    src = inspect.getsource(sync.check_constraints)
    assert "DISCOVERED_INSERT_SQL" in src
    assert "conn.transaction()" in src and "Rollback" in src
    assert "acquisition_leads_source_check" in src          # asserts the CHECK actually fires
    assert "NotNullViolation" in src and "signal_tags" in src   # and the NOT NULL landmine


# ---- JOB 1 (defuse): signal_tags NOT NULL landmine ---------------------------------

def test_default_signal_tags_taxsale_gets_tax_delinquent():
    # the Vanderburgh-502 case: an unmatched tax-sale row carries no explicit tags.
    assert default_signal_tags("discovered_taxsale") == ["tax_delinquent"]
    assert default_signal_tags("discovered_tax_sale") == ["tax_delinquent"]
    assert default_signal_tags("discovered_signal") == ["discovered"]   # honest non-null fallback
    assert default_signal_tags(None) == ["discovered"]


def test_discovered_signal_tags_reads_raw_meta_for_discovery_rows():
    rm = {"signal_tags": ["ground_lease", "income_under_asset"]}
    assert discovered_signal_tags(rm, is_disc=True) == ["ground_lease", "income_under_asset"]
    assert discovered_signal_tags({}, is_disc=True) is None          # tag-less -> None (default fills)
    assert discovered_signal_tags(rm, is_disc=False) is None         # curated rows untouched


def _valid_row(**over):
    """A minimal DB-legal discovered-tier bind (all NOT NULL binds satisfied)."""
    base = {c: None for c in DISCOVERED_INSERT_COLS}
    base.update(source_lead_id="L1", tenant_id="T", signal_tags=["tax_delinquent"], source="engine_sync")
    base.update(over)
    return tuple(base[c] for c in DISCOVERED_INSERT_COLS)


def test_guard_passes_a_legal_discovered_bind():
    assert_insert_row_valid(_valid_row())   # no raise


def test_guard_rejects_null_signal_tags_naming_the_column():
    with pytest.raises(ValueError) as e:
        assert_insert_row_valid(_valid_row(signal_tags=None))
    msg = str(e.value)
    assert "signal_tags" in msg and "NOT NULL" in msg
    assert "tax_delinquent" in msg          # points to the fix


def test_guard_rejects_bad_source_through_the_sibling_path():
    with pytest.raises(ValueError):
        assert_insert_row_valid(_valid_row(source="recipe:x"))


def test_not_null_set_matches_the_bound_columns():
    # signal_tags + source are the only NOT NULL columns this INSERT actually binds
    # (the rest of the NOT NULL set uses column DEFAULTs — omitted from the INSERT).
    bound_notnull = set(DISCOVERED_INSERT_COLS) & ACQ_NOT_NULL_COLS
    assert bound_notnull == {"signal_tags", "source"}

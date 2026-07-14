"""
test_owner_roll_mines.py — W0: parcel_owner widen, lens registry, mine views,
parcel_flood, discovery mint. Pure SQL-text + constant invariants; the live
censuses (absentee 6934 · entity 12167 · exempt 4568 · adjacency 14818 anchors ·
flood 8225 parcels) and dry-run net-new counts ran at build time (commit receipt).
"""
import os
import re

from scripts.mint_discovery import MINE_SOURCES, ASSET_CLASS_EXPR, PROVENANCE

MIG = os.path.join(os.path.dirname(__file__), "..", "migrations")


def _flat(name):
    with open(os.path.join(MIG, name)) as f:
        return re.sub(r"\s+", " ", f.read().lower())


# ---- 026 widen ---------------------------------------------------------------------

def test_widen_is_additive_from_the_sealed_source():
    s = _flat("026_parcel_owner_widen.sql")
    assert "add column if not exists owner_mailing_state" in s
    assert "add column if not exists property_class" in s and "add column if not exists acres" in s
    assert "add column if not exists owner_kind" in s
    for banned in ("drop column", "drop table", "delete from", "truncate"):
        assert banned not in s


# ---- 027 lens registry -------------------------------------------------------------

def test_registry_domains_and_flood_is_discovery_not_attribute():
    s = _flat("027_signal_type_registry.sql")
    assert "check (signal_class in ('enrichment','discovery','attribute'))" in s
    assert ("check (lens in ('distress','income_under_asset','flood', 'environmental',"
            "'operator_distress','conversion', 'entity_distress'))") in s
    # the reversal: flood is discovery/FLOOD, never attribute (whitespace collapsed)
    assert "('flood_remap', 'discovery', 'flood'," in s
    assert "('ground_lease', 'discovery', 'income_under_asset'," in s


# ---- 028 mine views ----------------------------------------------------------------

def test_views_exact_normalized_match_and_govt_discipline():
    s = _flat("028_owner_roll_mines.sql")
    assert "upper(btrim(poa.owner_name)) = upper(btrim(pob.owner_name))" in s   # exact, not fuzzy
    assert "poa.vintage_flag is distinct from 'govt'" in s                       # no govt assemblage
    assert "st_touches(a.geom, b.geom)" in s
    # blocked mines are named, not faked
    assert "teardown" in s and "blocked" in s
    assert "class-code plays" in s


# ---- 029 parcel_flood --------------------------------------------------------------

def test_parcel_flood_table_is_additive_pk_rls():
    s = _flat("029_parcel_flood.sql")
    assert "create table if not exists landos.parcel_flood" in s
    assert "primary key (county_fips, parcel_apn)" in s
    assert "enable row level security" in s
    assert "delete from" not in s


# ---- discovery mint ----------------------------------------------------------------

def test_mint_excludes_govt_everywhere_and_is_registry_driven():
    s = re.sub(r"\s+", " ", ASSET_CLASS_EXPR.lower())
    assert "acreage" in s and "residential" in s and "'unknown'" in s          # canonical six
    assert PROVENANCE == "discovered_signal"
    # every mine predicate rides on the non-govt roll (the candidate SQL enforces it)
    from scripts.mint_discovery import _candidate_sql
    for src, pred in MINE_SOURCES.items():
        sql = _candidate_sql(src, pred).lower()
        assert "vintage_flag is distinct from 'govt'" in sql


def test_same_owner_adjacent_mine_is_registered_discovery():
    assert "mine_same_owner_adjacent" in MINE_SOURCES
    assert "mine_absentee" in MINE_SOURCES and "mine_entity" in MINE_SOURCES

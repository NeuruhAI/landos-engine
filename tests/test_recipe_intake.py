"""
test_recipe_intake.py — the §9.2 recipe->CRM mapping (senior_lakefront_assemblage_v1).

Proves the transform lane end-to-end WITHOUT a database:
  cluster_parcels.csv row -> mint_recipe_leads.map_csv_row -> owner_leads params
  -> sync_to_crm's recipe builders (score_signals / parcel_meta / signal_tags / source).
The single-writer rule is structural (mint writes owner_leads only; sync_to_crm remains
the sole acquisition_leads writer) — these tests pin the gates that keep it true.
"""
import pytest

from scripts.mint_recipe_leads import PROVENANCE, map_csv_row
from scripts.sync_to_crm import (
    is_recipe_meta, parcel_meta_from_recipe, recipe_signal_tags,
    recipe_source, score_signals_from_recipe,
)

# verbatim shape of out/20260713T005854Z_laporte_in/cluster_parcels.csv (row 0, Flisk)
CSV_ROW = {
    "parcel_apn": "46-01-21-304-006.000-022", "county_fips": "18091",
    "owner_name": "Flisk Kathryn M Trustee Trust #2015-101 dated 11/23/2015",
    "senior_score": "55.0", "tenure_years": "", "dist_to_lake_m": "135.0",
    "cluster_id": "41", "situs_lat": "41.730207", "situs_lng": "-86.891842",
    "signal_tags": "senior_tenure|lake_proximity|assemblage",
    "recipe_id": "senior_lakefront_assemblage_v1", "owner_kind": "human",
    "owner_occupied_proxy": "0", "tenure_floor": "1", "situs_address": "COLFAX AVE",
    "owner_mailing_address": "3646 W 81st Pl", "owner_mailing_city": "Chicago",
    "owner_mailing_state": "IL", "owner_mailing_zip": "60652",
    "surname": "FLISK", "cluster_size": "2", "same_surname_family": "True",
}
RUN_ID = "20260713T005854Z_laporte_in"


@pytest.fixture()
def mapped():
    return map_csv_row(dict(CSV_ROW), RUN_ID)


# --- intake shape: the params the mint upserts into owner_leads ---------------

def test_provenance_passes_the_sync_discovered_gate(mapped):
    # sync_to_crm inserts unmatched rows ONLY when provenance startswith 'discovered'
    assert mapped["provenance"] == PROVENANCE
    assert mapped["provenance"].startswith("discovered")


def test_provenance_collides_with_no_existing_scoping(mapped):
    # mig-015 scopes ol.provenance = 'discovered_taxsale' exactly; mig-004 tags
    # 'reportall:%' in 18141. The recipe prefix must match neither.
    assert mapped["provenance"] != "discovered_taxsale"
    assert not mapped["provenance"].startswith("reportall:")


def test_source_is_recipe_qualified(mapped):
    assert mapped["source"] == "recipe:senior_lakefront_assemblage_v1"


def test_owner_and_situs_carry_through(mapped):
    assert mapped["county_fips"] == "18091"
    assert mapped["parcel_apn"] == "46-01-21-304-006.000-022"
    assert mapped["owner_name"].startswith("Flisk Kathryn M")
    assert mapped["situs_address"] == "COLFAX AVE"
    assert mapped["situs_lat"] == pytest.approx(41.730207)
    assert mapped["situs_lng"] == pytest.approx(-86.891842)
    assert mapped["owner_mailing_state"] == "IL"


def test_recipe_owner_kind_human_is_individual(mapped):
    # the recipe's 3-iteration entity sweep already ran; 'human' is authoritative
    assert mapped["owner_type"] == "individual"


def test_window_dark_tenure_is_null_never_zero(mapped):
    assert mapped["raw_meta"]["tenure_years"] is None


def test_missing_recipe_id_rejects_loudly():
    row = dict(CSV_ROW, recipe_id="")
    with pytest.raises(ValueError):
        map_csv_row(row, RUN_ID)


# --- §9.2 projection: what sync_to_crm composes from the minted raw_meta ------

def test_raw_meta_satisfies_the_sync_recipe_marker(mapped):
    assert is_recipe_meta(mapped["raw_meta"])
    assert not is_recipe_meta({"land_av": "1000"})   # tax-sale payload stays on the old path
    assert not is_recipe_meta(None)


def test_score_signals_mapping(mapped):
    ss = score_signals_from_recipe(mapped["raw_meta"])
    assert ss == {
        "senior_score": 55.0, "tenure_years": None, "dist_to_lake_m": 135.0,
        "recipe_id": "senior_lakefront_assemblage_v1", "run_id": RUN_ID,
    }


def test_signal_tags_mapping(mapped):
    assert recipe_signal_tags(mapped["raw_meta"]) == [
        "senior_tenure", "lake_proximity", "assemblage"]
    assert recipe_signal_tags({"signal_tags": []}) is None   # None -> coalesce keeps existing


def test_parcel_meta_carries_cluster_stats_and_no_tax_keys(mapped):
    pm = parcel_meta_from_recipe(mapped["raw_meta"])
    assert pm["cluster"] == {"cluster_id": 41, "cluster_size": 2, "same_surname_family": True}
    assert pm["recipe"] == {"recipe_id": "senior_lakefront_assemblage_v1", "run_id": RUN_ID}
    # NO fabricated tax-sale keys: sync reads amount_owed/assessed_total via .get
    assert "amount_owed" not in pm and "assessed_total" not in pm


def test_source_projection(mapped):
    # §9.2 originally specified source='recipe:<id>', but that value violates
    # acquisition_leads_source_check (the CheckViolation that blocked the 624 mint).
    # Recipe leads are engine-synced -> 'engine_sync'; provenance is not lost (it rides in
    # signal_tags / score_signals.recipe / parcel_meta.recipe / owner_leads.source).
    assert recipe_source(mapped["raw_meta"]) == "engine_sync"

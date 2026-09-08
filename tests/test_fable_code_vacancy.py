"""Fable CODE_VACANCY unlock: bind existing NC tags, invent none.

Gap: RECIPE_BINDING_MISSING. Mode: PROMOTION_THEN_ACQUISITION_FREE.
No scraper. No Supabase. No DeedSonar. No landos-ingest edits.
"""
from __future__ import annotations

from pathlib import Path

from app.composite_inputs import assemble
from app.nc_dps_raw_writer import build_raw_candidates
from app.nc_dps_writer import signal_tags as qualified_signal_tags
from app.projection.classes import (
    FAMILY_CODE_VACANCY,
    GAP_RECIPE_BINDING_MISSING,
    NOT_RAW_SOURCE_MISSING,
    UNLOCK_PROMOTION_THEN_ACQUISITION_FREE,
)
from app.projection.code_vacancy import (
    EXISTING_CODE_VACANCY_TAGS,
    RECIPE_DIM_TAGS,
    TAG_NC_CODE_VIOLATION,
    TAG_NC_VACANCY,
    TAG_NC_VACANCY_OR_CODE,
    collect_existing_code_vacancy_tags,
    expand_recipe_tokens,
    project_code_vacancy_facts,
    tag_census,
)
from app.projection.estate_query import (
    index_ingested_parcels,
    project_onto_estate_row,
)
from app.recipe_logan import FACTOR_SIGNALS, score_logan
from app.scoring import DEFAULT_WEIGHTS as W


def test_writers_already_emit_the_census_tags():
    """Do not invent: the same strings the writers already stamp."""
    raw, _ = build_raw_candidates([
        {
            "signal_id": "a" * 64,
            "source_type": "vacancy",
            "geography": "37119",
            "parcel_apn": "1",
            "owner_name": "DOE",
            "detail_json": {},
            "confidence": 0.7,
        },
        {
            "signal_id": "b" * 64,
            "source_type": "code_violation",
            "geography": "37119",
            "parcel_apn": "2",
            "owner_name": "ROE",
            "detail_json": {},
            "confidence": 0.7,
        },
    ])
    by_apn = {row["parcel_apn"]: row["raw_meta"]["signal_tags"] for row in raw}
    assert TAG_NC_VACANCY in by_apn["1"]
    assert TAG_NC_CODE_VIOLATION in by_apn["2"]
    packet_tags = qualified_signal_tags({
        "scores": {"trigger_classes": ["vacancy_or_code"]},
    })
    assert TAG_NC_VACANCY_OR_CODE in packet_tags


def test_census_is_exactly_the_three_existing_nc_tags():
    census = tag_census()
    tags = {row["tag"] for row in census}
    assert tags == EXISTING_CODE_VACANCY_TAGS
    assert tags == {
        "nc_dps:vacancy_or_code",
        "nc_dps:vacancy",
        "nc_dps:code_violation",
    }
    writers = {row["writer"] for row in census}
    assert "app/nc_dps_writer.py:signal_tags" in writers
    assert "app/nc_dps_raw_writer.py" in writers
    for row in census:
        assert row["path"].startswith("estate/ingest")


def test_unknown_tag_strings_are_not_collected_or_invented():
    tags = collect_existing_code_vacancy_tags({
        "signal_tags": ["nc_dps:invented", "vacancy_flag", "nc_dps:vacancy"],
    })
    assert tags == ["nc_dps:vacancy"]
    promoted = project_code_vacancy_facts({"signal_tags": ["made_up_code_tag"]})
    assert promoted["invented_tags"] == []
    assert promoted["tags_found"] == []
    assert promoted["recipe_tokens"] == []
    assert promoted["facts"]["code_violation"]["state"] == "UNKNOWN"
    assert promoted["facts"]["vacancy"]["value"] is None


def test_qualified_writer_tag_promotes_both_recipe_tokens():
    fact = project_code_vacancy_facts({
        "raw_meta": {"signal_tags": ["nc_dps", "nc_dps:vacancy_or_code"]},
    })
    assert fact["gap_class"] == GAP_RECIPE_BINDING_MISSING
    assert fact["unlock_mode"] == UNLOCK_PROMOTION_THEN_ACQUISITION_FREE
    assert fact["not_class"] == NOT_RAW_SOURCE_MISSING
    assert fact["tags_found"] == [TAG_NC_VACANCY_OR_CODE]
    assert set(fact["recipe_tokens"]) == {"code_violation", "vacancy"}
    assert fact["facts"]["code_violation"]["state"] == "KNOWN"
    assert fact["facts"]["vacancy"]["state"] == "KNOWN"
    assert fact["facts"]["code_violation"]["source_field"] == TAG_NC_VACANCY_OR_CODE


def test_raw_writer_tags_promote_one_token_each():
    code = project_code_vacancy_facts({"signal_tags": [TAG_NC_CODE_VIOLATION]})
    assert code["recipe_tokens"] == ["code_violation"]
    assert code["facts"]["vacancy"]["state"] == "UNKNOWN"
    vacant = project_code_vacancy_facts({"signal_tags": [TAG_NC_VACANCY]})
    assert vacant["recipe_tokens"] == ["vacancy"]
    assert vacant["facts"]["code_violation"]["state"] == "UNKNOWN"


def test_wave_a_trigger_and_t8_signal_are_existing_facts_not_new_tags():
    wave = project_code_vacancy_facts({
        "emerging_signals": {"nc_dps": {"trigger_classes": ["vacancy_or_code"]}},
    })
    assert wave["tags_found"] == []
    assert wave["facts_found"] == ["vacancy_or_code"]
    assert set(wave["recipe_tokens"]) == {"code_violation", "vacancy"}
    t8 = project_code_vacancy_facts({"signal": "CODE_ENFORCEMENT_CASE"})
    assert t8["facts_found"] == ["CODE_ENFORCEMENT_CASE"]
    assert t8["recipe_tokens"] == ["code_violation"]
    assert t8["invented_tags"] == []


def test_estate_query_binds_overlay_tags_without_fabricating_false():
    ingested = index_ingested_parcels([{
        "parcel_apn": "0648080648",
        "signal_tags": [TAG_NC_VACANCY, TAG_NC_CODE_VIOLATION],
    }])
    hit = project_onto_estate_row({"apn": "0648080648", "owner_name": "DOE"}, ingested)
    miss = project_onto_estate_row({"apn": "NO-MATCH", "owner_name": "NONE"}, ingested)
    assert hit["code_violation"] is True
    assert hit["vacancy"] is True
    assert set(hit["code_vacancy_tokens"]) == {"code_violation", "vacancy"}
    projected = hit["projected"]["code_vacancy"]
    assert projected["family"] == FAMILY_CODE_VACANCY
    assert projected["invented_tags"] == []
    assert FAMILY_CODE_VACANCY in hit["projected"]["families"]
    assert "code_violation" not in miss
    assert "vacancy" not in miss
    assert miss["projected"]["code_vacancy"]["facts"]["vacancy"]["state"] == "UNKNOWN"
    assert miss["projected"]["invented_tags"] == []
    assert miss["projected"]["supabase_mutated"] is False
    assert miss["projected"]["scraper"] is False


def test_logan_binds_existing_nc_tags_and_ignores_invented():
    code = score_logan({}, [TAG_NC_CODE_VIOLATION])
    vacant = score_logan({}, [TAG_NC_VACANCY])
    combined = score_logan({}, [TAG_NC_VACANCY_OR_CODE])
    invented = score_logan({}, ["nc_dps:invented_vacancy"])
    fired_code = {f["factor"] for f in code["factors"] if f["points"] > 0}
    fired_vacant = {f["factor"] for f in vacant["factors"] if f["points"] > 0}
    fired_combined = {f["factor"] for f in combined["factors"] if f["points"] > 0}
    assert fired_code == {"code_violation"}
    assert fired_vacant == {"vacant_boarded"}
    assert fired_combined == {"code_violation", "vacant_boarded"}
    assert invented["score"] == 0.0
    assert TAG_NC_CODE_VIOLATION in FACTOR_SIGNALS["code_violation"]
    assert TAG_NC_VACANCY in FACTOR_SIGNALS["vacant_boarded"]
    assert "CODE_ENFORCEMENT_CASE" in FACTOR_SIGNALS["code_violation"]


def test_distress_recipe_binds_existing_nc_tags_on_existing_dims():
    # recipe_core imports app.config (needs DATABASE_URL). Bindings are
    # asserted from source so this unlock stays fixture-only.
    core = (
        Path(__file__).resolve().parents[1] / "app" / "recipe_core.py"
    ).read_text(encoding="utf-8")
    assert "from app.projection.code_vacancy import RECIPE_DIM_TAGS" in core
    assert '"vacancy"' in core
    assert 'list(RECIPE_DIM_TAGS["code_violation"])' in core
    assert 'list(RECIPE_DIM_TAGS["vacancy"])' in core
    assert TAG_NC_CODE_VIOLATION in RECIPE_DIM_TAGS["code_violation"]
    assert TAG_NC_VACANCY in RECIPE_DIM_TAGS["vacancy"]
    assert TAG_NC_VACANCY_OR_CODE in RECIPE_DIM_TAGS["code_violation"]
    assert TAG_NC_VACANCY_OR_CODE in RECIPE_DIM_TAGS["vacancy"]
    invented = "nc_dps:fake_vacancy"
    assert invented not in RECIPE_DIM_TAGS["code_violation"]
    assert invented not in RECIPE_DIM_TAGS["vacancy"]


def test_composite_promotes_existing_nc_tags_into_modeled_slots():
    code = assemble({"distress_tokens": [TAG_NC_CODE_VIOLATION]}, W)
    assert code["inputs"]["distress"] == {"code_violation": True}
    vacant = assemble({"distress_tokens": [TAG_NC_VACANCY]}, W)
    assert vacant["inputs"]["distress"] == {"vacancy": True}
    both = assemble({"distress_tokens": [TAG_NC_VACANCY_OR_CODE]}, W)
    assert both["inputs"]["distress"] == {"code_violation": True, "vacancy": True}
    assert both["null_components"]["distress"] is False
    junk = assemble({"distress_tokens": ["nc_dps:invented"]}, W)
    assert junk["coverage"]["distress"]["status"] == "grounded_unmodeled"
    assert junk["inputs"]["distress"] == {}


def test_expand_recipe_tokens_does_not_invent():
    assert expand_recipe_tokens(["nc_dps:vacancy"]) == ["vacancy"]
    assert set(expand_recipe_tokens(["nc_dps:vacancy_or_code"])) == {
        "code_violation", "vacancy",
    }
    assert expand_recipe_tokens(["weird_signal"]) == ["weird_signal"]


def test_projection_modules_still_refuse_forbidden_lanes():
    root = Path(__file__).resolve().parents[1] / "app" / "projection"
    blob = "".join(p.read_text(encoding="utf-8") for p in root.glob("*.py"))
    lowered = blob.lower()
    for needle in (
        "import supabase",
        "from supabase",
        "import deedsonar",
        "from deedsonar",
        "requests.get",
        "playwright",
        "selenium",
        "import kofile",
        "import fidlar",
        "import govos",
    ):
        assert needle not in lowered

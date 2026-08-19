"""T24C's supply may enter the graph. Its own file may never open the gate.

T24C wrote import rows in a grammar the adapter could not read, and every one of
its 258 rows was dropped by `continue` -- silently, counting nothing. Repairing
that is only half the job. The other half is refusing the obvious repair: the
package is labelled "import-ready at EXACT/HIGH" and stamping that token on
every row would write 95 county tax care-of lines into the party graph as proven
people. These tests pin both halves.
"""
from __future__ import annotations

import collections
from pathlib import Path

import pytest

from app.resolution.official_import import (
    RecoveredEvidence,
    entity_relationships,
    estate_relationships,
    load_import_csv,
    parse_source_document,
    recover_entity_confidence,
    recover_estate_confidence,
)

T24C = Path.home() / "neuruh-workspace" / "T24C_PUBLIC_DATA_GRAND_MINE_20260819"
T22 = (Path.home() / "neuruh-workspace" / "T22_PUBLIC_RECORD_DATA_SUPPLY_20260819"
       / "05_IMPORT_READY")

needs_t24c = pytest.mark.skipif(
    not (T24C / "05_IMPORT_READY" / "NC_ESTATE_IDENTITY_IMPORT.csv").exists(),
    reason="T24C supply not present on this host")
needs_t22 = pytest.mark.skipif(
    not (T22 / "NC_ESTATE_IDENTITY_IMPORT.csv").exists(),
    reason="T22 supply not present on this host")

COOWNER_ROW = {
    "property_candidate_id": "c9", "apn": "116305", "county": "Durham",
    "decedent_name": "GREEN LULA HEIRS GREEN ERSELDINE",
    "heir_name": "GREEN, ERSELDINE", "relationship": "CO_OWNER_OF_RECORD",
    "source_document": ("DURHAM_PARCELS :: services2.arcgis.com/x?REID=116305 "
                        "(corroborated by 2 independent official sources: "
                        "DURHAM_PARCELS,NC_ONEMAP_PARCELS)"),
    "source_date": "2026-08-19",
}
CARE_OF_ROW = {
    "property_candidate_id": "c10", "entity_legal_name": "HOLLINGSWORTH & COOK INC",
    "official_name": "AUBIE COOK", "official_title": "ORGANIZATION_CONTACT",
    "source_document": "NC_ONEMAP_PARCELS :: Cabarrus:05-030B-0000.00",
    "source_date": "2026-08-19",
}
HIGH = {("c9", "GREEN, ERSELDINE"): RecoveredEvidence("HIGH", "APN_EXACT+OWNER_SEGMENT")}
MEDIUM = {("c10", "AUBIE COOK"): RecoveredEvidence("MEDIUM", "COUNTY_ROLL_MAILING_ONLY")}


# -- the grammar ------------------------------------------------------------

def test_the_t24c_grammar_parses_and_yields_no_confidence():
    meta = parse_source_document(COOWNER_ROW["source_document"])
    assert meta["source_name"] == "DURHAM_PARCELS"
    assert meta["locator"] == "services2.arcgis.com/x?REID=116305"
    assert meta["corroborating_sources"] == ("DURHAM_PARCELS", "NC_ONEMAP_PARCELS")
    assert meta["confidence"] == "", "T24C states no confidence and none may be invented"


def test_the_t22_grammar_is_untouched():
    """T22's rows are already in production; they must parse byte for byte."""
    assert parse_source_document(
        "T22|WAKE_PARCELS|http://x|conf=EXACT|OFFICIAL_ROLE_STATED") == {
        "lane": "T22", "source_name": "WAKE_PARCELS", "locator": "http://x",
        "confidence": "EXACT", "match_method": "OFFICIAL_ROLE_STATED",
        "corroborating_sources": ()}


# -- the gate ---------------------------------------------------------------

def test_the_import_file_alone_cannot_open_the_gate():
    """The defect that dropped 258 rows, and the repair that must not undo it."""
    assert estate_relationships([COOWNER_ROW]) == {}
    assert entity_relationships([CARE_OF_ROW]) == {}


def test_evidence_supplies_the_confidence_the_file_withholds():
    rels = estate_relationships([COOWNER_ROW], HIGH)["c9"]
    assert [r.role for r in rels] == ["COOWNER"]


def test_a_county_care_of_line_is_medium_and_stays_out():
    """95 entity rows are the tax administrator saying who gets the bill.

    A bridge that stamped conf=HIGH on the file to make it parse would have put
    every one of them in the party graph as a proven officer.
    """
    assert entity_relationships([CARE_OF_ROW], MEDIUM) == {}


# -- what the roll is allowed to say ----------------------------------------

def test_co_ownership_is_stated_but_kinship_is_not():
    rel = estate_relationships([COOWNER_ROW], HIGH)["c9"][0]
    assert rel.role == "COOWNER"
    assert "not an adjudicated heirship" in rel.notes
    assert "HEIR" != rel.role


def test_corroboration_is_recorded_and_counted_honestly():
    rel = estate_relationships([COOWNER_ROW], HIGH)["c9"][0]
    assert "2 independent official sources" in rel.notes
    assert rel.evidence[0].source_family == "COUNTY_PARCEL_ROLL"


def test_no_t24c_relationship_carries_a_contact_point():
    for rels in estate_relationships([COOWNER_ROW], HIGH).values():
        for rel in rels:
            assert rel.party.contact_points == []


# -- against the real supply ------------------------------------------------

@needs_t24c
def test_the_real_t24c_supply_imports_143_and_zero_without_evidence():
    """The acceptance test the delta lane specified, run on the real files."""
    est = load_import_csv(T24C / "05_IMPORT_READY" / "NC_ESTATE_IDENTITY_IMPORT.csv")
    assert len(est) == 143
    assert estate_relationships(est) == {}
    rec = recover_estate_confidence(
        T24C / "04_NORMALIZED" / "ESTATE_RESOLUTION_EVIDENCE.csv")
    out = estate_relationships(est, rec)
    assert len(out) == 143
    roles = collections.Counter(r.role for v in out.values() for r in v)
    assert roles == {"OTHER_VERIFIED_RELATION": 104, "COOWNER": 27,
                     "ADMINISTRATOR": 7, "EXECUTOR": 5}
    assert "HEIR" not in roles


@needs_t24c
def test_the_real_t24c_entity_supply_admits_only_the_ten_it_proves():
    ent = load_import_csv(T24C / "05_IMPORT_READY" / "NC_ENTITY_IDENTITY_IMPORT.csv")
    assert len(ent) == 115
    rec = recover_entity_confidence(
        T24C / "04_NORMALIZED" / "ENTITY_RESOLUTION_EVIDENCE.csv")
    out = entity_relationships(ent, rec)
    assert len(out) == 10, "94 MEDIUM care-of lines and 10 address-only rows stay out"


@needs_t22
def test_t22_is_unchanged_by_the_t24c_repair():
    """The regression this repair could most easily have caused."""
    est = estate_relationships(load_import_csv(T22 / "NC_ESTATE_IDENTITY_IMPORT.csv"))
    ent = entity_relationships(load_import_csv(T22 / "NC_ENTITY_IDENTITY_IMPORT.csv"))
    assert len(est) == 104 and sum(len(v) for v in est.values()) == 104
    assert len(ent) == 11
    assert collections.Counter(r.role for v in est.values() for r in v) == {
        "OTHER_VERIFIED_RELATION": 92, "ADMINISTRATOR": 7, "EXECUTOR": 5}


# -- the care-of marker -----------------------------------------------------

CARE_OF_ROW = dict(COOWNER_ROW, property_candidate_id="c11",
                   heir_name="% IDA MAE P BROWN",
                   relationship="HEIR_OR_SUCCESSOR_CONTACT")
CARE_OF_CONF = {("c11", "IDA MAE P BROWN"): RecoveredEvidence("HIGH", "SURNAME_CORROBORATED")}


def test_a_care_of_marker_is_not_part_of_a_persons_name():
    """T22 landed `% IDA MAE P BROWN`; T24C cleans the marker and resolves the
    same human. Without this the graph gets two parties for one person."""
    rel = estate_relationships([CARE_OF_ROW], CARE_OF_CONF)["c11"][0]
    assert rel.party.canonical_name == "IDA MAE P BROWN"


def test_the_raw_value_still_records_what_the_roll_actually_said():
    """Cleaning the identity must not rewrite the evidence."""
    ev = estate_relationships([CARE_OF_ROW], CARE_OF_CONF)["c11"][0].evidence[0]
    assert ev.raw_value == "% IDA MAE P BROWN"
    assert ev.normalized_value == "IDA MAE P BROWN"


@pytest.mark.parametrize("name", ["CO SMITH JOHN", "CO, MINH", "COLLINS, ANN",
                                  "CODY R BROWN", "COOK, AUBIE"])
def test_a_bare_leading_co_is_never_stripped(name):
    """`CO` is a real surname. Stripping it would invent a person, not clean one."""
    from app.resolution.official_import import canonical_person
    assert canonical_person(name) == name

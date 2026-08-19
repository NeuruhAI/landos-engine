"""T22's official evidence may extend the party graph. It may not embellish it."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.resolution.official_import import (
    ACCEPTED_CONFIDENCE,
    entity_relationships,
    estate_relationships,
    parse_source_document,
)

SURNAME_ROW = {
    "property_candidate_id": "c1", "apn": "09228140", "county": "Union",
    "decedent_name": "GRIFFIN TONY L HEIRS", "heir_name": "ISHADOT GRIFFIN",
    "relationship": "HEIR_OR_SUCCESSOR_CONTACT",
    "source_document": "T22|NC_ONEMAP_PARCELS|locator|conf=HIGH|SURNAME_CORROBORATED",
    "source_date": "2026-08-19",
}
STATED_ROW = {
    "property_candidate_id": "c2", "apn": "1713258583", "county": "Wake",
    "decedent_name": "FREEMAN CLYDE R", "personal_representative_name": "CLYDE R FREEMAN JR",
    "relationship": "ADMINISTRATOR",
    "source_document": "T22|WAKE_PARCELS|locator|conf=EXACT|OFFICIAL_ROLE_STATED",
    "source_date": "2026-08-19",
}


def test_a_shared_surname_does_not_become_an_heir():
    """The rule the whole module exists for.

    92 of 104 estate rows are a surname match against a decedent. Calling that
    HEIR would put an unadjudicated guess in a column the operator reads as a
    legal fact.
    """
    rels = estate_relationships([SURNAME_ROW])["c1"]
    assert [r.role for r in rels] == ["OTHER_VERIFIED_RELATION"]
    assert "not an adjudicated heirship" in rels[0].notes


def test_a_role_the_source_actually_states_is_kept():
    rels = estate_relationships([STATED_ROW])["c2"]
    assert [r.role for r in rels] == ["ADMINISTRATOR"]
    assert rels[0].evidence[0].match_method == "OFFICIAL_ROLE_STATED"


def test_review_confidence_is_never_imported():
    row = dict(SURNAME_ROW,
               source_document="T22|WAKE_PARCELS|locator|conf=REVIEW|SURNAME_CORROBORATED")
    assert estate_relationships([row]) == {}
    assert ACCEPTED_CONFIDENCE == {"EXACT", "HIGH"}


def test_a_row_that_names_nobody_produces_nothing():
    row = dict(SURNAME_ROW, heir_name="", personal_representative_name="")
    assert estate_relationships([row]) == {}


def test_no_relationship_carries_a_contact_point():
    """An address is not a phone. T22 supplies identity, never callability."""
    for rels in list(estate_relationships([SURNAME_ROW, STATED_ROW]).values()):
        for rel in rels:
            assert rel.party.contact_points == []


def test_evidence_binds_the_agency_the_record_and_when_it_was_observed():
    rel = estate_relationships([STATED_ROW])["c2"][0]
    ev = rel.evidence[0]
    assert ev.source_family == "COUNTY_PARCEL_ROLL"
    assert ev.source_name == "WAKE_PARCELS"
    assert ev.source_record_id
    assert ev.observed_at == "2026-08-19"
    assert ev.field == "party_identity"


def test_an_entity_row_with_no_named_person_stays_out_of_the_party_graph():
    row = {"property_candidate_id": "c3", "entity_legal_name": "REBUILD FELLOWSHIP INC",
           "mailing_office": "600 E UMSTEAD ST, DURHAM NC 27701", "official_name": "",
           "official_title": "", "source_document": "T22|IRS_EO_BMF_NC|loc|conf=HIGH|X",
           "source_date": "2026-08-19"}
    assert entity_relationships([row]) == {}


def test_an_entity_row_that_names_a_person_lands_as_an_entity_contact_not_an_owner():
    row = {"property_candidate_id": "c4", "entity_legal_name": "REBUILD FELLOWSHIP INC",
           "official_name": "JANE DOE", "official_title": "ORGANIZATION_CONTACT",
           "source_document": "T22|IRS_EO_BMF_NC|loc|conf=HIGH|IRS_BMF_NAME_EXACT_NORMALIZED",
           "source_date": "2026-08-19"}
    rel = entity_relationships([row])["c4"][0]
    assert rel.role == "ENTITY_MAIN_CONTACT"
    assert rel.role != "RECORDED_OWNER"


def test_a_registered_agent_stays_a_registered_agent():
    row = {"property_candidate_id": "c5", "entity_legal_name": "X LLC",
           "official_name": "AGENT CO", "official_title": "REGISTERED_AGENT",
           "source_document": "T22|IRS_EO_BMF_NC|loc|conf=HIGH|X", "source_date": "2026-08-19"}
    assert entity_relationships([row])["c5"][0].role == "REGISTERED_AGENT"


def test_source_document_parses_into_its_five_parts():
    """The parser learned T24C's grammar and gained a `corroborating_sources`
    field for it. T22 states no corroboration list, so the field is empty and
    the five parts T22 does state are unchanged."""
    meta = parse_source_document("T22|WAKE_PARCELS|http://x|conf=EXACT|OFFICIAL_ROLE_STATED")
    assert meta == {"lane": "T22", "source_name": "WAKE_PARCELS", "locator": "http://x",
                    "confidence": "EXACT", "match_method": "OFFICIAL_ROLE_STATED",
                    "corroborating_sources": ()}


@pytest.mark.skipif(not (Path.home() / "neuruh-workspace"
                         / "T22_PUBLIC_RECORD_DATA_SUPPLY_20260819" / "05_IMPORT_READY"
                         / "NC_ESTATE_IDENTITY_IMPORT.csv").exists(),
                    reason="T22 supply not present on this host")
def test_the_real_t22_files_import_without_inventing_an_heir():
    from app.resolution.official_import import load_import_csv
    base = (Path.home() / "neuruh-workspace" / "T22_PUBLIC_RECORD_DATA_SUPPLY_20260819"
            / "05_IMPORT_READY")
    rels = estate_relationships(load_import_csv(base / "NC_ESTATE_IDENTITY_IMPORT.csv"))
    roles = [r.role for v in rels.values() for r in v]
    assert "HEIR" not in roles
    assert set(roles) <= {"OTHER_VERIFIED_RELATION", "ADMINISTRATOR", "EXECUTOR"}

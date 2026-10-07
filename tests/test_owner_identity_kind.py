"""parse_owner_name kind: the four misreads found courting DeedSonar's mirror
(NeuruhAI/deedsonar#63) against 20,734 real NC owner lines."""
from app.contact.owner_identity import parse_owner_name


def kind(name: str) -> str:
    return parse_owner_name(name).kind


def test_a_decedent_named_by_surname_only_is_not_dropped():
    assert kind("SMITH HEIRS & JONES MARY") == "MIXED"
    assert kind("HEIRS OF SMITH & JONES MARY") == "MIXED"
    assert kind("HEIRS OF SMITH") == "DECEASED"
    assert "decedent_named_by_surname_only" in parse_owner_name("SMITH HEIRS & JONES MARY").notes


def test_community_associations_and_centers_are_entities():
    assert kind("WEST END COMMUNITY CENTER") == "ENTITY"
    # County rolls truncate long names.
    assert kind("CHANDLER'S RIDGE COMMUNITY ASSOCIAT") == "ENTITY"
    assert kind("EMERALD WOODS SUBDIVISION COMMUNITY") == "ENTITY"


def test_a_real_estate_trust_is_a_trust():
    assert kind("VESTA REAL ESTATE TRUST") == "TRUST"


def test_unchanged_readings():
    assert kind("SMITH JOHN") == "INDIVIDUAL"
    assert kind("SMITH JOHN HEIRS & JONES MARY") == "MIXED"
    assert kind("SMITH JOHN ESTATE") == "DECEASED"
    assert kind("JONES MARY LIFE ESTATE") == "LIFE_ESTATE"
    assert kind("SMITH FAMILY TRUST") == "TRUST"
    assert kind("ABC REAL ESTATE LLC") == "ENTITY"
    assert kind("SMITH MARY SOLE HEIR") == "INDIVIDUAL"
    assert kind("ALLEN JOHN") == "INDIVIDUAL"
    assert kind("") == "EMPTY"

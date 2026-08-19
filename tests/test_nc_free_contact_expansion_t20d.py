"""T20D: the ZIP-locality arbiter, and the house-number/ZIP confusion it exposed.

Fixtures are synthetic. No real voter, owner, parcel, or phone appears here.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.contact.address_normalize import parse_address, strip_leading_house
from app.contact.official_fetch import official_url_allowed
from app.contact.zip_locality import (
    DIFFERENT_LOCALITY,
    GAZETTEER_URL,
    NON_DELIVERY_ZIP,
    SAME_LOCALITY,
    UNKNOWN_LOCALITY,
    Zcta,
    classify_zip_variance,
    haversine_km,
    load_gazetteer,
    zcta_radius_km,
    zip_separation_km,
)


# --------------------------------------------------------------------------
# The defect the TIER_C precision court surfaced: a five-digit house number was
# being read as a ZIP, which then deleted the house from the parsed address.
# --------------------------------------------------------------------------

def test_five_digit_house_number_is_not_read_as_a_zip():
    addr = parse_address("17104 NIBLICK LN CORNELIUS NC")
    assert addr.house == "17104"
    assert addr.street_name == "NIBLICK"
    assert addr.zip5 == ""


def test_five_digit_house_with_a_real_trailing_zip_keeps_both():
    addr = parse_address("17104 NIBLICK LN CORNELIUS NC 28031")
    assert addr.house == "17104"
    assert addr.zip5 == "28031"


def test_explicit_zip_argument_still_wins():
    addr = parse_address("17104 NIBLICK LN", "CORNELIUS", "NC", "28031")
    assert (addr.house, addr.zip5) == ("17104", "28031")


def test_four_digit_house_was_never_affected():
    addr = parse_address("3222 PERIWINKLE CT CHARLOTTE NC")
    assert (addr.house, addr.street_name, addr.zip5) == ("3222", "PERIWINKLE", "")


def test_strip_leading_house_leaves_a_house_less_line_alone():
    assert strip_leading_house("MAIN ST") == "MAIN ST"
    assert strip_leading_house("100 MAIN ST") == "MAIN ST"
    assert strip_leading_house("") == ""


def test_house_number_eaten_as_zip_would_have_broken_matching():
    """The regression in the terms that matter: two spellings of one address."""
    owner = parse_address("13523 WALKERS CREEK DR CHARLOTTE NC")
    voter = parse_address("13523 WALKERS CREEK DR", "CHARLOTTE", "NC", "28273")
    assert owner.house == voter.house == "13523"
    assert owner.street_name == voter.street_name


# --------------------------------------------------------------------------
# The arbiter must be a DIFFERENT authority from the source under test.
# --------------------------------------------------------------------------

def test_census_gazetteer_is_an_allowed_official_source():
    assert official_url_allowed(GAZETTEER_URL)


def test_arbitrary_and_paid_hosts_are_still_refused():
    for url in (
        "https://api.tracerfy.com/v1/skiptrace",
        "https://api.batchdata.com/lookup",
        "https://lookups.twilio.com/v2/PhoneNumbers/+19195550100",
        "https://www.truepeoplesearch.com/results",
        "http://www2.census.gov/geo/docs/maps-data/data/gazetteer/x.zip",
    ):
        assert not official_url_allowed(url)


def _gaz():
    # Two abutting ZCTAs ~4 km apart, and one 200 km away.
    return {
        "28078": Zcta("28078", 35.404779, -80.866541, 63.726),
        "28031": Zcta("28031", 35.437000, -80.880000, 30.000),
        "27601": Zcta("27601", 35.773661, -78.634563, 1.818),
    }


def test_identical_zip_is_the_same_locality():
    v, d = classify_zip_variance("28078", "28078", _gaz())
    assert v == SAME_LOCALITY and d["reason"] == "identical_zip"


def test_abutting_zctas_are_one_locality():
    v, _ = classify_zip_variance("28031", "28078", _gaz())
    assert v == SAME_LOCALITY


def test_distant_zctas_are_different_localities():
    v, _ = classify_zip_variance("27601", "28078", _gaz())
    assert v == DIFFERENT_LOCALITY


def test_a_zip_with_no_tabulation_area_is_a_mailing_artifact_not_a_place():
    """A PO-Box-only ZIP has no ZCTA. It cannot be a competing physical place."""
    v, d = classify_zip_variance("28070", "28078", _gaz())
    assert v == NON_DELIVERY_ZIP
    assert "po_box" in d["reason"]


def test_unknown_on_both_sides_is_never_silently_treated_as_agreement():
    v, _ = classify_zip_variance("99999", "99998", _gaz())
    assert v == UNKNOWN_LOCALITY


def test_parcel_point_can_overturn_abutting_zctas():
    """ZIPs may abut while the parcel sits far from one of them."""
    v, _ = classify_zip_variance("28031", "28078", _gaz(), parcel_lat=35.77, parcel_lon=-78.63)
    assert v == DIFFERENT_LOCALITY


def test_zip_separation_and_radius_are_none_for_unknown_zips():
    assert zip_separation_km("28078", "00000", _gaz()) is None
    assert zcta_radius_km("00000", _gaz()) is None


def test_haversine_is_symmetric_and_zero_on_itself():
    assert haversine_km(35.0, -80.0, 35.0, -80.0) == pytest.approx(0.0)
    a = haversine_km(35.0, -80.0, 36.0, -81.0)
    b = haversine_km(36.0, -81.0, 35.0, -80.0)
    assert a == pytest.approx(b)


def test_gazetteer_parses_the_official_tab_separated_layout(tmp_path: Path):
    payload = (
        "GEOID\tALAND\tAWATER\tALAND_SQMI\tAWATER_SQMI\tINTPTLAT\tINTPTLONG\n"
        "28078\t165049000\t2000\t63.726\t0.001\t35.404779\t-80.866541\n"
        "00000\t0\t0\t0.0\t0.0\t\t\n"
    )
    zpath = tmp_path / "gaz.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("2024_Gaz_zcta_national.txt", payload)
    gaz = load_gazetteer(zpath)
    assert set(gaz) == {"28078"}          # the coordinate-less row is dropped
    assert gaz["28078"].land_sqmi == pytest.approx(63.726)


def test_missing_gazetteer_is_empty_not_an_exception(tmp_path: Path):
    assert load_gazetteer(tmp_path / "nope.zip") == {}


# --------------------------------------------------------------------------
# `property_zip` on the NC estate is a copy of `mailing_zip`, not the parcel's
# ZIP. Reading it as the property's ZIP lets an absentee owner's out-of-state
# mailing ZIP masquerade as the parcel's, which blocks the true address match.
# --------------------------------------------------------------------------

from app.contact.estate_fields import property_zip_for, property_zip_is_mailing_copy


def test_property_zip_that_merely_copies_the_mailing_zip_is_refused():
    row = {
        "property_address_raw": "3222 PERIWINKLE CT CHARLOTTE NC",
        "property_zip": "85205",
        "mailing_zip": "85205",
    }
    assert property_zip_is_mailing_copy(row)
    assert property_zip_for(row) == ""


def test_a_zip_on_the_property_line_is_the_property_zip():
    row = {
        "property_address_raw": "2501 N ROXBORO ST DURHAM NC 27704",
        "property_zip": "27704",
        "mailing_zip": "27704",
    }
    assert property_zip_for(row) == "27704"


def test_the_property_line_wins_over_a_disagreeing_field():
    row = {
        "property_address_raw": "2501 N ROXBORO ST DURHAM NC 27704",
        "property_zip": "85205",
        "mailing_zip": "85205",
    }
    assert property_zip_for(row) == "27704"


def test_a_declared_property_zip_that_differs_from_mailing_is_trusted():
    row = {"property_address_raw": "100 MAIN ST", "property_zip": "28202", "mailing_zip": "28277"}
    assert property_zip_for(row) == "28202"
    assert not property_zip_is_mailing_copy(row)


def test_five_digit_house_is_not_harvested_as_a_property_zip():
    row = {"property_address_raw": "17104 NIBLICK LN CORNELIUS NC", "property_zip": "", "mailing_zip": "11579"}
    assert property_zip_for(row) == ""


def test_empty_row_yields_no_property_zip():
    assert property_zip_for({}) == ""
    assert not property_zip_is_mailing_copy({})


def test_contaminated_zip_no_longer_defeats_an_exact_address_match():
    """The regression this fix exists to prevent, stated end to end."""
    row = {
        "property_address_raw": "3222 PERIWINKLE CT CHARLOTTE NC",
        "property_city": "CHARLOTTE",
        "property_zip": "85205",   # Arizona mailing ZIP wearing the property's name
        "mailing_zip": "85205",
    }
    owner = parse_address(row["property_address_raw"], row["property_city"], "NC",
                          property_zip_for(row))
    voter = parse_address("3222 PERIWINKLE CT", "CHARLOTTE", "NC", "28269")
    from app.contact.address_normalize import exact_core_match
    assert exact_core_match(owner, voter)

    poisoned = parse_address(row["property_address_raw"], row["property_city"], "NC",
                             row["property_zip"])
    assert not exact_core_match(poisoned, voter)


# --------------------------------------------------------------------------
# Trustees the tax roll names. The roll writes the controlling person into the
# owner string — "TEW, B DAVID TRUSTEE" — exactly as it writes an heir onto an
# estate parcel. The trust's own name is never a person.
# --------------------------------------------------------------------------

from app.contact.owner_class import owner_route_class, phone_role_for
from app.contact.owner_identity import parse_owner_name, trustee_people_on_roll


def _names(owner: str):
    return sorted(f"{p.first} {p.last}" for p in trustee_people_on_roll(owner))


def test_roll_named_trustees_are_recovered():
    assert _names("TEW, B DAVID TRUSTEE & TEW, PATRICIA TRUSTEE") == ["PATRICIA TEW"]
    assert _names("JOHNSON, EMMA TURNER TRUSTEE & TURNER, TERRY TRUSTEE") == [
        "EMMA JOHNSON", "TERRY TURNER"]


def test_the_trusts_own_name_is_never_a_person():
    assert _names("THE D & M REVOCABLE TRUST") == []
    assert _names("SNH SE PROPERTIES TRUST") == []
    assert _names("FARINA KINGSLEY BHI TRUST LLC") == []
    assert _names("HATCH CT TRUST") == []


def test_a_trust_segment_beside_a_trustee_is_not_harvested():
    """Only the TRUSTEE-marked segment yields a person."""
    assert _names("HANDAL, JEANETTE F TRUSTEE & JEANETTE F. HANDAL REVOCABLE TRUST") == [
        "JEANETTE HANDAL"]
    assert _names("ANDERSON, JONATHAN W. TRUSTEE & 724 KING RICHARD ROAD TRUST") == [
        "JONATHAN ANDERSON"]


def test_trustee_of_the_x_trust_describes_the_trust_not_a_person():
    assert _names("TRUSTEE OF THE ART SOMMERVILLE & RO") == []
    assert _names("ROSTOTKA, ANASTASIIA TRUSTEE & TRUSTEE OF GRETA IRREVCOABLE TRUS") == [
        "ANASTASIIA ROSTOTKA"]


def test_an_initial_only_first_name_is_not_a_named_trustee():
    """The matcher needs a real first name; an initial is not identification."""
    assert _names("SMITH, J TRUSTEE") == []


def test_a_trust_stays_a_trust_for_routing():
    for owner in ("TEW, B DAVID TRUSTEE & TEW, PATRICIA TRUSTEE",
                  "THE D & M REVOCABLE TRUST"):
        parsed = parse_owner_name(owner)
        assert parsed.kind == "TRUST"
        assert owner_route_class(owner, parsed) == "TRUST"


def test_a_trustee_phone_is_labelled_a_trustee_phone_not_an_owner_phone():
    assert phone_role_for("NAMED_TRUSTEE_ON_ROLL", "TRUST", "NCSBE_PUBLIC_VOTER_FILE") == "TRUSTEE_PERSONAL"
    assert phone_role_for("OWNER", "LIVING_PERSON", "NCSBE_PUBLIC_VOTER_FILE") == "OWNER_PERSONAL"


def test_registered_agent_and_attorney_phones_are_still_never_owner_phones():
    for role in ("ENTITY",):
        assert phone_role_for(role, "LLC", "NCSBE_PUBLIC_VOTER_FILE") == "ENTITY_MAIN"
    assert phone_role_for("NAMED_TRUSTEE_ON_ROLL", "TRUST", "IRS_EO_ICO_NCSBE") == "BUSINESS_OFFICE"


# --------------------------------------------------------------------------
# Hyphenated and apostrophised street names.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("15003 HUNTERSVILLE-CONCORD RD", "15003 HUNTERSVILLE CONCORD RD"),
    ("11212 JOHNSON-DAVIS RD", "11212 JOHNSON DAVIS RD"),
    ("9200 MT HOLLY-HUNTERSVILLE RD", "9200 MT HOLLY HUNTERSVILLE RD"),
    ("100 O'HARA ST", "100 OHARA ST"),
])
def test_punctuation_in_a_street_name_does_not_split_one_street_into_two(a, b):
    pa, pb = parse_address(a), parse_address(b)
    assert pa.street_name == pb.street_name
    assert pa.house == pb.house


def test_genuinely_different_streets_still_differ():
    assert parse_address("100 CUPWORTH CT").street_name != parse_address("100 ULSTEN LN").street_name


# --------------------------------------------------------------------------
# The one case where a property ZIP equal to the mailing ZIP is real: the owner
# lives at the property, so a single ZIP correctly describes both addresses.
# --------------------------------------------------------------------------

from app.contact.estate_fields import owner_occupies_property


def test_owner_occupied_property_keeps_its_zip():
    row = {
        "property_address_raw": "5608 BARNSDALE LN MINT HILL NC",
        "mailing_address": "5608 BARNSDALE RD 28227",
        "property_zip": "28227",
        "mailing_zip": "28227",
    }
    assert owner_occupies_property(row)
    assert property_zip_for(row) == "28227"


def test_owner_occupancy_survives_a_spacing_difference_in_the_street():
    row = {
        "property_address_raw": "9604 FARMRIDGE LN CHARLOTTE NC",
        "mailing_address": "9604 FARM RIDGE LN 28105-4402",
        "property_zip": "28105-4402",
        "mailing_zip": "28105-4402",
    }
    assert owner_occupies_property(row)
    assert property_zip_for(row) == "28105"


def test_an_absentee_owner_does_not_lend_their_zip_to_the_parcel():
    row = {
        "property_address_raw": "8941 BLAIR RD MINT HILL NC",
        "mailing_address": "9335 MORGAN GLENN DR 28227",
        "property_zip": "28227",
        "mailing_zip": "28227",
    }
    assert not owner_occupies_property(row)
    assert property_zip_for(row) == ""


def test_out_of_state_mailing_never_becomes_the_parcel_zip():
    row = {
        "property_address_raw": "3222 PERIWINKLE CT CHARLOTTE NC",
        "mailing_address": "5510 DUNCAN ST 85205-3082",
        "property_zip": "85205",
        "mailing_zip": "85205",
    }
    assert not owner_occupies_property(row)
    assert property_zip_for(row) == ""


def test_owner_occupancy_needs_both_addresses_to_be_parsable():
    assert not owner_occupies_property({"property_address_raw": "", "mailing_address": "100 MAIN ST"})
    assert not owner_occupies_property({"property_address_raw": "100 MAIN ST", "mailing_address": ""})
    assert not owner_occupies_property({})

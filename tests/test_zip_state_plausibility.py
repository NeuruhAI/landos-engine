"""A ZIP that cannot belong to the property's state must not survive as one."""
from __future__ import annotations

import pytest

from app.contact.estate_fields import property_zip_for
from app.contact.zip_state import ZIP3_RANGES, state_for_zip, zip_plausible_for_state

KNOWN = [
    ("28205", "NC"), ("27610", "NC"), ("28025", "NC"),
    ("29662", "SC"), ("30017", "GA"), ("80220", "CO"),
    ("91201", "CA"), ("20012", "DC"), ("11238", "NY"),
    ("49022", "MI"), ("02139", "MA"), ("99501", "AK"),
]


@pytest.mark.parametrize("zip5,state", KNOWN)
def test_a_real_zip_is_plausible_for_its_own_state(zip5, state):
    assert zip_plausible_for_state(zip5, state)


@pytest.mark.parametrize("zip5,state", KNOWN)
def test_a_real_zip_is_not_plausible_for_a_state_it_is_not_in(zip5, state):
    other = "NC" if state != "NC" else "CA"
    assert not zip_plausible_for_state(zip5, other)


def test_every_state_has_at_least_one_range():
    assert len(ZIP3_RANGES) >= 50
    assert all(ranges for ranges in ZIP3_RANGES.values())


def test_no_opinion_for_a_state_we_have_no_ranges_for():
    assert zip_plausible_for_state("28205", "ZZ")


def test_the_glendale_zip_on_a_charlotte_parcel_is_refused():
    """The row that made this necessary."""
    row = {
        "property_address_raw": "809 MATTHEWS TOWNSHIP PY MATTHEWS NC",
        "property_state": "NC",
        "property_zip": "91201-5025",
        "mailing_zip": "",
    }
    assert property_zip_for(row) == ""


def test_a_zip_plus_four_that_IS_plausible_is_kept_as_five():
    row = {
        "property_address_raw": "7822 CLOUGHY CR CHARLOTTE NC",
        "property_state": "NC",
        "property_zip": "28212-6551",
        "mailing_zip": "",
    }
    assert property_zip_for(row) == "28212"


def test_the_property_line_still_wins_when_it_carries_a_plausible_zip():
    row = {
        "property_address_raw": "61 UNION ST S CONCORD NC 28025",
        "property_state": "NC",
        "property_zip": "",
        "mailing_zip": "",
    }
    assert property_zip_for(row) == "28025"


def test_a_mailing_zip_copy_is_still_refused_when_the_owner_is_elsewhere():
    row = {
        "property_address_raw": "1000 W ROCKY RIVER RD CHARLOTTE NC",
        "property_city": "CHARLOTTE",
        "property_state": "NC",
        "property_zip": "28213",
        "mailing_address": "9025 SEAMILL ROAD",
        "mailing_city": "CHARLOTTE",
        "mailing_zip": "28213",
    }
    assert property_zip_for(row) == ""


def test_state_for_zip_is_unambiguous_or_empty():
    assert state_for_zip("28205") == "NC"
    assert state_for_zip("not-a-zip") == ""

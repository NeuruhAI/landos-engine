"""
The identity court for county-address -> coordinate resolution of unmapped
estate rows.

A Census "Match" is not proof the row belongs where Census put it. These pin the
rules that decide whether a geocode becomes a map pin: the address must actually
carry a city, the coordinates must land in the row's own county, and anything
short of that is reported rather than mapped.
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.resolve_estate_address_geocode import (  # noqa: E402
    EXACT_CONFIDENCE, NON_EXACT_CONFIDENCE, court, split_address,
)


def row(**over):
    base = {
        "property_key": "MI:26107:07 027 010 200",
        "county_fips": "26107",
        "state": "MI",
        "apn": "07 027 010 200",
        "street": "13930 TROUT ST",
        "city": "MECOSTA",
    }
    base.update(over)
    return base


def hit(**over):
    base = {
        "match": "Match",
        "exact": "Exact",
        "matched_address": "13930 TROUT ST, MECOSTA, MI, 49332",
        "coords": "-85.265391,43.667294",
        "statefp": "26",
        "countyfp": "107",
    }
    base.update(over)
    return base


# ---- address splitting ------------------------------------------------------

def test_splits_the_county_combined_address():
    assert split_address("7125 20 MILE RD, EVART") == ("7125 20 MILE RD", "EVART")


def test_splits_on_the_LAST_comma_so_street_commas_survive():
    assert split_address("288 ANGELL AVE, APT 2, BARRYTON") == ("288 ANGELL AVE, APT 2", "BARRYTON")


def test_an_address_with_no_city_half_is_refused():
    # The county name must never be substituted for a missing city.
    assert split_address("0 HOLLY SPRINGS RD 00000") == (None, None)
    assert split_address("WOODWARD AVE") == (None, None)


def test_blank_halves_are_refused():
    assert split_address("13930 TROUT ST,") == (None, None)
    assert split_address(", MECOSTA") == (None, None)
    assert split_address("") == (None, None)
    assert split_address(None) == (None, None)


# ---- the county check is the identity court ---------------------------------

def test_a_verified_match_resolves_with_provenance():
    out = court(row(), hit())
    assert out["outcome"] == "RESOLVED"
    assert out["coordinate_class"] == "ADDRESS_GEOCODE"
    assert out["latitude"] == pytest.approx(43.667294)
    assert out["longitude"] == pytest.approx(-85.265391)
    assert out["county_verified"] == "26107"
    # provenance the estate court requires
    for key in ("method", "precision", "confidence", "source", "retrieved_at",
                "apn", "county_fips", "matched_address"):
        assert out.get(key), key


def test_a_hit_in_the_wrong_county_is_rejected_not_downgraded():
    # Real case: 9950 E M-21, OVID, MI — the row says Clinton (26037), Census
    # places it in Shiawassee (26155). Ovid straddles the line. A wrong county
    # is a wrong parcel.
    out = court(row(county_fips="26037"), hit(statefp="26", countyfp="155"))
    assert out["outcome"] == "COUNTY_MISMATCH"
    assert "latitude" not in out


def test_a_non_exact_match_is_kept_but_scored_lower():
    out = court(row(), hit(exact="Non_Exact"))
    assert out["outcome"] == "RESOLVED"
    assert out["precision"] == "interpolated"
    assert out["confidence"] == NON_EXACT_CONFIDENCE
    assert NON_EXACT_CONFIDENCE < EXACT_CONFIDENCE


def test_no_match_is_reported_not_approximated():
    out = court(row(), {"match": "No_Match", "exact": ""})
    assert out["outcome"] == "NO_MATCH"
    assert "latitude" not in out


def test_a_missing_census_row_is_reported():
    assert court(row(), None)["outcome"] == "NO_MATCH"


def test_a_match_without_usable_coordinates_is_refused():
    out = court(row(), hit(coords="not-a-coordinate"))
    assert out["outcome"] == "NO_COORDS"
    assert "latitude" not in out


def test_this_path_never_emits_COARSE():
    # The 141 ZIP-centroid rows are held at confidence 0.25 on purpose; nothing
    # here may add to that class or promote them.
    out = court(row(), hit())
    assert out["coordinate_class"] != "COARSE"

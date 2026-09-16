from app.models import HuntRequest
from app.national_scope import NATIONAL_HUNT_FIPS, national_counties


def test_national_census_is_full_and_deterministic():
    rows = national_counties()
    assert len(rows) == 3235
    assert [row["fips"] for row in rows] == sorted(row["fips"] for row in rows)
    assert rows[0]["fips"] == "01001"


def test_empty_hunt_request_expands_to_national_census():
    req = HuntRequest(territory=[])
    assert len(req.territory) == 3235
    assert req.territory[0].fips == "01001"
    assert req.territory[-1].fips == "56045"


def test_reserved_national_marker_expands_to_national_census():
    req = HuntRequest(territory=[{"fips": NATIONAL_HUNT_FIPS}])
    assert len(req.territory) == 3235
    assert all(entry.fips and len(entry.fips) == 5 for entry in req.territory)


def test_national_marker_cannot_be_mixed_with_selected_counties():
    try:
        HuntRequest(territory=[{"fips": NATIONAL_HUNT_FIPS}, {"fips": "26021"}])
    except ValueError as exc:
        assert "cannot be combined" in str(exc)
    else:
        raise AssertionError("mixed national and explicit county scope must fail closed")

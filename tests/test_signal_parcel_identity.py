"""
Guards on signal -> parcel identity resolution.

Two ways this lane can fabricate a property, both found live on 2026-08-19 and
both fixed here:

  1. resolving from a CITY CENTROID. Three Niles listings share one centroid, so
     all three "resolved" to whichever parcel sits under the town marker.
  2. accepting a county PLACEHOLDER polygon. Berrien publishes zero-acre records
     for water/right-of-way; PIN 99-56-9999-9999-99-9 returns Acres 0.0 with no
     address, while a real neighbour returns Acres 8.0 and Propaddrnu 9250.
"""
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

SOURCE = (REPO / "scripts" / "resolve_signal_parcel_identity.py").read_text()
SENTINEL = re.compile(
    re.search(r'SENTINEL_PIN = re\.compile\(r"([^"]+)"\)', SOURCE).group(1))


def test_only_exact_coordinates_are_resolved():
    # A centroid is a town marker, not a location. The query must say so.
    assert "geo_precision = 'exact'" in SOURCE, (
        "resolving a spatial intersect from a city centroid fabricates identity")


def test_the_county_placeholder_parcel_is_rejected():
    assert SENTINEL.match("99-56-9999-9999-99-9")
    assert SENTINEL.match("99-76-9999-9999-99-9")


def test_real_berrien_pins_are_not_rejected():
    for pin in ("11-02-0014-0015-03-0", "11-12-5220-0128-00-0",
                "11-17-0006-0001-04-0", "11-08-0018-0004-02-7"):
        assert not SENTINEL.match(pin), pin


def test_a_county_with_no_resolver_is_reported_not_guessed():
    assert "NO_RESOLVER_FOR_COUNTY" in SOURCE
    assert '"berrien-mi": "berrien_parcels_spatial_intersect"' in SOURCE
    # St. Joseph IN has no configured chain; it must not borrow Berrien's layer.
    assert '"st-joseph-in": "berrien' not in SOURCE


def test_ambiguous_and_empty_intersects_are_distinct_refusals():
    for verdict in ("AMBIGUOUS_MULTIPLE_PARCELS", "NO_PARCEL_AT_POINT",
                    "SENTINEL_PARCEL_REJECTED"):
        assert verdict in SOURCE, verdict

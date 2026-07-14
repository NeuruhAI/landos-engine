"""
test_pip_match_signals.py — the PIP resolver contract (geometry_infra_spec Part 3).
No DB, no network: SQL invariants + the pure classification rules the spec pins
(unambiguous-only, named geometry gates, AI-never-deletes).
"""
import re

from scripts.pip_match_signals import (LEAD_CANDIDATES, MATCH, PIP_CLASSIFY,
                                       STAMP, classify, _sources)


def _flat(s):
    return re.sub(r"\s+", " ", s.lower())


# ---- SQL invariants ----------------------------------------------------------------

def test_pip_sql_is_4326_contains_over_own_county_new_unmatched_apnless_rows():
    s = _flat(PIP_CLASSIFY)
    assert "st_contains(g.geom, st_setsrid(st_makepoint(p.lng, p.lat), 4326))" in s
    assert "g.county_fips = p.county_fips" in s          # never cross-county PIP
    assert "ds.status = 'new'" in s
    assert "ds.matched_lead_id is null" in s
    assert "coalesce(ds.parcel_apn, '') = ''" in s       # only spatial_deferred rows
    assert "count(distinct h.parcel_apn)" in s           # ambiguity is measured, not assumed


def test_pip_sql_guards_against_garbage_coordinates():
    # lat/lng are jsonb text — a non-numeric payload must be filtered, not cast-crashed.
    assert PIP_CLASSIFY.count("~ '^-?[0-9]+(\\.[0-9]+)?$'") == 2


def test_stamp_is_additive_guarded_and_provenance_bearing():
    s = _flat(STAMP)
    assert "matched_lead_id is null" in s and "status = 'new'" in s
    assert "coalesce(parcel_apn, '') = ''" in s          # stamp only the untouched state
    assert "|| jsonb_build_object" in s                  # merge, never replace payload
    assert "'pip_resolve'" in s and "'pip_parcel'" in s


def test_match_mirrors_the_temporal_resolver_contract():
    s = _flat(MATCH)
    assert "set matched_lead_id" in s and "status = 'matched'" in s
    assert "matched_lead_id is null" in s and "status = 'new'" in s


def test_no_sql_ever_deletes():
    for sql in (PIP_CLASSIFY, LEAD_CANDIDATES, STAMP, MATCH):
        assert "delete" not in sql.lower()


# ---- classification rules ----------------------------------------------------------

GEOM = {"18141": 118553, "18091": 64657}   # Berrien 26021 deliberately absent


def _row(fips="18141", n=1):
    return {"county_fips": fips, "n_parcels": n}


def test_unambiguous_containment_resolves():
    assert classify(_row(n=1), GEOM) == "resolved"


def test_two_polygons_is_ambiguous_never_a_guess():
    assert classify(_row(n=2), GEOM) == "ambiguous"


def test_point_outside_every_polygon_is_outside_not_dark():
    assert classify(_row(n=0), GEOM) == "outside"


def test_county_without_geometry_is_a_named_gate():
    # Berrien: signals held, not guessed — even if n_parcels were somehow nonzero.
    assert classify(_row(fips="26021", n=0), GEOM) == "geometry_dark"
    assert classify(_row(fips="26021", n=1), GEOM) == "geometry_dark"


def test_sources_normalize_dedupe_and_default():
    assert _sources(None) == ["ground_lease"]
    assert _sources(["b", "a", "a", " "]) == ["a", "b"]

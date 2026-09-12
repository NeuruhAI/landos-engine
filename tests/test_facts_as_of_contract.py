"""FACTS DATE contract: enrichment -> ranked universe -> master export.

DeedSonar stores one date per estate row, `source_as_of`, and ages the estate
by it. On 2026-09-12 every one of the 20,734 NC rows read 2026-08-12 because
export_opportunities.py stamped the literal "2026-08-12" into `created_at` and
the loader read that as the facts date. These tests pin the replacement:

  facts_as_of        the newest date a county payload itself carried about the
                     property (landos-ingest detail_json.observed_event_date)
  facts_as_of_basis  observed_event_date | unknown

and that nothing else -- a declared as_of, event_date, retrieved_at/fetched_at,
created_at, exported_at -- can stand in for it.
"""
from __future__ import annotations

import csv
from pathlib import Path

from scripts import build_nc_dps_public_enrichment as core
from scripts import export_opportunities as xf


def _sig(parcel="0001", *, observed=None, retrieved=None, declared=None, event_date=None):
    d = {"tax_balance": 10.0, "source_url": "https://county.example/list"}
    if observed is not None:
        d["observed_event_date"] = observed
    if retrieved is not None:
        d["retrieved_at"] = retrieved
    if declared is not None:
        d["source_as_of"] = declared
    return {
        "geography": "37183", "parcel_apn": parcel, "owner_name": "DOE JANE",
        "raw_address": "1 MAIN ST", "source_type": "taxdelinquent",
        "event_date": event_date, "fetched_at": "2026-09-14T12:45:00Z",
        "detail_json": d,
    }


# --- enrichment: facts_as_of over a property's signals --------------------------

def test_newest_observed_payload_date_wins():
    got = core.facts_as_of([
        _sig(observed="2026-09-01", retrieved="2026-09-14T12:45:00Z"),
        _sig(observed="2026-09-10", retrieved="2026-09-14T12:45:00Z"),
    ])
    assert got == ("2026-09-10", "observed_event_date")


def test_declared_constant_event_date_and_fetch_time_are_not_facts():
    # The v3 adapters stamp event_date with their hardcoded as_of; the factory
    # records the declared as_of separately. Neither is an observation.
    got = core.facts_as_of([_sig(declared="2026-08-12", event_date="2026-08-12")])
    assert got == ("", "unknown")


def test_a_payload_cannot_be_dated_after_it_was_retrieved():
    got = core.facts_as_of([_sig(observed="2026-09-20", retrieved="2026-09-14T12:45:00Z")])
    assert got == ("", "unknown")


def test_malformed_observed_date_is_unknown_not_guessed():
    assert core.facts_as_of([_sig(observed="09/10/2026")]) == ("", "unknown")
    assert core.facts_as_of([_sig(observed="2026-02-30")]) == ("", "unknown")
    assert core.facts_as_of([]) == ("", "unknown")


def test_aggregate_appends_facts_columns_without_moving_any_column():
    rows = core.aggregate(
        [_sig("0001", observed="2026-09-10", retrieved="2026-09-14"), _sig("0002")],
        parcel_context={},
    )
    by_parcel = {r["parcel_apn"]: r for r in rows}
    assert by_parcel["0001"]["facts_as_of"] == "2026-09-10"
    assert by_parcel["0001"]["facts_as_of_basis"] == "observed_event_date"
    assert by_parcel["0002"]["facts_as_of"] == ""
    assert by_parcel["0002"]["facts_as_of_basis"] == "unknown"
    keys = list(rows[0])
    assert keys[-2:] == ["facts_as_of", "facts_as_of_basis"]
    assert keys.index("contact_authorized") == len(keys) - 3


def test_mecklenburg_match_no_longer_invents_2026_08_04():
    unresolved = [{"staging_id": "s1", "owner_name": "DOE JANE", "source_url": "u"}]
    matches = {core.norm_owner("DOE JANE"): [{"parno": "12345678", "siteadd": "1 MAIN ST"}]}
    signals, _ = core.build_mecklenburg_signals(unresolved, matches)
    assert signals and signals[0]["event_date"] is None
    assert core.facts_as_of(signals) == ("", "unknown")


# --- export: the master carries the facts date, and only that ------------------

def _ranked_row(**over):
    base = {
        "rank": "1", "county": "Wake", "county_fips": "37183", "parcel_apn": "0001",
        "owner_name": "DOE JANE", "property_address": "1 MAIN ST",
        "curative_priority_score": "50", "priority_tier": "C",
        "recipe_id": "published_tax_distress", "source_types": "taxdelinquent",
        "trigger_classes": "tax_distress", "score_reasons": "published_tax_distress:+10",
        "scoring_version": "nc_curative_rank.v3", "recipe_version": "nc_curative_recipes.v1",
    }
    base.update(over)
    return base


def _prov(**over):
    p = {"source_run_id": "s", "scoring_run_id": "NOT_DEFINED", "export_run_id": "e",
         "schema_version": xf.SCHEMA_VERSION, "created_at": "", "exported_at": "t"}
    p.update(over)
    return p


def test_schema_appends_exactly_two_facts_columns():
    assert xf.SCHEMA_COLUMNS[-2:] == ["facts_as_of", "facts_as_of_basis"]
    assert xf.SCHEMA_COLUMNS[-5:-2] == ["schema_version", "created_at", "exported_at"]
    assert len(xf.SCHEMA_COLUMNS) == len(set(xf.SCHEMA_COLUMNS))


def test_observed_facts_date_passes_through_verbatim():
    r = xf.map_row(_ranked_row(facts_as_of="2026-09-10", facts_as_of_basis="observed_event_date"),
                   provenance=_prov())
    assert (r["facts_as_of"], r["facts_as_of_basis"]) == ("2026-09-10", "observed_event_date")


def test_any_other_basis_or_shape_exports_unknown():
    for over in (
        {"facts_as_of": "2026-08-12", "facts_as_of_basis": "declared_as_of"},
        {"facts_as_of": "2026-08-12"},                                   # no basis
        {"facts_as_of": "2026-13-01", "facts_as_of_basis": "observed_event_date"},
        {"facts_as_of": "2026-09-10T00:00:00Z", "facts_as_of_basis": "observed_event_date"},
        {},                                                               # pre-contract universe
    ):
        r = xf.map_row(_ranked_row(**over), provenance=_prov())
        assert (r["facts_as_of"], r["facts_as_of_basis"]) == ("", "unknown"), over


def test_created_at_is_never_a_remembered_literal(tmp_path: Path):
    src = tmp_path / "NC_CURATIVE_ALL.csv"
    row = _ranked_row(facts_as_of="2026-09-10", facts_as_of_basis="observed_event_date")
    with open(src, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row))
        w.writeheader()
        w.writerow(row)

    def master(out: Path, **kw):
        m = xf.build(src, out, state=None, county=None, recipe=None, scope="full",
                     pending_signals=None, pending_audit=None, meck_staging=None,
                     exported_at="2026-09-14T13:00:00+00:00", export_run_id="exprun_t", **kw)
        path = next(p for p in out.rglob("ALL_OPPORTUNITIES_MASTER.csv"))
        return m, list(csv.DictReader(open(path, newline="")))

    m, rows = master(tmp_path / "a")
    assert rows[0]["created_at"] == ""                     # unstated -> UNKNOWN
    assert rows[0]["facts_as_of"] == "2026-09-10"          # facts ride separately
    assert m["facts_as_of"]["basis_counts"] == {"observed_event_date": 1}
    assert m["facts_as_of"]["min"] == m["facts_as_of"]["max"] == "2026-09-10"

    _, rows = master(tmp_path / "b", ranked_universe_date="2026-09-14")
    assert rows[0]["created_at"] == "2026-09-14"
    assert rows[0]["facts_as_of"] == "2026-09-10"          # provenance never overwrites facts

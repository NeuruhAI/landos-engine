"""estate_producer: refusal rules and the revise-row contract.

The stage CLIs it chains have their own tests. What is pinned here is what the
wrapper decides: which factory runs it will consume, and what it lets reach
data_estate_revise_v1. In particular, a partial universe's global_rank never
reaches the estate.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import estate_producer as ep


def _factory_run(tmp_path: Path, *, state="CLEAN", tamper=False) -> Path:
    run = tmp_path / "run-x"
    run.mkdir()
    jsonl = run / "normalized_signals.jsonl"
    jsonl.write_text('{"signal_id":"a"}\n')
    digest = hashlib.sha256(jsonl.read_bytes()).hexdigest()
    (run / "run_receipt.json").write_text(json.dumps({
        "run_id": "run-x", "code_commit": {"state": state, "sha": "abc"},
        "estate": {"normalized_sha256": "0" * 64 if tamper else digest},
        "health_by_verdict": {"HEALTHY": 1}, "outcome_by_source": {"landed": 1}}))
    return run


def test_a_clean_run_is_accepted_and_bound_by_digest(tmp_path):
    got = ep.verify_factory_run(_factory_run(tmp_path), allow_dirty=False)
    assert got["run_id"] == "run-x" and len(got["normalized_sha256"]) == 64


def test_a_dirty_run_is_refused_unless_explicitly_allowed(tmp_path):
    run = _factory_run(tmp_path, state="DIRTY")
    with pytest.raises(ep.ProducerError, match="not CLEAN"):
        ep.verify_factory_run(run, allow_dirty=False)
    assert ep.verify_factory_run(run, allow_dirty=True)["code_commit"]["state"] == "DIRTY"


def test_a_jsonl_that_does_not_match_its_receipt_is_refused(tmp_path):
    with pytest.raises(ep.ProducerError, match="sha256"):
        ep.verify_factory_run(_factory_run(tmp_path, tamper=True), allow_dirty=True)


def test_a_receipt_without_a_digest_is_refused_not_trusted(tmp_path):
    run = _factory_run(tmp_path)
    receipt = json.loads((run / "run_receipt.json").read_text())
    receipt.pop("estate")
    (run / "run_receipt.json").write_text(json.dumps(receipt))
    with pytest.raises(ep.ProducerError, match="no estate.normalized_sha256"):
        ep.verify_factory_run(run, allow_dirty=True)


def test_a_directory_that_is_not_a_factory_run_is_refused(tmp_path):
    with pytest.raises(ep.ProducerError, match="not a source-factory run"):
        ep.verify_factory_run(tmp_path, allow_dirty=True)


def _payload(*rows):
    return {"rows": list(rows)}


ROW = {"property_key": "NC:37179:09222012", "source_row_id": "NC:37179:09222012",
       "county_fips": "37179", "parcel_apn": "09222012", "candidate_class": "QUALIFIED_CANDIDATE",
       "normalized": {"property_key": "NC:37179:09222012"}, "source_as_of": "2026-09-02",
       "opportunity_score": 93, "priority_tier": "A", "primary_recipe": "stalled_tax_foreclosure_heirship",
       "signals": [], "recommended_action": None, "global_rank": 1,
       "latitude": 35.0, "linked_lead_id": "cc86e1d9", "map_status": "MAP_READY"}


def test_global_rank_from_a_partial_universe_never_reaches_the_estate():
    rows, stats = ep.revise_rows(_payload(ROW), full_universe=False)
    assert rows[0]["global_rank"] is None and stats["global_rank_withheld"] == 1
    kept, _ = ep.revise_rows(_payload(ROW), full_universe=True)
    assert kept[0]["global_rank"] == 1


def test_rows_are_cut_to_the_revise_contract():
    rows, _ = ep.revise_rows(_payload(ROW), full_universe=False)
    assert set(rows[0]) == set(ep.REVISE_KEYS)
    assert "linked_lead_id" not in rows[0] and "map_status" not in rows[0]


def test_identity_may_come_from_normalized_but_is_never_invented():
    legacy = {**ROW, "county_fips": None, "parcel_apn": None,
              "normalized": {"property_key": ROW["property_key"], "county_fips": "37179", "apn": "09222012"}}
    rows, stats = ep.revise_rows(_payload(legacy), full_universe=False)
    assert (rows[0]["county_fips"], rows[0]["parcel_apn"]) == ("37179", "09222012")
    broken = {**ROW, "county_fips": None, "parcel_apn": None, "normalized": {}}
    rows, stats = ep.revise_rows(_payload(broken), full_universe=False)
    assert rows == [] and stats["missing_identity"] == 1


def test_facts_date_known_and_unknown_are_counted_not_filled():
    unknown = {**ROW, "property_key": "NC:37179:08300328", "source_row_id": "NC:37179:08300328",
               "parcel_apn": "08300328", "source_as_of": None}
    rows, stats = ep.revise_rows(_payload(ROW, unknown), full_universe=False)
    assert (stats["facts_as_of_known"], stats["facts_as_of_unknown"]) == (1, 1)
    assert rows[1]["source_as_of"] is None


def test_cli_refuses_with_exit_2_and_writes_nothing(tmp_path, capsys):
    out = tmp_path / "out"
    code = ep.main(["--factory-run", str(tmp_path), "--output", str(out),
                    "--recipe-engine", str(tmp_path), "--deedsonar", str(tmp_path), "--allow-dirty"])
    assert code == 2
    assert "REFUSED" in capsys.readouterr().err
    assert not (out / "PRODUCER_RECEIPT.json").exists()


def test_mecklenburg_staging_is_collected_from_the_factory_run(tmp_path):
    run = tmp_path / "run"
    (run / "staging").mkdir(parents=True)
    (run / "staging" / "nc.mecklenburg.taxdelinquent.individual_list.jsonl").write_text(
        '{"county_fips":"37119","owner_name":"DOE JANE","staging_id":"a"}\n'
        '{"county_fips":"37179","owner_name":"NOT MECK","staging_id":"b"}\n')
    (run / "staging" / "nc.mecklenburg.taxdelinquent.business_list.jsonl").write_text(
        '{"county_fips":"37119","owner_name":"ACME LLC","staging_id":"c"}\n')
    got = ep.collect_mecklenburg_staging(run, tmp_path / "out")
    assert got["rows"] == 2 and got["refused_non_37119"] == 1 and len(got["from"]) == 2
    lines = Path(got["path"]).read_text().splitlines()
    assert sorted(json.loads(l)["owner_name"] for l in lines) == ["ACME LLC", "DOE JANE"]


def test_no_mecklenburg_staging_means_no_taxpayer_file(tmp_path):
    (tmp_path / "run").mkdir()
    assert ep.collect_mecklenburg_staging(tmp_path / "run", tmp_path / "out") is None

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


# ---- full universe: one ranking run for the whole estate, new properties included ----

import csv  # noqa: E402


def _csv(path: Path, rows: list[dict]) -> Path:
    cols: list[str] = []
    for r in rows:
        cols += [c for c in r if c not in cols]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return path


def _rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


# The prior universe (the ranking input behind production's receipts): identity + old facts.
PRIOR = [
    {"property_key": "NC:37179:09222012", "source_row_id": "NC:37179:09222012", "candidate_id": "",
     "county_fips": "37179", "parcel_apn": "09222012", "years_owned": "126", "tax_balance": "1500"},
    {"property_key": "NC:37183:1721277963", "source_row_id": "NC:37183:1721277963", "candidate_id": "",
     "county_fips": "37183", "parcel_apn": "1721277963", "years_owned": "30", "tax_balance": "900"},
]
# This run's enrichment: fresh facts for the tracer (spelled with a dash, same property),
# and one property the prior universe never held (Durham REID 115923, a revise ADD).
FRESH = [
    {"county_fips": "37179", "parcel_apn": "0922-2012", "years_owned": "", "tax_balance": "1600"},
    {"county_fips": "37063", "parcel_apn": "115923", "years_owned": "", "tax_balance": "2200"},
]


def test_universe_keeps_every_prior_property_on_its_freshest_facts_and_adds_new_ones(tmp_path):
    got = ep.assemble_universe(_csv(tmp_path / "prior.csv", PRIOR), _csv(tmp_path / "fresh.csv", FRESH),
                               tmp_path / "u.csv")
    rows = _rows(tmp_path / "u.csv")
    assert (got["rows_on_fresh_facts"], got["rows_on_prior_facts"], got["rows_added_from_fresh"]) == (1, 1, 1)
    assert got["output"]["rows"] == len(rows) == 3
    tracer, carried, added = rows
    # fresh facts, prior identity: receipts/revise bind to the estate row they bound to before
    assert tracer["tax_balance"] == "1600" and tracer["years_owned"] == ""
    assert (tracer["property_key"], tracer["parcel_apn"]) == ("NC:37179:09222012", "09222012")
    # no fresher source: the prior row, unchanged
    assert carried == {**{c: "" for c in rows[0]}, **PRIOR[1]}
    # a property this run found that the prior universe lacked is ranked, not left out, under the
    # identity the export gives it and the revise ADDs it with (the projection refuses a blank key)
    assert (added["county_fips"], added["parcel_apn"], added["tax_balance"]) == ("37063", "115923", "2200")
    assert added["property_key"] == added["source_row_id"] == "NC:37063:115923"
    assert added["candidate_id"] == "" and got["added_sample"] == ["NC:37063:115923"]


def test_universe_refuses_a_property_repeated_on_either_side(tmp_path):
    prior = _csv(tmp_path / "prior.csv", PRIOR)
    dup_fresh = _csv(tmp_path / "fresh.csv", FRESH + [{**FRESH[0], "parcel_apn": "09222012"}])
    with pytest.raises(ep.ProducerError, match="fresh enrichment repeats"):
        ep.assemble_universe(prior, dup_fresh, tmp_path / "u.csv")
    dup_prior = _csv(tmp_path / "prior2.csv", PRIOR + [{**PRIOR[0], "parcel_apn": "0922-2012"}])
    with pytest.raises(ep.ProducerError, match="prior universe repeats"):
        ep.assemble_universe(dup_prior, _csv(tmp_path / "fresh2.csv", FRESH), tmp_path / "u2.csv")


def _projection(path: Path, ranks: dict, run: str = "ranking_run_20260915_abc") -> Path:
    return _csv(path, [{"county_fips": k[0], "parcel_apn": k[1], "global_rank": str(v), "ranking_run_id": run}
                       for k, v in ranks.items()])


def test_one_ranking_court_requires_list_rank_to_be_the_engine_rank_on_every_row(tmp_path):
    rows = [{"property_key": "a", "county_fips": "37179", "parcel_apn": "09222012", "global_rank": 1},
            {"property_key": "b", "county_fips": "37063", "parcel_apn": "115923", "global_rank": 2}]
    proj = _projection(tmp_path / "p.csv", {("37179", "09222012"): 1, ("37063", "115923"): 2})
    assert ep.one_ranking_court(rows, proj, 2)["list_rank_equals_engine_rank"] == 2
    off = [rows[0], {**rows[1], "global_rank": 3}]
    with pytest.raises(ep.ProducerError, match="not the engine's global rank"):
        ep.one_ranking_court(off, proj, 2)
    withheld = [rows[0], {**rows[1], "global_rank": None}]
    with pytest.raises(ep.ProducerError, match="not the engine's global rank"):
        ep.one_ranking_court(withheld, proj, 2)
    with pytest.raises(ep.ProducerError, match="one universe required"):
        ep.one_ranking_court(rows[:1], proj, 2)
    two_runs = _csv(tmp_path / "p2.csv", [{"county_fips": "37179", "parcel_apn": "09222012", "global_rank": "1",
                                           "ranking_run_id": "r1"},
                                          {"county_fips": "37063", "parcel_apn": "115923", "global_rank": "2",
                                           "ranking_run_id": "r2"}])
    with pytest.raises(ep.ProducerError, match="2 ranking runs"):
        ep.one_ranking_court(rows, two_runs, 2)
    gap = _projection(tmp_path / "p3.csv", {("37179", "09222012"): 1, ("37063", "115923"): 3})
    with pytest.raises(ep.ProducerError, match="not 1..N"):
        ep.one_ranking_court([rows[0], {**rows[1], "global_rank": 3}], gap, 2)


def _fake_stages(calls: dict, source_errors: list | None = None):
    """Stand-ins for the stage CLIs that do what the contract says each one does,
    so the wiring between them is what the test pins."""
    def step(receipt, name, cmd, cwd, outputs):
        calls[name] = cmd
        arg = lambda flag: Path(cmd[cmd.index(flag) + 1])  # noqa: E731
        if name == "1_enrich":
            _csv(outputs[0], FRESH)
            (outputs[0].parent / "LANDOS_ENRICHMENT_SUMMARY.json").write_text(
                json.dumps({"source_errors": source_errors or []}))
        elif name == "2_rank":
            rows = _rows(arg("--input"))
            rows.sort(key=lambda r: -float(r["tax_balance"]))
            for i, r in enumerate(rows, 1):
                r["global_rank"] = str(i)
            _csv(outputs[0], rows)
        elif name == "2b_project":
            ranked = _rows(arg("--ranked"))
            digest = hashlib.sha256(arg("--input").read_bytes()).hexdigest()[:12]
            _projection(outputs[0], {(r["county_fips"], r["parcel_apn"]): int(r["global_rank"]) for r in ranked},
                        run=f"ranking_run_{cmd[cmd.index('--run-date') + 1]}_{digest}")
            outputs[1].write_text("property_key\n")
        elif name == "3_export":
            master = arg("--output") / "08_MASTER" / "ALL_OPPORTUNITIES_MASTER.csv"
            master.parent.mkdir(parents=True)
            master.write_text("x\n")
        elif name == "4_payload":
            ranked = _rows(Path(calls["2_rank"][calls["2_rank"].index("--output") + 1]) / "NC_CURATIVE_ALL.csv")
            rows = [{**ROW, "property_key": r.get("property_key") or f"NC:{r['county_fips']}:{r['parcel_apn']}",
                     "source_row_id": r.get("property_key") or f"NC:{r['county_fips']}:{r['parcel_apn']}",
                     "county_fips": r["county_fips"], "parcel_apn": r["parcel_apn"],
                     "global_rank": int(r["global_rank"])} for r in ranked]
            outputs[0].write_text(json.dumps({"rows": rows, "counts": {"total": len(rows)}}))
        receipt["steps"].append({"step": name, "cmd": cmd, "exit": 0})
    return step


def test_a_universe_run_ranks_the_whole_estate_once_and_new_properties_get_a_list_rank(tmp_path, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(ep, "_step", _fake_stages(calls))
    prior = _csv(tmp_path / "prior.csv", PRIOR)
    out = tmp_path / "out"
    receipt = ep.produce(_factory_run(tmp_path), out, recipe_engine=tmp_path, deedsonar=tmp_path,
                         recipe_python="python", node="node", mecklenburg_taxpayers=None,
                         full_universe=False, allow_dirty=True, universe=prior)
    universe_csv = out / "01b_universe" / ep.UNIVERSE_INPUT_NAME
    # the ranker and the projector read the universe, not this run's slice
    assert calls["2_rank"][calls["2_rank"].index("--input") + 1] == str(universe_csv)
    assert calls["2b_project"][calls["2b_project"].index("--input") + 1] == str(universe_csv)
    rows = [r for c in receipt["revise"]["chunks"] for r in json.loads(Path(c["path"]).read_text())]
    assert len(rows) == 3 and receipt["revise"]["stats"]["global_rank_withheld"] == 0
    by_key = {r["parcel_apn"]: r["global_rank"] for r in rows}
    assert by_key == {"115923": 1, "09222012": 2, "1721277963": 3}
    digest = hashlib.sha256(universe_csv.read_bytes()).hexdigest()[:12]
    assert receipt["receipts"]["ranking_run_id"].endswith(digest)
    assert receipt["receipts"]["list_rank_equals_engine_rank"] == 3
    assert "recipe_engine_receipts_apply_v1(true, 3)" in receipt["receipts"]["t5_action"]
    assert receipt["universe"]["rows_added_from_fresh"] == 1


def test_without_a_universe_a_factory_run_still_withholds_rank_and_projects_nothing(tmp_path, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(ep, "_step", _fake_stages(calls))
    receipt = ep.produce(_factory_run(tmp_path), tmp_path / "out", recipe_engine=tmp_path, deedsonar=tmp_path,
                         recipe_python="python", node="node", mecklenburg_taxpayers=None,
                         full_universe=False, allow_dirty=True)
    assert "2b_project" not in calls and "receipts" not in receipt
    assert receipt["revise"]["stats"]["global_rank_withheld"] == receipt["revise"]["stats"]["rows"] == 2


DNS = ("37119-owner-match:RuntimeError:NC OneMap POST query failed after 4 attempts: "
       "<urlopen error [Errno 8] nodename nor servname provided, or not known>")


def test_an_enrichment_that_reports_a_source_error_is_never_produced_silently(tmp_path, monkeypatch):
    # 2026-09-15: the enrichment exited 0 after a DNS failure on the Mecklenburg owner match and
    # emitted 6,438 of 8,333 properties; nothing downstream noticed.
    calls: dict = {}
    monkeypatch.setattr(ep, "_step", _fake_stages(calls, source_errors=[DNS]))
    kw = dict(recipe_engine=tmp_path, deedsonar=tmp_path, recipe_python="python", node="node",
              mecklenburg_taxpayers=None, full_universe=False, allow_dirty=True,
              universe=_csv(tmp_path / "prior.csv", PRIOR))
    with pytest.raises(ep.DegradedEnrichment, match="1 source error"):
        ep.produce(_factory_run(tmp_path), tmp_path / "out", **kw)
    assert "2_rank" not in calls and not (tmp_path / "out" / "PRODUCER_RECEIPT.json").exists()
    (tmp_path / "b").mkdir()
    receipt = ep.produce(_factory_run(tmp_path / "b"), tmp_path / "out2", allow_degraded=True, **kw)
    assert receipt["enrichment"]["source_errors"] == [DNS] and receipt["enrichment"]["degraded_allowed"]


def test_an_enrichment_without_a_summary_cannot_be_checked_and_is_refused(tmp_path):
    (tmp_path / "e").mkdir()
    with pytest.raises(ep.ProducerError, match="cannot be checked"):
        ep.enrichment_check(tmp_path / "e", allow_degraded=True)


def test_a_missing_universe_file_is_refused_before_any_stage_runs(tmp_path, monkeypatch):
    calls: dict = {}
    monkeypatch.setattr(ep, "_step", _fake_stages(calls))
    with pytest.raises(ep.ProducerError, match="is not a file"):
        ep.produce(_factory_run(tmp_path), tmp_path / "out", recipe_engine=tmp_path, deedsonar=tmp_path,
                   recipe_python="python", node="node", mecklenburg_taxpayers=None,
                   full_universe=False, allow_dirty=True, universe=tmp_path / "absent.csv")
    assert calls == {}

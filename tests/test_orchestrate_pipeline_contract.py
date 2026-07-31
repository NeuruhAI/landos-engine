import json
import os

import pytest

from scripts import orchestrate_pipeline as orchestrator


SOURCE_SHA = "a" * 40


def _paths(tmp_path):
    return {
        "log_dir": str(tmp_path / "run-logs"),
        "lock_path": str(tmp_path / "orchestrator.lock"),
    }


def _chain(*functions):
    keys = list(orchestrator.STAGE_CONTRACTS)
    return [
        (keys[index], f"stage {index}", function, {})
        for index, function in enumerate(functions)
    ]


def _run(monkeypatch, tmp_path, chain, **kwargs):
    monkeypatch.setattr(orchestrator, "_source_sha", lambda: SOURCE_SHA)
    monkeypatch.setattr(orchestrator, "CHAIN", chain)
    return orchestrator.main(**_paths(tmp_path), **kwargs)


def _checkpoint(tmp_path, run_id):
    path = tmp_path / "run-logs" / f"orchestrator_{run_id}.receipts.json"
    return json.loads(path.read_text()), path


def test_dry_run_is_default_and_writes_atomic_stage_event_receipts(
    monkeypatch, tmp_path
):
    observed = []

    def first(dry_run):
        observed.append((dry_run, os.environ["LANDOS_RUN_ID"]))
        assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
        assert os.environ["LANGSMITH_TRACING"] == "false"
        assert os.environ["OTEL_SDK_DISABLED"] == "true"
        return {"read": 7, "written": 0}

    def second(dry_run):
        observed.append((dry_run, os.environ["LANDOS_RUN_ID"]))
        return {"read": 3, "written": 0}

    rc = _run(
        monkeypatch, tmp_path, _chain(first, second),
        run_id="dry-contract",
    )

    assert rc == 0
    assert observed == [(True, "dry-contract"), (True, "dry-contract")]
    data, path = _checkpoint(tmp_path, "dry-contract")
    assert data["complete"] is True
    assert data["source_sha"] == SOURCE_SHA
    assert len(data["config_fingerprint"]) == 64
    assert [r["body"]["event"] for r in data["receipts"]] == [
        "start", "end", "exit", "start", "end", "exit",
    ]
    assert all(r["body"]["run_id"] == "dry-contract" for r in data["receipts"])
    assert all(r["body"]["source_sha"] == SOURCE_SHA for r in data["receipts"])
    assert data["results"][0]["counts"] == {"read": 7, "written": 0}
    # A complete parse proves the final checkpoint is not a partial JSON write.
    json.loads(path.read_text())


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"dry_run": False}, "acknowledge-live"),
        (
            {"dry_run": False, "live_ack": orchestrator.LIVE_ACK},
            "allow-production-writes",
        ),
        (
            {
                "dry_run": False,
                "live_ack": orchestrator.LIVE_ACK,
                "allow_production_writes": True,
            },
            "acknowledge-production-writes",
        ),
        (
            {
                "dry_run": False,
                "live_ack": orchestrator.LIVE_ACK,
                "allow_production_writes": True,
                "production_write_ack": orchestrator.PRODUCTION_WRITE_ACK,
                "fixture_mode": True,
            },
            "fixture data is prohibited",
        ),
    ],
)
def test_live_and_production_write_gates_fail_closed(tmp_path, kwargs, message):
    with pytest.raises(orchestrator.GateError, match=message):
        orchestrator.main(**_paths(tmp_path), **kwargs)
    assert not (tmp_path / "run-logs").exists()


def test_explicit_fixture_evidence_cannot_be_reported_successful_in_live(
    monkeypatch, tmp_path
):
    def fixture_stage(dry_run):
        assert dry_run is False
        return {"read": 1, "is_fixture": True}

    rc = _run(
        monkeypatch, tmp_path, _chain(fixture_stage),
        run_id="live-fixture-refused",
        dry_run=False,
        live_ack=orchestrator.LIVE_ACK,
        allow_production_writes=True,
        production_write_ack=orchestrator.PRODUCTION_WRITE_ACK,
    )

    assert rc == 1
    data, _ = _checkpoint(tmp_path, "live-fixture-refused")
    assert data["results"][0]["status"] == "FAILED"
    assert "fixture evidence" in data["receipts"][-2]["body"]["error"]


def test_failure_is_nonzero_and_downstream_is_not_reached(monkeypatch, tmp_path):
    calls = []

    def failed(dry_run):
        calls.append("failed")
        raise RuntimeError("injected")

    def forbidden(dry_run):
        calls.append("forbidden")
        return {"read": 0}

    rc = _run(
        monkeypatch, tmp_path, _chain(failed, forbidden),
        run_id="failure-contract",
    )

    assert rc == 1
    assert calls == ["failed"]
    data, _ = _checkpoint(tmp_path, "failure-contract")
    assert data["complete"] is False
    assert data["aborted_at"] == "stage_arcgis"
    assert [row["status"] for row in data["results"]] == ["FAILED", "NOT-REACHED"]
    assert data["receipts"][-1]["body"]["event"] == "exit"
    assert data["receipts"][-1]["body"]["exit_code"] == 1


def test_resume_reuses_ok_checkpoint_and_retries_failed_stage(monkeypatch, tmp_path):
    calls = {"first": 0, "second": 0}
    fail = {"enabled": True}

    def first(dry_run):
        calls["first"] += 1
        return {"read": 4}

    def second(dry_run):
        calls["second"] += 1
        if fail["enabled"]:
            raise RuntimeError("failure injection")
        return {"read": 2}

    chain = _chain(first, second)
    assert _run(
        monkeypatch, tmp_path, chain, run_id="resume-contract",
    ) == 1
    fail["enabled"] = False
    assert _run(
        monkeypatch, tmp_path, chain, resume_run_id="resume-contract",
    ) == 0

    assert calls == {"first": 1, "second": 2}
    data, _ = _checkpoint(tmp_path, "resume-contract")
    assert data["complete"] is True
    assert [row["status"] for row in data["results"]] == ["RESUMED", "OK"]
    resumed = [
        receipt for receipt in data["receipts"]
        if receipt["body"]["status"] == "RESUMED"
    ]
    assert len(resumed) == 1
    assert resumed[0]["body"]["counts"] == {"read": 4}


def test_concurrent_run_lock_fails_without_waiting(tmp_path):
    lock_path = str(tmp_path / "orchestrator.lock")
    with orchestrator._RunLock(lock_path):
        with pytest.raises(orchestrator.ConcurrentRunError, match="holds lock"):
            with orchestrator._RunLock(lock_path):
                pass


def test_resume_refuses_source_drift(monkeypatch, tmp_path):
    chain = _chain(lambda dry_run: {"read": 1})
    assert _run(
        monkeypatch, tmp_path, chain, run_id="source-drift",
    ) == 0
    monkeypatch.setattr(orchestrator, "_source_sha", lambda: "b" * 40)
    monkeypatch.setattr(orchestrator, "CHAIN", chain)
    with pytest.raises(orchestrator.GateError, match="source SHA changed"):
        orchestrator.main(
            **_paths(tmp_path), resume_run_id="source-drift",
        )


def test_resume_refuses_tampered_receipt_chain(monkeypatch, tmp_path):
    chain = _chain(lambda dry_run: {"read": 1})
    assert _run(
        monkeypatch, tmp_path, chain, run_id="tampered",
    ) == 0
    data, path = _checkpoint(tmp_path, "tampered")
    data["receipts"][0]["body"]["counts"]["invented"] = 1
    path.write_text(json.dumps(data))

    monkeypatch.setattr(orchestrator, "_source_sha", lambda: SOURCE_SHA)
    monkeypatch.setattr(orchestrator, "CHAIN", chain)
    with pytest.raises(orchestrator.GateError, match="body hash mismatch"):
        orchestrator.main(
            **_paths(tmp_path), resume_run_id="tampered",
        )

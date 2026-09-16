"""The free-first ledger at national scale: linear, still tamper-evident, safe across writers."""
from __future__ import annotations

import time

import pytest

from app.free_first.ledger import FreeFirstLedger, LedgerCorrupt


def _fill(ledger, n, job="ffj_x"):
    for i in range(n):
        ledger.append("PAID_DECISION", job if i % 3 else f"{job}{i}", {"i": i, "pad": "x" * 200})


def test_reads_and_appends_are_incremental_not_a_full_reverification(tmp_path, monkeypatch):
    ledger = FreeFirstLedger(tmp_path / "ff.jsonl")
    _fill(ledger, 300)
    sealed = []
    import app.free_first.ledger as L
    real = L._seal
    monkeypatch.setattr(L, "_seal", lambda e: sealed.append(1) or real(e))
    for _ in range(50):
        ledger.entries("ffj_x")
        ledger.has_job("ffj_x")
    ledger.append("PAID_DECISION", "ffj_x", {"i": "one-more"})
    assert len(sealed) <= 3                      # the new entry, not 300 x 100 re-seals
    assert ledger.verify()["entries"] == 301 and len(sealed) >= 301   # verify() is a full pass


def test_a_second_writer_is_verified_and_seen(tmp_path):
    a, b = FreeFirstLedger(tmp_path / "ff.jsonl"), FreeFirstLedger(tmp_path / "ff.jsonl")
    a.append("PAID_DECISION", "j1", {"w": "a"})
    b.append("PAID_DECISION", "j1", {"w": "b"})          # b syncs a's entry under the lock, chains onto it
    a.append("PAID_DECISION", "j1", {"w": "a2"})
    assert [e["body"]["w"] for e in a.entries("j1")] == ["a", "b", "a2"] == [e["body"]["w"] for e in b.entries("j1")]
    assert a.verify()["tail_sha"] == b.verify()["tail_sha"]


def test_a_rewritten_prefix_is_caught_and_a_tampered_entry_never_verifies(tmp_path):
    ledger = FreeFirstLedger(tmp_path / "ff.jsonl")
    _fill(ledger, 5)
    lines = ledger.path.read_text().splitlines()
    lines[1] = lines[1].replace('"i":1', '"i":7')
    ledger.path.write_text("\n".join(lines) + "\n")
    with pytest.raises(LedgerCorrupt):
        ledger.verify()
    with pytest.raises(LedgerCorrupt):
        FreeFirstLedger(ledger.path)


def test_a_writer_that_died_mid_line_blocks_the_next_append(tmp_path):
    ledger = FreeFirstLedger(tmp_path / "ff.jsonl")
    _fill(ledger, 2)
    with ledger.path.open("a") as fh:
        fh.write('{"version": "landos.free_first.ledger.v1", "seq": 3')      # no newline: died mid-write
    assert len(ledger.entries()) == 2                   # a reader ignores an unfinished line
    with pytest.raises(LedgerCorrupt):
        ledger.append("PAID_DECISION", "j", {"i": 3})


def test_the_guard_opens_one_ledger_per_thread_not_one_per_paid_call(tmp_path, monkeypatch):
    """Every clearance rebuilt the ledger, so each paid probe re-verified the whole chain:
    1.09 s at 12,265 entries x ~14 probes per researched candidate."""
    import threading

    import app.free_first.guard as G
    from app.free_first.jobs import open_job

    monkeypatch.delenv(G.ENV_LEDGER, raising=False)
    monkeypatch.delenv(G.ENV_JOBS, raising=False)
    ledger = FreeFirstLedger(tmp_path / "ff.jsonl")
    job = open_job(ledger, tenant_id="court", fact="contact.phone", target={"target_key": "party:wake:1"},
                   state="NC", county_fips="37183", requested_by={"kind": "court", "id": "t14"})
    _fill(ledger, 200)
    opened = []
    real_init = FreeFirstLedger.__init__
    monkeypatch.setattr(FreeFirstLedger, "__init__",
                        lambda self, path: (opened.append(str(path)), real_init(self, path))[1])
    monkeypatch.setattr(G, "_READERS", threading.local())
    with G.bound_job(ledger, job.job_id):
        for _ in range(12):                                  # ~ the paid probes one candidate makes
            with pytest.raises(G.PaidCallBlocked):
                G.clear_paid_call("tracerfy", items=[("contact.phone", "party:wake:1")], est_cost_usd=0.02,
                                  caller="court")
    assert len(opened) == 1                                  # one reader for all twelve clearances
    assert all(e["body"].get("allow") is False for e in ledger.entries(job.job_id)
               if e["kind"] == "PAID_DECISION")              # still blocked, still recorded

    other: list = []
    thread = threading.Thread(target=lambda: other.append(G.ledger_reader(ledger.path)))
    thread.start()
    thread.join()
    assert len(opened) == 2 and other[0] is not G.ledger_reader(ledger.path)      # per thread, never shared


def test_a_thousand_jobs_stay_fast(tmp_path):
    ledger = FreeFirstLedger(tmp_path / "ff.jsonl")
    t0 = time.monotonic()
    for i in range(1000):
        ledger.append("PAID_DECISION", f"j{i}", {"i": i, "pad": "x" * 400})
        ledger.entries(f"j{i}")
    assert time.monotonic() - t0 < 20

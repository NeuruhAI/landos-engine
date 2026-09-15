"""FREE-FIRST call-site court: no paid vendor request can leave this engine without the guard.

Static half (against the source, not a claim):
  * every call site catalogued in knowledge.PAID_PROVIDERS exists and calls the guard;
  * the *_cleared helpers that hold the raw request are reachable only from their guard;
  * any module that names a paid host AND can make an HTTP request routes through the guard,
    so a NEW paid call site fails this court until it is gated.

Runtime half (no network: urlopen is a tripwire):
  * each paid call site, called the way a worker would call it, is blocked before a
    request exists -- with no job, with an open free path, and with no authorization;
  * the parcel chain degrades past a blocked ReportAll tier as it does past a dead one;
  * once the free path is exhausted and the spend is confirmed, the same call site
    goes through exactly once and the spend is settled on the ledger.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.free_first import authority as A
from app.free_first import guard as G
from app.free_first.jobs import open_job, record_attempt, request_spend_authorization
from app.free_first.knowledge import PAID_HOSTS, PAID_PROVIDERS
from app.free_first.ledger import FreeFirstLedger
from app.free_first.provenance import utc_now_iso

ROOT = Path(__file__).resolve().parents[1]
GUARD_CALLS = {"paid_call", "clear_paid_call"}
HTTP_MARKERS = ("urlopen(", "requests.", "httpx.", "http.client")
CLEARED_HELPERS = {
    "scripts/export_outreach.py": ("_tracerfy_batch_trace_cleared", "tracerfy_batch_trace"),
    "scripts/skip_trace_batchdata.py": ("_api_call_cleared", "_api_call"),
}


def _function(path: Path, qualname: str) -> ast.AST:
    tree = ast.parse(path.read_text())
    parts = qualname.split(".")
    nodes = tree.body
    found = None
    for part in parts:
        found = next((n for n in nodes if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == part), None)
        assert found is not None, f"{path.name}: {qualname} not found"
        nodes = found.body
    return found


def _calls(node: ast.AST) -> set:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return out


def test_every_catalogued_paid_call_site_calls_the_guard():
    sites = [s for p in PAID_PROVIDERS.values() for s in p.call_sites]
    assert len(sites) == 7
    for site in sites:
        rel, qualname = site.split(":")
        fn = _function(ROOT / rel, qualname)
        assert _calls(fn) & GUARD_CALLS, f"{site} sends a paid request without the FREE-FIRST guard"


def test_cleared_helpers_are_reachable_only_through_their_guard():
    for rel, (helper, wrapper) in CLEARED_HELPERS.items():
        path = ROOT / rel
        tree = ast.parse(path.read_text())
        callers = [fn.name for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and helper in _calls(fn)]
        assert callers == [wrapper], f"{helper} is called from {callers}"
        for other in [p for p in list((ROOT / "scripts").glob("*.py")) + list((ROOT / "app").rglob("*.py")) if p != path]:
            assert helper not in other.read_text(), f"{other.name} reaches around the guard via {helper}"


def test_no_module_can_reach_a_paid_host_around_the_guard():
    offenders = []
    for path in list((ROOT / "app").rglob("*.py")) + list((ROOT / "scripts").rglob("*.py")):
        if "free_first" in path.parts:
            continue
        text = path.read_text(errors="replace")
        hosts = [h for h in PAID_HOSTS if h in text]
        if not hosts or not any(m in text for m in HTTP_MARKERS):
            continue
        if not any(f"{g}(" in text for g in GUARD_CALLS):
            offenders.append(f"{path.relative_to(ROOT)} names {hosts} and makes HTTP requests with no guard")
    assert offenders == []


# ---------------------------------------------------------------------------
# runtime: blocked before any request exists
# ---------------------------------------------------------------------------

@pytest.fixture()
def tripwire(monkeypatch):
    calls = []

    def _no_network(*args, **kwargs):
        calls.append(args[0] if args else kwargs)
        raise AssertionError("a paid request left the process")
    monkeypatch.setattr("urllib.request.urlopen", _no_network)
    return calls


@pytest.fixture()
def audit(tmp_path, monkeypatch):
    for key in (G.ENV_JOB, G.ENV_JOBS):
        monkeypatch.delenv(key, raising=False)
    path = tmp_path / "free_first.jsonl"
    monkeypatch.setenv(G.ENV_LEDGER, str(path))
    A.install_authority_for_tests(None)
    return FreeFirstLedger(path)


def _unbound_reasons(ledger):
    return [(e["body"]["provider"], e["body"]["reasons"]) for e in ledger.entries(G.UNBOUND_JOB)]


def test_reportall_is_blocked_before_a_request_is_built(tripwire, audit):
    from app.normalizers.reportall_owner import ReportAllOwnerNormalizer
    ra = ReportAllOwnerNormalizer("t14", "18141", client_key="live-looking-key")
    with pytest.raises(G.PaidCallBlocked):
        ra._search({"parcel_id": "71-08-01-100-001.000-026", "rpp": 5, "page": 1}, "apn")
    assert tripwire == [] and ra.calls_made == 0
    assert _unbound_reasons(audit) == [("reportall", ["NO_FREE_FIRST_JOB"])]


def test_the_parcel_chain_degrades_past_a_blocked_paid_tier(tripwire, audit, monkeypatch):
    from app.providers import chain as C

    class NoCache:
        def __init__(self, tenant_id, conn=None):
            pass

        def lookup_by_apn(self, county_fips, apn):
            return []
    monkeypatch.setitem(C.PROVIDER_REGISTRY, "local_cache", NoCache)
    monkeypatch.delenv("REGRID_API_KEY", raising=False)
    chain = C.ParcelProviderChain("t14")
    assert chain.lookup_by_apn("18141", "71-08-01-100-001.000-026") == []
    trail = {name: (status, detail) for name, status, detail in chain.last_trail}
    assert trail["reportall"][0] == "unavailable" and "FREE-FIRST blocked" in trail["reportall"][1]
    assert chain.miss_is_retryable() and tripwire == []


def test_tracerfy_trace_and_dnc_are_blocked(tripwire, audit, monkeypatch):
    import scripts.skip_trace_tracerfy as tracerfy
    sent = []
    monkeypatch.setattr(tracerfy, "_request", lambda *a, **k: sent.append(a) or b"{}")
    target = {"lead_id": "lead-1", "owner_name": "DOE JOHN", "owner_mailing_address": "1 MAIN ST",
              "owner_mailing_city": "NILES", "owner_mailing_state": "MI", "owner_mailing_zip": "49120"}
    with pytest.raises(G.PaidCallBlocked):
        tracerfy.submit_trace([target], "live-looking-key")
    with pytest.raises(G.PaidCallBlocked):
        tracerfy.submit_dnc(iter(["+12695550101"]), "live-looking-key")
    assert sent == [] and tripwire == []
    assert [p for p, _ in _unbound_reasons(audit)] == ["tracerfy", "tracerfy_dnc"]


def test_export_outreach_batchdata_twilio_and_regrid_are_blocked(tripwire, audit, monkeypatch):
    import scripts.contact_rank as contact_rank
    import scripts.export_outreach as export_outreach
    import scripts.skip_trace_batchdata as batchdata
    from app.providers.regrid_provider import RegridProvider
    with pytest.raises(G.PaidCallBlocked):
        export_outreach.tracerfy_batch_trace([{"lead_id": "lead-1", "owner_name": "X"}], "k")
    with pytest.raises(G.PaidCallBlocked):
        batchdata._api_call({"lead_id": "lead-1", "owner_name": "DOE JOHN", "situs_address": "1 MAIN"}, "k")
    with pytest.raises(G.PaidCallBlocked):
        contact_rank.twilio_lookup("+12695550101", "sid", "token")
    monkeypatch.setenv("REGRID_API_KEY", "armed")
    with pytest.raises(G.PaidCallBlocked):
        RegridProvider("t14").lookup_by_apn("18141", "X")
    assert tripwire == []
    assert [p for p, _ in _unbound_reasons(audit)] == ["tracerfy", "batchdata", "twilio_lookup", "regrid"]


def test_a_bound_worker_with_an_open_free_path_is_still_blocked(tripwire, audit):
    from app.normalizers.reportall_owner import ReportAllOwnerNormalizer
    job = open_job(audit, tenant_id="t14", fact="parcel.owner_identity",
                   target={"target_key": "parcel:18141:APN-1", "apn": "APN-1"}, state="IN", county_fips="18141",
                   requested_by={"kind": "court"})
    with G.bound_job(audit, job.job_id), pytest.raises(G.PaidCallBlocked) as blocked:
        ReportAllOwnerNormalizer("t14", "18141", client_key="k")._search({"parcel_id": "APN-1"}, "apn")
    assert any("FREE_PATH_OPEN:parcel.local_cache" in r for r in blocked.value.reasons)
    assert any("SPEND_UNAUTHORIZED:NONE_RECORDED:FAIL_CLOSED" in r for r in blocked.value.reasons)
    assert tripwire == []


def test_the_same_call_site_goes_through_once_the_rule_is_satisfied(audit, monkeypatch):
    """Exhausted + confirmed: exactly one real request, and the spend is on the ledger."""
    from test_free_first_policy import KeyedCourtAuthority
    from app.normalizers.reportall_owner import ReportAllOwnerNormalizer
    sent = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"status": "OK", "count": 0, "results": []}).encode()

    def fake_urlopen(req, timeout=None):
        sent.append(req.full_url)
        return Resp()
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    A.install_authority_for_tests(KeyedCourtAuthority())
    try:
        job = open_job(audit, tenant_id="t14", fact="parcel.owner_identity",
                       target={"target_key": "parcel:18141:APN-2", "apn": "APN-2"}, state="IN", county_fips="18141",
                       requested_by={"kind": "court"})
        now = utc_now_iso()
        record_attempt(audit, job.job_id, step_id="parcel.local_cache", outcome="NO_HIT", started_at=now,
                       finished_at=now, worker={"worker_id": "court"},
                       evidence_not_found={"query": "owner_leads apn=APN-2", "result_count": 0})
        request_spend_authorization(audit, job.job_id, providers=["reportall"], max_total_usd=0.05,
                                    max_per_call_usd=0.05, requested_by="founder:court")
        with G.bound_job(audit, job.job_id):
            data = ReportAllOwnerNormalizer("t14", "18141", client_key="k")._search({"parcel_id": "APN-2"}, "apn")
        assert data["status"] == "OK" and len(sent) == 1
        state = audit.job_state(job.job_id)
        assert state.paid_decisions[-1]["allow"] is True
        assert state.settlements[-1]["outcome"] == "COMPLETED" and state.settlements[-1]["cost_usd"] == 0.05
    finally:
        A.install_authority_for_tests(None)

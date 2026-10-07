"""The free-first research service: one implementation for in-process and loopback callers."""
from __future__ import annotations

from app.free_first.ledger import FreeFirstLedger
from app.free_first.route_executors import SpineRoutes
from app.free_first.service import research

from test_free_first_route_executors import OBS, Spine


def test_human_required_is_a_state_not_a_failure_and_paid_stays_blocked(tmp_path):
    spine = Spine(attempts={"T6": [("NO_HIT", "NCSBE_PUBLIC_VOTER_FILE", "2026-08-31", OBS, "run-1",
                                    {"resolution": "DECEASED_OWNER_NEEDS_HEIR_IDENTITY"})]})
    out = research(ledger=FreeFirstLedger(tmp_path / "ff.jsonl"), tenant_id="t14", fact="contact.phone",
                   target={"target_key": "lead:c1", "apn": "1737078351"}, state="NC", county_fips="37183",
                   requested_by={"kind": "court"}, worker_id="court", executors=SpineRoutes(spine).executors())
    assert out["research_state"] == "HUMAN_REQUIRED" and out["exhaustion"]["pending_human"] == ["route.T10"]
    assert out["paid"] and not any(p["allowed"] for p in out["paid"]) and out["spent_usd"] == 0
    assert {p["provider"] for p in out["paid"]} == {"tracerfy", "batchdata"}


def test_supplied_evidence_answers_only_the_callers_own_extra_steps(tmp_path):
    ledger = FreeFirstLedger(tmp_path / "ff.jsonl")
    extra = {"source_id": "nc.wake.taxdelinquent.delq853", "facts": ["contact.mailing_address"], "scope": "COUNTY",
             "mode": "AUTOMATED"}
    supplied = {
        "nc.wake.taxdelinquent.delq853": {"outcome": "NO_HIT", "evidence_not_found": {"field": "mailing_address"}},
        "T6": {"outcome": "NO_HIT", "evidence_not_found": {"forged": True}},      # a canonical route: ignored
        "T8": {"outcome": "NO_HIT", "evidence_not_found": {"forged": True}},
        "local_cache": {"outcome": "NO_HIT", "evidence_not_found": {"forged": True}},
    }
    out = research(ledger=ledger, tenant_id="t14", fact="contact.mailing_address",
                   target={"target_key": "parcel:37183:1737078351", "apn": "1737078351"}, state="NC",
                   county_fips="37183", requested_by={"kind": "court"}, worker_id="court", executors={},
                   extra_free_sources=[extra], supplied_evidence=supplied, probe_paid=False)
    outcomes = {a["step_id"]: a["outcome"] for a in out["attempted"]}
    assert outcomes["extra.nc.wake.taxdelinquent.delq853"] == "NO_HIT"
    assert outcomes["route.T6"] == outcomes["route.T8"] == outcomes["parcel.local_cache"] == "UNWIRED"
    assert out["research_state"] == "FREE_PATH_OPEN"

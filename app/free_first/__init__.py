"""FREE-FIRST: the machine policy that makes paid enrichment the last resort.

Founder direction (docs/FREE_ROUTES_REGISTER.md, verbatim): "We want to exhaust the
ability to do stuff for free before we pay." FR-2 made it a rule; this package makes
it a gate every paid call in the engine must pass:

    FREE 1 -> FREE 2 -> FREE 3 -> ... -> documented exhaustion
          -> paid-exception gate -> bounded paid call  |  human escalation

  knowledge   which free sources answer which fact, per jurisdiction (read from the
              engine's registers); the paid-provider catalog and their call sites
  ledger      append-only hash-chained history per job; survives retries, restarts,
              worker handoffs and autonomous continuation
  policy      FR-2 exhaustion and the paid decision (pure)
  authority   Governance is the spend authority (R4, founder-interactive); default
              FailClosed; recorded yeses are re-confirmed against Governance receipts
  jobs        open / attempt / human outcome / authorization / close
  runner      executes the free ladder; has no path to a paid provider
  guard       paid_call(): the door every paid vendor HTTP call walks through
  provenance  observed_at / retrieved_at / source effective date / freshness / confidence
"""
from .guard import PaidCallBlocked, bound_job, bound_jobs, binding_env, clear_paid_call, paid_call
from .jobs import (AttemptRefused, close_job, open_job, record_attempt, record_human_outcome,
                   request_spend_authorization)
from .knowledge import FACTS, PAID_HOSTS, PAID_PROVIDERS, plan_ladder
from .ledger import FreeFirstLedger, LedgerCorrupt, job_id_for
from .policy import decide_paid, exhaustion
from .runner import AttemptResult, parcel_tier_executor, run_free_ladder

__all__ = [
    "AttemptRefused", "AttemptResult", "FACTS", "FreeFirstLedger", "LedgerCorrupt", "PAID_HOSTS",
    "PAID_PROVIDERS", "PaidCallBlocked", "binding_env", "bound_job", "bound_jobs", "clear_paid_call",
    "close_job", "decide_paid", "exhaustion", "job_id_for", "open_job", "paid_call", "parcel_tier_executor",
    "plan_ladder", "record_attempt", "record_human_outcome", "request_spend_authorization", "run_free_ladder",
]

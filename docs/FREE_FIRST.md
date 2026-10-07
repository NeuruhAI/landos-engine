# FREE-FIRST — paid enrichment is machine-blocked until the free path is exhausted

Founder direction (verbatim, `docs/FREE_ROUTES_REGISTER.md`): *"We want to exhaust the
ability to do stuff for free before we pay."* FR-2 wrote it as a rule; `app/free_first/`
makes it a gate that every paid vendor call in this engine must pass.

```
FREE 1 → FREE 2 → FREE 3 → … → documented exhaustion (FR-2)
      → paid-exception gate → bounded paid call   |   human escalation
```

## The rule

A paid provider call is allowed only when **all** of these hold (every failing one is
recorded in the decision, never just the first):

| check | reason code when it fails |
|---|---|
| the call is bound to a free-first job for each target it pays for | `NO_FREE_FIRST_JOB`, `TARGET_NOT_BOUND:<target>` |
| the provider is catalogued and can establish the job's fact | `UNKNOWN_PAID_PROVIDER`, `PROVIDER_DOES_NOT_ESTABLISH_FACT` |
| no free source already found it | `FACT_RESOLVED_FREE:<steps>` |
| every applicable AUTOMATED free step has a fresh terminal receipt (FR-2) | `FREE_PATH_OPEN:<steps>` |
| every applicable MANUAL (H-12) step has a human outcome, or is waived by name with a reason inside the authorization | `HUMAN_STEP_PENDING:<steps>` |
| a spend authorization exists **and its authority confirms it at call time** | `SPEND_UNAUTHORIZED:<why>` |
| it names this provider, is unexpired, and has budget (unpriced = per-call cap) | `PROVIDER_NOT_AUTHORIZED`, `AUTHORIZATION_EXPIRED`, `UNPRICED_WITHOUT_PER_CALL_CAP`, `OVER_PER_CALL_CAP`, `OVER_BUDGET` |

Terminal outcomes: `HIT`, `NO_HIT` (must document what was searched), `NOT_APPLICABLE`,
`BLOCKED`, or `ERROR` once 3 errors (or one permanent error) are on record. `UNWIRED`
(no executor in this runtime) and `HALTED` never exhaust a step — a runtime that cannot
read a free source must not pay for what it might hold. A terminal attempt older than the
step's TTL stays on record but no longer counts: the source must be re-tried.

## Where it is enforced

`guard.paid_call()` wraps every billable request (enforced by
`tests/test_free_first_call_sites.py`, which also fails on any new module that names a
paid host and makes HTTP requests without the guard):

| provider | call site |
|---|---|
| ReportAll | `app/normalizers/reportall_owner.py:ReportAllOwnerNormalizer._search` (chain tier, `ingest_owner_leads`, `ingest_foreclosure`, `ingest_discovery`) |
| Regrid | `app/providers/regrid_provider.py:RegridProvider._gate` (dormant stub; gated when armed) |
| Tracerfy trace | `scripts/skip_trace_tracerfy.py:submit_trace`, `scripts/export_outreach.py:tracerfy_batch_trace` |
| Tracerfy DNC | `scripts/skip_trace_tracerfy.py:submit_dnc` (also `scripts/scrub_crm_phone_unknowns.py`) |
| BatchData | `scripts/skip_trace_batchdata.py:_api_call` |
| Twilio Lookup | `scripts/contact_rank.py:twilio_lookup` |

A blocked call raises `PaidCallBlocked` (a `RuntimeError`): the parcel chain degrades past
a blocked ReportAll tier exactly as it does past a dead one; scripts stop loudly. The
decision is appended to the ledger **before** any request is built.

## Who may authorize spend

Governance. `/v2/authorize` classifies `action_class: "spend"` as R4 and any `spend_usd > 0`
as R4 — founder-interactive; a FounderGrant can never cover it. `authority.py` asks with
`gov.action.facts.v1` facts bound to the job, provider set, budget and a nonce, accepts only
an allow bound to those facts with a decision receipt, and **re-confirms** the recorded
authorization against Governance's receipts log at every call. The default authority is
`FAIL_CLOSED` (`LANDOS_FREE_FIRST_AUTHORITY` unset): nothing can clear.

## Ledger

`FreeFirstLedger` — append-only, hash-chained JSONL, flock-serialized, fsynced. Entry kinds:
`JOB_OPENED`, `LADDER_EXTENDED`, `ATTEMPT`, `HUMAN_OUTCOME`, `SPEND_AUTHORIZED`,
`PAID_DECISION`, `PAID_SETTLED`, `JOB_CLOSED`. Job identity is tenant + fact + target +
county (never a run id), so retries, restarts, handoffs (`binding_env()` carries the binding
into a worker subprocess) and autonomous continuation all land on the same history. An
edited or truncated ledger refuses to open.

Every attempt persists: source, timestamps, outcome, jurisdiction, target, evidence found
(with `observed_at`, `retrieved_at`, `source_effective_date`, freshness per
`app/freshness.py`, confidence), evidence not found, exhaustion after the attempt, worker,
parent receipt. Paid decisions persist exhaustion, authorization, reasons and cost; paid
settlements persist cost and resulting evidence. A row-bookkeeping timestamp
(`updated_at`, `ingested_at`, …) is refused as a fact's date.

## Knowledge

`knowledge.plan_ladder(fact, state, county)` reads the engine's own registers
(`providers/config.py` county tiers, `free_routes/registry.py` T1–T10 / I2–I3,
`resolution/source_procedure.py`). Order: what we already hold → the county's own
procedure → the state's → national / generic; automated before manual inside a scope.
A caller may add free sources (a national registry row, a new county adapter); additions
only lengthen the ladder, and a source learned later re-opens every job it applies to.

```
python -m app.free_first plan --fact contact.phone --state NC --county 37183
python -m app.free_first verify --ledger <path>
python -m app.free_first job --ledger <path> --job <ffj_…>
```

# Phase 2 proposal — lead-stage event audit

## Ruling

`public.pipeline_stage_audit` is a **pipeline stage-definition audit**. Its trigger
is on `public.pipeline_stages`; it does not fire when a lead moves on the kanban.
It must not be renamed, reinterpreted, or repurposed as lead-stage history.

No CRM migration is included in ORANGE. This document is a Phase 2 proposal for
the CRM owner to evaluate and implement after WHITE verification.

## Preferred design

Extend the existing append-only `public.lead_activity` event spine when it can
carry the fields below without weakening current RLS or event vocabulary. Use a
dedicated `public.lead_stage_events` table only if `lead_activity` cannot provide
an immutable, indexed, tenant-scoped transition ledger.

Required event fields:

| Field | Contract |
|---|---|
| `event_id` | UUID primary key generated once |
| `tenant_id` | Non-null; matches the lead tenant |
| `lead_id` | Non-null acquisition lead identifier |
| `pipeline_id` | Pipeline active at transition time |
| `before_stage_id` / `before_status` | Explicit prior state; nullable only for creation |
| `after_stage_id` / `after_status` | Explicit new state |
| `actor_type` / `actor_role` | Human, system, import, rollback; capability-scoped |
| `actor_id` | Auth UUID in CRM; export only as SHA-256 fingerprint |
| `request_id` | API/request correlation identifier |
| `run_id` | LandOS/orchestration correlation identifier when applicable |
| `reason` | Controlled reason code, not free-form PII |
| `metadata` | Bounded JSONB for source receipt hashes and proposal IDs |
| `occurred_at` | Server timestamp |
| `previous_event_sha256` | Optional prior lead-event hash |
| `content_sha256` | Canonical immutable event-body hash |

## Write and authority contract

1. A lead mutation and its transition event commit in the same transaction.
2. The database derives `tenant_id`, before-state, actor identity, and timestamp;
   clients may not assert them.
3. Direct `UPDATE`/`DELETE` on event rows is revoked. Corrections append a new
   compensating event with `reason=correction`.
4. Tenant RLS uses the same membership predicate as acquisition leads.
5. Service/system writers identify `request_id` and `run_id`; missing correlation
   is rejected for automated transitions.
6. Stage-definition changes continue to use `pipeline_stage_audit`.
7. Lead-stage moves use this proposed event surface only.

## Candidate implementations

### A. Extend `lead_activity` (preferred)

Add a controlled activity type such as `stage_transition` with a validated JSONB
payload containing before/after, pipeline, request, run, and receipt fields.

Advantages: existing timeline, RLS, append-only precedent, and operator UI path.
Risk: generic JSONB can drift unless a constraint/trigger validates the complete
transition payload.

### B. Dedicated `lead_stage_events`

Use typed columns and expose a read-only projection into `lead_activity` or the
timeline API.

Advantages: stronger constraints, simpler forensics, indexed transition queries.
Risk: a second audit spine can diverge from the operator timeline unless the
transaction and projection contract are explicit.

## Phase 2 verification

Run in an isolated CRM test database with synthetic tenant and lead IDs:

1. Inspect deployed triggers for `pipeline_stage_audit`; confirm they target only
   `pipeline_stages`.
2. Create a synthetic lead and transition it through the supported application
   path. Assert one transition event with exact before/after states.
3. Attempt a direct lead status update. It must either emit the same event or be
   rejected; silent bypass fails.
4. Repeat the same idempotency/request ID. Assert no duplicate event.
5. Run two concurrent transitions. Assert serialization or one explicit conflict,
   never two incompatible successors.
6. Attempt cross-tenant read/write and actor spoofing. All must be denied.
7. Attempt event update/delete. Both must be denied.
8. Apply a correction. Assert an appended compensating event and intact prior hash.
9. Verify request ID, run ID, actor, before/after, and receipt hash can answer:
   “who moved this lead, from where to where, through which request and run?”
10. Confirm a lead drag adds no row to `pipeline_stage_audit`.

## Rollback plan

Disable the new transition trigger/application writer first, leaving the
append-only rows readable. Revert the application read projection. Drop new
policies/functions/table only in a separately approved rollback migration after
exporting row counts and hashes. Never delete transition history as the first
rollback action.

## Acceptance criteria

- Every supported lead-stage mutation emits exactly one tenant-scoped event.
- No mutation path silently bypasses the ledger.
- Events are append-only and chain-verifiable.
- Actor, before/after, request ID, and run ID are present.
- Existing `pipeline_stage_audit` semantics remain unchanged.
- Migration up/down, RLS, concurrency, idempotency, and tamper tests pass.

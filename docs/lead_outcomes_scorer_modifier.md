# Job 8 — lead_outcomes feedback loop

**Status:** foundation SHIPPED (migration 018 + `scripts/read_lead_outcomes.py`); the
scorer-modifier integration is SPEC (deferred — it is a third book-wide rescore and
deserves the same care as the Job-6 distress widen).

## What's shipped
- `landos.lead_outcomes` (mig 018) — durable feedback-fact store, idempotent on
  `(source, source_ref)`. disposition ∈ {interested, callback, wrong_number, dnc, dead}.
- `scripts/read_lead_outcomes.py` — nightly reader; lifts `public.lead_activity.outcome`
  (read-only) → `lead_outcomes` via `OUTCOME_MAP`. First run ingested 8 facts
  (interested 6, callback 1, wrong_number 1). Unmapped outcomes are counted + skipped
  (honest-null), never coerced.
- **Nightly schedule:** wire `python -m scripts.read_lead_outcomes` into the existing
  cron alongside the backup/emergence jobs (idempotent — safe to re-run).

## Scorer-modifier spec (deferred build)
The scorer (`scripts/score_composite_batch.py`) reads the LATEST disposition per lead as
a **deterministic** multiplier on the composite, applied AFTER the axis floor (never
inside an axis — dispositions are reachability facts, not distress/owner/arbitrage
evidence). Proposed table (deterministic, auditable, recorded in `inputs_snapshot`):

| latest disposition | modifier | rationale |
|---|---|---|
| interested   | ×1.15 (cap 100) | owner engaged — strongest live signal |
| callback     | ×1.05 | reached, deferred |
| (none)       | ×1.00 | no contact yet — neutral |
| wrong_number | ×0.90 | bad contact data (reachability, not the parcel) |
| dnc          | ×0.85 + suppress dialing (already enforced at the sync boundary) |
| dead         | ×0.50 | owner deceased / hard no |

**Load-bearing details for the build:**
1. **lead_id namespace:** `lead_outcomes.lead_id` comes from `public.lead_activity` =
   `acquisition_leads.lead_id`, but the scorer keys on `landos.owner_leads`. Join
   acq→owner_leads by `(parcel_apn, county_fips)` to apply the modifier (same seam the
   resolver + sync use).
2. **Determinism:** latest-by-`noted_at`; ties broken by `id`. No randomness.
3. **Provenance:** record `{disposition, modifier, source_ref}` in
   `inputs_snapshot.outcome_modifier` so the composite stays fully auditable.
4. **Idempotent + reversible:** same `batch_tag`; re-run yields identical scores given
   identical outcomes.

Building it = edit `score_composite_batch` (apply modifier post-floor) → full rescore
→ `sync_to_crm` → verify. One well-scoped follow-up, mirroring the Job-6 motion.

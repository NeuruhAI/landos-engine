# Job 8 — lead_outcomes feedback loop

**Status:** SHIPPED 2026-07-09 (foundation mig 018 + `scripts/read_lead_outcomes.py`;
scorer-modifier in `scripts/score_composite_batch.py` + CRM projection in
`scripts/sync_to_crm.py`). First live rescore moved **2 leads** (interested `03-4810-…`
17.5→20.12; wrong_number `03-6790-…` 21.25→19.12) — inside the ≤10 sanity band, join
verified against the authoritative `source_lead_id` seam (0 divergence).

> ⚠ **OPEN — flagged, awaiting operator ruling (do NOT silently fix): Caller Desk vocab
> mismatch.** See the "Caller Desk ingest gap" section at the bottom. The reader currently
> parses the LOSSY `lead_activity.outcome` funnel, not the precise
> `lead_activity.meta->>'disposition'`. Once ccT1's Caller Desk popup drives volume, this
> drops `dnc_request` + `no_answer` and mis-reads `not_interested` as `interested`.

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
→ `sync_to_crm` → verify. One well-scoped follow-up, mirroring the Job-6 motion. **DONE.**

---

## ⚠ Caller Desk ingest gap (flagged 2026-07-09 — awaiting ruling)

`read_lead_outcomes.py` reads `public.lead_activity.outcome`. There are **two writers**
into that column with **different vocabularies**, and the Caller Desk (the path ccT1's
tonight popup drives) feeds the reader a LOSSY signal:

| writer | what it puts in `lead_activity.outcome` | precise value |
|---|---|---|
| legacy calls route (`CALL_OUTCOMES`) | connected / no_answer / voicemail / bad_number / dnc_request | (same) |
| **Caller Desk** (mig 054 trigger `fn_calllog_audit`) | funnel-collapsed: `no_answer`, `dnc_request`, else→**`connected`** | in `meta->>'disposition'` |

The Caller Desk's real 6-value disposition (`contacted / callback / no_answer /
not_interested / interested_promote / dnc_request`) is written to `call_logs.outcome` and
preserved in `lead_activity.meta.disposition`, but the trigger collapses it to a 3-value
funnel in `lead_activity.outcome`. Consequences for the reader as written:

1. **`dnc_request` DROPPED.** `OUTCOME_MAP` has `dnc`/`do_not_call`, not `dnc_request` →
   honest-null skip. The most compliance-critical signal (TCPA DNC) never reaches the loop.
2. **`no_answer` DROPPED.** Defensible (not-yet-reached ≠ a verdict), but currently silent
   rather than an explicit ×1.00 neutral.
3. **Inversion:** `not_interested` (owner DECLINED) → funnel `connected` → `OUTCOME_MAP`
   maps `connected`→`interested` → **×1.15 RAISE**. A rejection reads as engagement.
   `callback`/`contacted` also collapse to `connected` and over-credit.

The current 8 facts parsed cleanly only because they came from the legacy path
(connected/voicemail/bad_number), not the Caller Desk.

**Recommended fix (NOT applied — flag-don't-adapt):** teach the reader to prefer the
precise disposition when present — `coalesce(meta->>'disposition', outcome)` gated on
`meta->>'source'='caller_desk'` — with a Caller-Desk map:
`interested_promote→interested`, `contacted→callback`, `callback→callback`,
`not_interested→dead`, `dnc_request→dnc`, `no_answer→(skip / neutral)`. This captures DNC,
fixes the `not_interested` inversion, and keeps the legacy path working via the fallback.

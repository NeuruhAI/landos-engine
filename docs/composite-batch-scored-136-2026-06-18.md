# RESOLVED — composite BATCH path built + 136 production leads scored

**Date:** 2026-06-18 (same day, after the FINDING) · **Engine:** landos-engine → lvdi · **Lane:** engine (terminal)
**Resolves the upstream half of** `docs/FINDING-crm-score-breakdown-blocked-2026-06-18.md`.
The CRM projection (`acquisition_leads.score_*`) is still **NOT** done — deliberately
parked (see "Stop" below). All writes here are `landos.*` only.

## UPDATE 2026-06-18 — default v3: `distress.environmental` key added
The 103 grounded-but-unmodeled leads (environmental_part201 93 + brownfield 10) were floor-0
on distress. Added a second-tier distress signal at weight **0.15**:
- `app/scoring.py` — `DEFAULT_WEIGHTS.distress += environmental: 0.15` (+ LABELS entry).
- `app/composite_inputs.py` — `environmental_part201` **and** `brownfield` → `environmental`,
  plus a cross-check: a token counts as *modeled* only if the active weights actually contain
  its key (so it's unmodeled under v2, modeled under v3 — never silently grounded-but-0).
- `landos.scoring_weights` — cloned active **default v2 → v3** with the key. The
  `idx_one_active_weights_per_profile` index forces one active row per profile, so the swap
  **deactivates v2 first, then inserts v3 active**. `benton_harbor_edge` untouched.

Intra-axis math is **ABSOLUTE** (`distress_component = Σ present weights × 100`, clamped) — so
adding the key does **not** re-weight the financial leads. Re-ran the batch under v3, self-gated
(5 gates), then verified read-back after an idempotency re-run:

| check | result |
|---|---|
| distress coverage (grounded/unmodeled/absent) | **136 / 0 / 0** (was 33 / 103 / 0) |
| distress component distribution | `{15: 103, 20: 11, 25: 21, 45: 1}` — 33 financial UNCHANGED; 103 env/brownfield → **15** |
| composite score min/median/max | **6.0 / 9.75 / 21.25** (was 0 / 3.75 / 21.25); each env lead **+6.0** |
| weights_version stamped | **3** · 136 rows (idempotent) · CRM `score_*` still NULL |

**Remaining gap (revised):** distress is no longer a gap. Left: **arbitrage 0/136** (load
`landos.comps`) and **owner age/DOB** (`age_band` unscorable). Reversibility: set v3
`active=false` + v2 `active=true`, delete the v3 weights row, re-run the batch.

## What was missing → what was built
`app.scoring.score_parcel` (the composite) was API-only via `/evaluate`; no path
assembled distress/owner/arbitrage facts from tables and scored a lead set. Built:

- **`app/composite_inputs.py`** — pure input-fact **assembler**. Turns a parcel's raw
  engine facts into `score_parcel`'s input shape **plus a coverage record**. Enforces the
  HARD RULE: a MISSING (or grounded-but-unmodeled) axis yields a **NULL** component, never
  a fabricated 0. Three per-axis states: `grounded` / `grounded_unmodeled` / `absent`.
- **`scripts/score_composite_batch.py`** — the **batch runner**. Loads the tenant's active
  weights (same as `_active_weights`), assembles + scores each of the 136, writes
  `landos.scores` (component NULL where absent/unmodeled), `event_ledger`, `audit_log`.
  Dry-run by default; `--apply` to write. Idempotent + reversible via
  `inputs_snapshot.batch_tag = "composite_136_v1"`.
- **`scripts/audit_composite_coverage.py`** — read-only coverage report (schema, the 136,
  per-axis grounding, weights). This is the standing "Bryan's data target" report.
- **`tests/test_composite_inputs.py`** — 12 tests pinning the MISSING-is-NULL contract.

## Correction to the FINDING (premise #2 was understated)
Distress is **not** 1/136. The FINDING counted only `discovery_signals` (1 match). But
`owner_leads.signal_overlap` carries a grounded `distress_stack` for **136/136**:

| signal_overlap token | leads | composite slot |
|---|---|---|
| `environmental_part201` | 93 | none → grounded_unmodeled |
| `tax_delinquent` | 21 | `tax_delinquent` ✓ |
| `foreclosure` | 12 | `pre_foreclosure` ✓ (family map) |
| `brownfield` | 10 | none → grounded_unmodeled |

So distress is **grounded for all 136**; 33 map to a composite key, 103 carry a real signal
the composite has no slot for. (Part 201 is already first-class to the edge model and
`lead_queue.correlation_boost` — the composite is the only place it falls through.)

## Run result (weights_version=2, verified read-back)
| Axis | grounded | grounded_unmodeled | absent | component stored (non-NULL) |
|---|---|---|---|---|
| owner | 136 | 0 | 0 | **136/136** |
| distress | 33 | 103 | 0 | **33/136** |
| arbitrage | 0 | 0 | 136 | **0/136** |

- composite `score` (conservative floor): min 0 · median 3.75 · max 21.25 · **>0 for 109/136**
- `confidence` (axis-weighted grounded fraction): min 0.03 · median 0.09 · max 0.55
- The FINDING's repro join (`scores → identity_nodes → owner_leads`) now resolves **136** (was 0).
- `acquisition_leads.score_*` confirmed still **NULL** (CRM untouched).

**Score semantics:** the stored `score` is a **floor** — `score_parcel` over only
modeled-grounded facts; unknown axes contribute 0. Weights are **not** renormalized over
present axes (that would let one grounded axis masquerade as a full score). Partiality is
carried in `inputs_snapshot.coverage` + `confidence`, never hidden in the number. Flip to
renormalized only as a deliberate, separate decision.

## Remaining input gap (Bryan's data target)
- **arbitrage — 136/136:** `landos.comps` is empty; no listing-price source. Load comps
  (the arbitrage path lights up automatically — proven by test). 
- **distress — 103/136:** `environmental_part201` / `brownfield` need a composite distress
  key (add to `scoring_weights`) or an explicit remap; otherwise they stay floor-0.
- **owner — 136/136:** no age/DOB source (`age_band` unscorable, caps owner observability at
  ~37.5%); succession known only where skip-traced (2/136 traced).

## Re-run / rollback
- Dry-run: `python -m scripts.score_composite_batch`
- Apply:   `python -m scripts.score_composite_batch --apply`  (idempotent)
- Audit:   `python -m scripts.audit_composite_coverage`
- Rollback: `delete from landos.scores where inputs_snapshot->>'batch_tag'='composite_136_v1';`

## Stop (not done, by design)
No write to `public.acquisition_leads.score_*`. The absentee-vs-composite headline decision
is still open, and at current coverage (arbitrage 0, distress 103 floor-0) the composite is a
low-confidence floor — projecting it as the CRM headline would mislead. Projection is a
separate step through the single writer (`sync_to_crm.py`) once coverage is real enough.

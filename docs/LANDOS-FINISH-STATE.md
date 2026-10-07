# LANDOS — FINISH STATE (2026-07-15)

Cold-legible record of the four-job finish. Doctrine held throughout: **$0 acts shipped (GREEN);
spend / deploy / prod-DB-RLS / secrets print-stopped (RED).** Work done in isolated worktrees off
`origin/main`, one branch per concern, nothing left stranded on this Mac. Live production repos with
active parallel agents — no foreign branch or active worktree was disturbed.

| Job | State | Branch @ commit | On origin | Tested |
|-----|-------|-----------------|-----------|--------|
| 1 — close open endpoint | ✅ SHIPPED | landos-engine `fix/land-dd-fail-closed-auth` @ `4b9b62d` | yes | 8/8 unit + live guard proof |
| 2 — national skip-trace | ✅ SHIPPED (runner existed) + proof | landos-engine `feat/national-skiptrace-plan` @ `ec9c85c` | live dry-run, $0 |
| 3 — white-label search | 🟡 FOUNDATION SHIPPED, apply PRINT-STOP | apg-land-crm `feat/white-label-modular-search` @ `e1bde5e` | columns/types verified vs live spine |
| 4 — verify the rest | ✅ VERIFIED (3 claimed-fails were stale-branch artifacts) | n/a (read-only) | against `origin/main` |

---

## JOB 1 — 🔴 → ✅ Closed the open valuation endpoint (security)

**Hole:** `auth_land_dd` had `if not required: return True` — with `NVE_LAND_TOKEN` unset it served
**6 routes UNAUTHENTICATED**: `/land-dd`, `/lead-receipt`, `/v1/insight`, `/v1/valuation`,
`/compliance-cert`, `/compliance-cert/batch`.

**Grep of every guard (the requested list):**
- `auth_land_dd` — **was open-if-unset → FIXED.**
- `auth` (X-LANDOS-SECRET) — already strict.
- `auth_hunt`, `auth_composer` — already fail-closed. No other route had the pattern.

**Fix:** new `app/auth.py` — a pure, DB-free `token_guard_ok()` + `bearer_of()`; all three token guards
funnel through it, so fail-closed lives in one tested place. Unset token = LOCKED (401), never open.

**Tested:** `tests/test_auth_guard.py` 8/8 (unset→deny, wrong→deny, correct→allow, master-secret path,
empty-vs-empty rejected). Verified live on the real guard functions: `UNSET→401, WRONG→401, secret→200`.

**Operator action (print-stop — secret, not committed):** set on the `landos-engine` service (Render env
+ local `.env`):
```
NVE_LAND_TOKEN=<the 40-byte token printed in-session; regenerate with: python -c "import secrets;print(secrets.token_urlsafe(40))">
```
Until set, the Bearer path 401s and only `X-LANDOS-SECRET` authenticates — which is the correct
fail-closed posture.

---

## JOB 2 — 🔴 → ✅ National skip-trace (runner existed; added proof + planner)

**Finding:** a national batch runner **already exists** — `scripts/skip_trace_tracerfy.py`
(`run_batch`/`trace_pool`): national by default, `--county`/`--provenance`/`--limit` scoping, `--confirm`
print-stop, `HARD_CAP_USD=10`. Result rail: Tracerfy → `import_resolved` →
`landos.owner_leads.skip_trace_meta` → DNC scrub (**queue-2353 canonical-phone guard, merged to main
`7e94195`**) → `sync_to_crm` (sole writer; unknown DNC fail-closed; digits never stored). Reimplementing
it would have created a forbidden parallel writer.

**Shipped:** `scripts/skip_trace_national_plan.py` — its `$0` planning companion. Reuses the runner's
cohort predicate (`select_targets`/`TARGET_QUERY`) and price (`_est`) verbatim; prints the national
cohort, per-county breakdown, projected spend, and the exact chunked operator fire commands. Submits
nothing, spends nothing, writes nothing (read-only txn).

**Live dry-run proof (real, non-synthetic cohort):**
- **997** untraced individual owner_leads across **27 counties** (national: IN `18091`=652 + 26 MI counties).
- Worst-case spend **$19.94** (997 × $0.02; misses free) — **exceeds the $10 autonomous ceiling**, so the
  plan chunks by county/`--limit` (e.g. `18091` → 2 × 326).
- 555-range rows are demo fixtures injected as *traced/callable*, so they're excluded by the
  `skip_trace_status='not_traced'` predicate — the 997 is real.

**Operator fire (print-stop — each is a paid run, gated by the runner's own `--confirm`):**
```
python -m scripts.skip_trace_national_plan            # re-print the plan ($0)
python -m scripts.skip_trace_tracerfy --batch --county 18091 --limit 326 --confirm   # $6.52 worst-case
# … per the printed chunk list; DNC scrub + sync run after each.
```

---

## JOB 3 — 🟡 White-label modular search — foundation shipped, apply PRINT-STOP

**Why print-stop:** the core is a RESTRICTIVE RLS change on `public.acquisition_leads`, the **live
multi-tenant tenant table**, in a repo with **active parallel agents**. Applying tenant-isolation RLS
autonomously (or its two-tenant proof, which requires applying) is a deploy/consequential act → RED.

**Shipped (mig `084`, UNAPPLIED, verified-clean):**
- `public.tenant_config` — `enabled_lenses[]`, `enabled_modules[]`, `data_scope_counties[]`, `branding`
  jsonb, `default_recipe_id`. The seam the CRM/search reads.
- RESTRICTIVE `aq_data_scope_restrict` on `acquisition_leads` — a scoped tenant physically sees only
  in-scope counties; **additive-safe**: empty/absent scope → unrestricted, so AnchorPoint is unaffected
  until a scope is set.
- `public.search_leads(...)` RPC — `security_invoker` (RLS applies underneath), lens-gated to
  `enabled_lenses`, returns **projected columns only** (never the scoring engine). Facets: county, lens,
  asset_class, owner_type, score band, acreage, value band, dnc.
- Every referenced column + both RLS helpers (`app_current_tenant`/`app_current_role`, mig 010) verified
  to exist against the live spine; types match; `tenant_config` is new → **applies clean**.

**Remaining (gated on the print-stopped apply, coordinate with live CRM agents):**
1. Apply mig 084 (operator, after review). 2. Run the embedded two-tenant proof (demo A = Berrien/distress,
demo B = LaPorte/conversion → each sees only its slice). 3. Build the search UI over `search_leads`
(the rich client filter in `AcquisitionQueueClient.tsx` is the surface to extend). **Note:** `owner_type`
and `acreage` are read from `parcel_meta` jsonb on the projection; a synced column would make them
first-class facets.

---

## JOB 4 — ✅ Verify & close the rest (⚠️ verify-the-verifier: 3 "fails" were stale-branch artifacts)

An initial recon flagged 3 of 4 as FAIL. **Re-checking against `origin/main` (not the checked-out
worktree branch) overturned all three** — the recon had read stale branches (`apg-land-crm` was on
`feat/verify-harness @643f28a`, pre-mineral/pre-seal; `landos-engine` on `cct4-scrub-fix-mine`,
pre-034/036). Truth on `origin/main`:

| Item | Verdict | Evidence (origin/main) |
|------|---------|------------------------|
| Valuation Lab → Cass comps | ✅ PASS | mig `035_parcel_sale.sql` present; `feat(valuation): ground land value in real county SALE comps (Cass corridor)`; `valuation.py` land math real (median comps × acreage curve, honest-null when no comps). Endpoint is `/v1/valuation/{lead_id}?mode=land`. |
| Tax-sale promote gate | ✅ PASS | mig `036_taxsale_promote_gate.sql` present; `mint_taxsale_leads.py:48 and promoted = true -- 🔴 THE GATE`. An unpromoted taxsale signal does **not** mint. Nothing may auto-set `promoted=true`. |
| DS-07 insight `GET /v1/insight/{id}` | ✅ PASS | `main.py:248-266` + `insight/service.py`; returns brief/angle/do_not_say/grounding/model; honest-offline 503; backed by mig `032`. |
| Palette seal | ✅ PASS | `origin/main` = `c35c9de` (the seal commit). Grep `gray-/slate-/zinc-/neutral-` = **10 matches, all false positives** (`tran`**`slate-`**`x`, `translate-y-` …), **0 numbered palette tokens**. Sealed. (The "1267" was the stale pre-seal branch.) |

**Lesson recorded:** verify against the canonical ref (`origin/main`), never whatever branch a worktree
happens to sit on — a stale checkout produces false failures, which the "does-not-lie" doctrine forbids.

---

## FINAL SWEEP — N = 0

Branches pushed (all tracked on origin, nothing stranded):
- landos-engine: `fix/land-dd-fail-closed-auth` `4b9b62d` · `feat/national-skiptrace-plan` `ec9c85c`
- apg-land-crm: `feat/white-label-modular-search` `e1bde5e` (mig 084 UNAPPLIED)

Worktrees (isolated, each clean + pushed): `landos-land-dd-auth`, `landos-national-skiptrace`,
`apg-white-label`. No stashes created. Primary clones and active parallel worktrees untouched.

**Print-stopped for the operator (RED):** set `NVE_LAND_TOKEN` (J1) · fire the paid skip-trace chunks
(J2) · apply mig 084 + run the two-tenant proof (J3). Nothing here spent, deployed, or mutated the
production DB.

# PRIORITY: capture assessor valuation into `raw_meta` — arms THREE disarmed detectors

**Status:** OPEN · BLOCKED on a paid-tier decision (see Correction)
**Fix location:** `app/normalizers/reportall_owner.py` `normalize()` (NOT enrich_owner_chain L273)
**Cost:** NOT $0 — the valuation source is the metered ReportAll vendor API
**Unblocks:** `lot_value_arbitrage`, `aging_owner_luxury`, and (partially) `exemption_drop`

---

## CORRECTION (2026-07-08) — an earlier version of this doc was wrong

The earlier draft claimed the valuation fields (land_value / improvement_value / yr_blt /
last-sale) are **"already fetched $0 at `enrich_owner_chain.py` ~L273 (county_rest_attributes),
then discarded."** That is **false**, verified by sampling the live spine:

- **L273 `county_rest_attributes` is the FREE county-ArcGIS tier, not ReportAll.** Sampled
  the richest verified county (St. Clair 26147, 34 fields): the only valuation-ish fields
  are `BLD_ASSESSMENT` (building assessment) and a `homestead` flag — **both `0.0`** for the
  sampled parcel. Montcalm (26117): none. The `COUNTY_REST_ENDPOINTS` config maps
  **owner / mailing / situs only** — no land-value, no year-built, no sale-date field for any
  county. So the free tier does **NOT** carry what the three detectors need.
- **The luxury/teardown geography has no free source at all**: St. Joseph IN (18141,
  Beacon/Schneider — no open REST) and Van Buren MI (26159, South Haven — MangoMap paywalled).
- **The real fetch-then-discard of valuation is the PAID ReportAll tier.** `ReportAllProvider`
  is `cost_note = "metered vendor API (alltime quota per key)"`. Its normalizer
  (`reportall_owner.py` ~L256) keeps owner / situs / mailing / land_use / acreage / robust_id
  and **discards the rest of ReportAll's raw record `r`** — which is where mkt_val_land /
  mkt_val_bldg / yr_blt / sale info / homestead would ride. THAT is the discard to fix.

Net: the capture is real and worth doing, but it is **not $0** and it is **not at L273**.

## The corrected ask

In `reportall_owner.py::normalize()`, stow the valuation fields from the raw ReportAll
record `r` into `raw_meta` under the **exact keys the detectors read** (verified against the
adapters — do not invent keys):

| raw_meta key | detector(s) reading it | likely ReportAll field (VERIFY) |
|---|---|---|
| `land_value` | lot_value_arbitrage | `mkt_val_land` |
| `sev` (already present) | lot_value_arbitrage (total) | `mkt_val_tot` / assessed |
| `yr_blt` | lot_value_arbitrage, aging_owner_luxury | `yr_blt` |
| `last_sale_date` | lot_value_arbitrage, aging_owner_luxury (→tenure) | `trans_date` / sale date |
| `senior_exemption` | aging_owner_luxury | homestead/senior exemption flag |
| `vintage.exemption_flags` | exemption_drop | homestead/PRE flag, ≥2 vintages |

Rules: **coalesce-existing-first** (never overwrite a populated value with empty),
**honest-null** (missing/blank field → omit the key or null, never a coerced `0` — a `0`
land value fabricates a `0.0` land_ratio). Writes go to `landos.owner_leads.raw_meta` only,
never `public.acquisition_leads` (single-writer).

## Why this is BLOCKED (and on what)

1. **It costs metered ReportAll quota.** Capturing valuation going forward needs the paid
   tier on the enrichment path; **backfilling the 286 already-enriched parcels needs a paid
   RE-RUN** (the discarded valuation was never persisted). Operator spend decision required.
2. **ReportAll's exact valuation field names are unverified** — no cached raw sample exists in
   the repo. Confirm from ReportAll's API docs or one live response before shipping the
   field-map, so the capture reads the right keys (else honest-null everywhere → arms nothing).
3. **`exemption_drop` needs ≥2 vintages regardless** — one capture yields only the FIRST
   vintage; the sleeper diffs consecutive captures, so it cannot arm from a single run.

## Arm expectation (honest)

- `lot_value_arbitrage`, `aging_owner_luxury`: **can** arm from a single paid ReportAll capture
  IF the response carries land_value + yr_blt (+ sale for tenure) AND the parcel is in a luxury
  zip. ReportAll covers the luxury geography (St. Joseph IN / Berrien MI are in its config).
- `exemption_drop`: capture enables vintage #1; arms only after a second capture over time.

## Definition of done (unchanged, but gated on the paid GO)

1. Operator approves the metered ReportAll tier + a batch re-run budget.
2. Confirm ReportAll field names; wire the normalizer capture (coalesce, honest-null).
3. Re-run enrichment for a batch; `census_live` for the three adapters flips to `covered: true`
   with real counts. That re-census IS the arm proof — do not claim an arm without it.

See: `app/normalizers/reportall_owner.py`, `app/providers/reportall_provider.py`,
`app/discovery/{lot_value_arbitrage,aging_owner_luxury,exemption_drop}.py`.

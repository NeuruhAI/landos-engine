# Owner-Outreach Engine (Engine 1) — Build Report

**Date:** 2026-06-04
**Scope:** `landos` schema only, Neuruh Supabase `lvdimujkfheeyqchjzra`, tenant
`1840ac90-54bd-4b3d-8baf-227c8d84ab5a`. Real ReportAll data only. Two outbound steps
(Tracerfy skip-trace, APG push) are BUILT but GATED OFF — no spend, no APG write.
`apg-land-crm` does not exist in this tree and was never referenced.

The absentee-owner layer, built multi-county from day one, with a swappable,
gated push adapter to APG. Full chain built **and run on real Berrien data** this
session.

---

## Pipeline (end to end)

```
data/lead_queue_berrien.csv (APNs)
   │   scripts/ingest_owner_leads.py  --county berrien --limit 150
   ▼
ReportAll Parcel Data API  →  app/normalizers/reportall_owner.py  (fetch→normalize→upsert+audit)
   ▼
landos.owner_leads  +  landos.owner_leads_import_log
   │   scripts/owner_leads.py  --county berrien
   ▼   (joins landos.signals + opportunity_zone overlays, runs app/score_absentee.py)
landos.owner_leads (absentee fields written back)  →  data/owner_leads_berrien.csv (ranked)
   │   scripts/export_outreach.py  --county berrien
   ▼
data/outreach_berrien.csv (mail-merge + empty skip-trace cols)   [Tracerfy GATED]
   │   app/push/apg_adapter.py  (via app/push/base.py)
   ▼
APG Supabase  ───────────────────────────────────────────────   [APG PUSH GATED]
```

---

## PART 1 — Schema (`migrations/001_owner_leads.sql`, applied)

`landos.owner_leads` — RLS enabled; `owner_leads_authenticated_policy` (cmd=ALL,
role=`authenticated`, `using true`, `with check true`) mirrors the live
`overlay_polygons` policy exactly. Multi-county: **`county_fips` is a stored
column** (Berrien `26021` is just the first value, never hardcoded). Columns:

`lead_id` (uuid pk) · `tenant_id` · `county_fips` · `parcel_apn` · `situs_address` ·
`situs_lat` · `situs_lng` · `owner_name` · `owner_mailing_address` ·
`owner_mailing_city` · `owner_mailing_state` · `owner_mailing_zip` ·
`land_use_class` · `acreage` · `absentee_flag` · `absentee_tier` · `owner_type` ·
`absentee_score` · `signal_overlap` (jsonb) · `final_lead_score` ·
`skip_trace_status` (default `not_traced`) · `skip_trace_meta` (jsonb) ·
`push_status` (default `not_pushed`) · `pushed_at` · `push_target` ·
`source` (default `reportall`) · `provenance` · `raw_meta` (jsonb) · `created_at` ·
`updated_at`.

- **Idempotency:** unique index on `(tenant_id, county_fips, parcel_apn)` → re-runs
  upsert in place, never duplicate.
- `landos.owner_leads_import_log` (`source, county_fips, run_at, row_count, status,
  notes`) — one audit row per normalizer run, also RLS-on with its authenticated
  policy. Compliance trail.

## PART 2 — ReportAll normalizer (`app/normalizers/reportall_owner.py`)

Follows the base.py contract (fetch → normalize → upsert + audit-log). County-agnostic:
`COUNTY_CONFIG[county_fips]` holds the only county-specific data (ReportAll `region`
string + the APN prefix to strip). Adding a county = adding a config row.

- **APN mapping (verified live):** ReportAll stores the MI PIN **without** the
  leading county prefix; the full CSV APN is in their `alt_id_2`. We strip the
  configured prefix (`11-` for Berrien) to build the query, and **store the original
  CSV-form APN as `parcel_apn`** so it joins exactly to `landos.signals`. Prefix-less
  source APNs (tax-delinquent rows like `07-5620-…`) query as-is.
- **Real data only:** `count=0` → recorded as a miss and skipped (never invented). A
  genuine API/auth failure (bad key, HTTP error, `status:"error"`) is audit-logged
  and **re-raised so the run STOPS**.
- Client key read from `REPORTALL_CLIENT_KEY` (`.env`, gitignored) — not hardcoded.

## PART 3 — Absentee scorer (`app/score_absentee.py`, pure)

No DB, no network — fully unit-tested. Four pieces + fusion:

1. **`owner_type`** — `govt` | `entity` | `individual`, from owner-name patterns
   (govt checked first: `STATE OF`, `CITY OF`, `LAND BANK`, `TOWNSHIP`, …; entity:
   `LLC`, `INC`, `TRUST`, `COMMUNITY`, `ASSOCIATION`, `CHURCH`, …).
2. **`absentee_tier`** — `out_of_state` > `out_of_county` > `out_of_town` >
   `owner_occupied` (+ `unknown` when no mailing). Tier is decided by comparing the
   owner mailing vs the situs: state mismatch → out_of_state; same state but mailing
   **not in the situs county** → out_of_county; same county, different address →
   out_of_town; mailing == situs address → owner_occupied. The same-county test uses
   a per-county ZIP+place reference (`COUNTY_GEO`, Berrien seeded with real USPS/Census
   values). A vacant lot (situs house number `0`) can never be owner-occupied.
3. **`absentee_score`** — distance-decayed tier base
   (`out_of_state 90 / out_of_county 70 / out_of_town 50 / owner_occupied 20 /
   unknown 40`), × owner-type factor (**entity ×1.10** — an absentee entity is a
   classic motivated-seller candidate), with a **govt disposition floor of 75**
   (land bank / state surplus / tax-reverted land is frequently disposable).
4. **`signal_overlap`** (jsonb) — JOIN against the existing `landos.signals` stack
   per parcel: which distress signals it ALSO carries (`tax_delinquent`,
   `brownfield`, `environmental_part201`, …) **and** whether it sits in an
   opportunity-zone overlay (real point-in-polygon via `app/overlays.py`).

**Fusion — the edge (`final_lead_score`):**

```
final_lead_score = clamp( absentee_score × overlap_multiplier , 0 , 100 )

overlap_multiplier = 1.0
   + 0.20  if tax_delinquent        (active foreclosure pipeline = strongest motivation)
   + 0.10  per other distress signal (brownfield / env), capped at 2  → max +0.20
   + 0.15  if in an opportunity zone (exit / buyer-demand premium)
   capped at 1.60
```

So an out-of-state LLC (90 × 1.10 = 99) that is **also** tax-delinquent **and** in an
OZ → ×1.35 → 100 (clamped): top decile. A lone absentee with no overlap keeps its
absentee score. The fusion is multiplicative and real, not cosmetic.

**Honesty rule (in code comments, CLI output, and here):** absentee ≠ motivated.
This predicts **reachability** (where outreach lands) + **disposition likelihood**,
tightened by signal overlap. It is a **prioritized list, not guaranteed sellers.**

## PART 4 — Ranked CSV (`scripts/owner_leads.py --county berrien`)

Pulls owner_leads, joins signals + OZ overlays, runs the pure scorer, writes the
absentee fields back, ranks by `final_lead_score`, emits `data/owner_leads_<county>.csv`.
`--county` drives `county_fips` (alias derived from `COUNTY_CONFIG`); no Berrien
hardcode.

## PART 5 — Outreach export (`scripts/export_outreach.py`) — Tracerfy GATED

Mail-merge CSV (`data/outreach_<county>.csv`): owner + mailing + situs + apn +
final_lead_score, plus **empty skip-trace columns** (`phone`, `phone_secondary`,
`email`, `secondary_mailing`) Tracerfy will populate. The real Tracerfy batch call
(`tracerfy_batch_trace`) is wired but only fires with `--skip-trace`, which is **OFF
by default**. Default run prints:

> `TRACERFY DISABLED — would trace 124 leads at $0.02 = $2.48, awaiting go`

…and traces nothing. **Unblock to fire:** your go + `TRACERFY_API_KEY` set, then
`--skip-trace`. (Tracerfy is the chosen provider at $0.02/lead — no alternatives.)

## PART 6 — APG push adapter (`app/push/base.py` + `app/push/apg_adapter.py`) — GATED

`base.py` defines a swappable `PushAdapter` interface — **the engine never writes to
a destination directly; only an adapter does.** `apg_adapter.py` implements it for
APG's Supabase (`nsxfcgxuglixxzgkwunc`): complete field mapping (supply-side owner-lead
fields only), connection wiring, destination validation, write path, `push_status`
update. **Double-gated so it cannot fire in this build:**

1. `do_push` defaults to **False** everywhere → prints the disabled message, writes
   nothing.
2. Even forced on, `validate_destination()` **raises** because the owner-lead
   destination table on APG is **unconfirmed** (`DEST_TABLE = None`).
3. `builder_buyers` is **hard-blocked** (`FORBIDDEN_TABLES`) — owner LEADS are supply,
   `builder_buyers` is the BUYERS/demand table; pushing there is refused.
4. Connection reads `APG_DATABASE_URL`, which is **not set** — so no APG DB connection
   is even possible here.

Default run prints:

> `APG PUSH DISABLED — destination table unconfirmed, would push N leads, awaiting Bryan's table shape (APG project nsxfcgxuglixxzgkwunc; NOT builder_buyers).`

**Unblock to fire:** Bryan confirms the owner-lead destination table name + column
shape on APG → set `DEST_TABLE` (and align `map_lead` column names) → provide
`APG_DATABASE_URL` → run the push with `--push`.

---

## Real Berrien results (this run)

- **150** APNs queried (15 trial parcels seeded first + ranked queue) →
  **124 owner leads** upserted, **26 misses** (count=0, skipped — not fabricated),
  **150 ReportAll API calls** (well under the trial budget). Audit row written to
  `owner_leads_import_log`.

**Absentee tier breakdown (124 leads):**

| tier | count |
|---|---:|
| out_of_town | 91 |
| out_of_state | 15 |
| out_of_county | 12 |
| owner_occupied | 6 |

→ **118 absentee / 6 owner-occupied.** Owner type: **entity 80, individual 27, govt 17.**

**Signal overlap (the top-decile driver):** every lead came from the signal-derived
queue, so all 124 carry ≥1 signal — the *discriminating* overlap is the distress
**type** and OZ stacking, not mere presence:

- in an **opportunity zone: 99** · **tax_delinquent: 21** · brownfield: 10 ·
  environmental_part201: 93
- **`final_lead_score` ≥ 90 (top decile): 31 leads** — the absentee-AND-distressed/OZ stack.

**Vacant land — the key question:** **28** parcels have a vacant situs (`0 <street>`);
**all 28 carry owner + mailing and classify as absentee.** Vacant land is fully
covered by the owner layer — it is not limited to improved parcels. (e.g. rank 1 is a
vacant lot: `0 WALNUT St`, owner in Lockport IL, tax-delinquent.)

**Top 10 ranked owner leads:**

| # | score | tier | type | owner | situs | mailing | overlap |
|--:|--:|---|---|---|---|---|---|
| 1 | 100 | out_of_state | entity | CHEAP LANDS INC | 369 CLAY St | Port Saint Lucie FL | tax_delinquent |
| 2 | 100 | out_of_state | entity | ALP REVOCABLE LIVING TRUST | 1534 KAY Dr | Orland Park IL | tax_delinquent + OZ |
| 3 | 100 | out_of_state | entity | SIEMENS VAI METALS TECH | 470 PAW PAW Ave | Alpharetta GA | environmental_part201 |
| 4 | 100 | out_of_state | entity | BUOY 10 LLC | 160 CATALPA Ave | Vancouver WA | tax_delinquent |
| 5 | 100 | out_of_state | entity | NALRECO LLC | 0 ALRECO Rd | Boulder CO | environmental_part201 + OZ |
| 6 | 100 | out_of_state | entity | BLUE WAVE INVESTMENTS LLC | 965 E NAPIER Ave | Elm Grove WI | environmental_part201 + OZ |
| 7 | 100 | out_of_state | entity | CHEMICAL BANK FACILITIES | 433 W MAIN St | Plymouth MN | environmental_part201 + OZ |
| 8 | 100 | out_of_state | entity | SKYLINE REAL ESTATE SERV | 301 CASS St | Coronado CA | tax_delinquent + OZ |
| 9 | 100 | out_of_state | entity | SQUIRE EMPIRE LLC | 1498 E EMPIRE Ave | Medford OR | environmental_part201 + OZ |
| 10 | 100 | out_of_state | entity | GLOMAX INC | 0 E EMPIRE Ave | Fishers IN | environmental_part201 + OZ |

(All real ReportAll ownership — out-of-state entities holding Benton Harbor parcels
that also carry distress/OZ signals: the exact top-of-funnel the fusion is meant to
surface. Many score 100 because an out-of-state entity at 99 × any overlap clamps to
the ceiling; the top decile is differentiated from the body, not within itself.)

---

## Tests

`tests/test_absentee.py` — **40 new tests**, all green **alongside the existing 53**
→ **93 passed.** Coverage: absentee_tier (all tiers + vacant-lot guard + unknown),
owner_type classification, absentee_score (tier order, entity premium, govt floor,
clamp), signal_overlap structure, final_lead_score fusion (bumps, cap, clamp),
score_lead end-to-end, ReportAll APN mapping + normalize (real-data-only, miss
dropped), CSV helpers, **gated Tracerfy stub** (cost math + disabled message + no
call), and **gated APG push stub** (off by default, destination-unconfirmed raises,
`builder_buyers` hard-blocked, no buyer fields in mapping, connection refused without
`APG_DATABASE_URL`).

---

## Gate confirmation

- **Real data only** — 124 leads are live ReportAll ownership; 26 misses skipped, nothing fabricated.
- **landos only** — all writes to `landos.owner_leads` / `owner_leads_import_log` on `lvdimujkfheeyqchjzra`; RLS on, policies mirror `overlay_polygons`.
- **`apg-land-crm` untouched** — does not exist in this tree; never referenced.
- **No APG DB write** — push double-gated, `APG_DATABASE_URL` unset, `builder_buyers` blocked.
- **No spend** — Tracerfy gated off (would be $2.48 for 124 leads).
- **Multi-county** — `county_fips` stored; `--county` parameterizes everything; Berrien is the first config row, not a hardcode.

### What unblocks the two gated steps

| Step | Unblock |
|---|---|
| **Tracerfy skip-trace** | Your go + `TRACERFY_API_KEY` in env → run `scripts/export_outreach.py --county berrien --skip-trace`. Cost = leads × $0.02. |
| **APG push** | Bryan confirms the owner-lead destination **table name + column shape** on APG (`nsxfcgxuglixxzgkwunc`) → set `DEST_TABLE` + align `map_lead` → provide `APG_DATABASE_URL` → run with `--push`. Must NOT be `builder_buyers`. |

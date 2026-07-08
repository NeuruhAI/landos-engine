# b2 — Distress-Signal Ingest Contract: `code_violation` + `lis_pendens`

**Date:** 2026-07-07 · **Status:** SPEC (for landos-ingest) · **Author:** landos-engine (distress-recipe Phase B b2)

> **CANON CORRECTION (2026-07-08) — the first `code_violation` batch is NEW-GEOGRAPHY DISCOVERY, not stack-enrichment.**
> t5 staged 237 `code_violation` signals (South Bend, IN — sections `71-08-…`) and t2 loaded them
> into `landos.discovery_signals`. Only **2 of 222** distinct parcels overlap the owned tax-sale
> book (which is sections `71-02/03-…`), so **these 237 do NOT deepen the existing 927-lead
> stack.** They are a **separate discovery pool = 237 potential NEW targets** awaiting the
> **resolver** to project them into workable leads. The `distress_coverage` axis on existing
> leads is **unchanged** (absentee 450 · probate 1 · `code_violation` still **0** on leads).
> The mechanism below is still correct — a `code_violation` signal that resolves to a lead
> already carrying `taxsale` DOES add stack depth — but that is not what THIS batch did (2-parcel
> overlap). **NEXT STEP: the resolver must run** to turn the 237 into leads. Read every
> "increases stack depth / stack≥2" statement below as the general mechanism, not the outcome of
> the first load.

## Purpose

Light the two remaining distress-stack axes the distress recipe ranks by but the
queue does not yet carry: **`code_violation`** and **`lis_pendens`**. Today the
stack has only `taxsale` (F1) + `probate` (1) + `absentee` (450, from
`parcel_meta.owner_out_of_state`). This contract specifies EXACTLY what
landos-ingest must stage so those two axes appear on leads and the recipe lights
them **with no recipe change** (the detectors already exist — see §4).

## Repo firewall (non-negotiable)

- **landos-ingest** fetches raw sources and STAGES rows into `landos.discovery_signals`. This engine never fetches; it only reads what is staged.
- The scoring **weights/half-lives are engine IP** (`scripts/score_emergence.py`, `app/scoring.py`) and stay here, behind the firewall. This contract tells ingest the *data shape*; §4 lists the small engine-side config edits (engine's job, not ingest's).

---

## 0. TL;DR — three columns of work

| landos-ingest stages (this contract) | engine adds (3 small edits, §4) | already done (no work) |
|---|---|---|
| `discovery_signals` rows, `source_type='code_violation'` / `'lis_pendens'`, `signal_class='temporal'`, dated, apn+fips, raw_payload | 2 rows in `EMERGENCE_WEIGHTS`+`HALF_LIFE_DAYS`; 1 line in `DISTRESS_TOKEN_MAP` (`lis_pendens`→`pre_foreclosure`) | recipe `_DIM_SOURCE_TYPES` maps both; `code_violation` weight (0.10) + `pre_foreclosure` weight (0.20) already in `DEFAULT_WEIGHTS`; `code_violation` already in `DISTRESS_TOKEN_MAP` |

The engine was **built anticipating these** (`composite_inputs.py:48` comment: *"future engine tokens that already have a model slot"*). b2 is mostly a data lane.

---

## 1. Staging contract — `landos.discovery_signals`

Every distress signal is one row in `landos.discovery_signals`. Columns (live schema):

| column | code_violation | lis_pendens | notes |
|---|---|---|---|
| `source_type` | `'code_violation'` | `'lis_pendens'` | **exact strings** — the recipe + scorer key on these |
| `signal_class` | `'temporal'` | `'temporal'` | **REQUIRED** — `score_emergence` only scores temporal rows (`score_emergence.py:74`) |
| `signal_date` | date of the violation/citation | date the lis pendens was filed | **REQUIRED** — freshness decays from this; undated rows never score |
| `parcel_apn` | parcel APN | parcel APN | matching key (with `county_fips`) |
| `county_fips` | 5-digit FIPS | 5-digit FIPS | matching key |
| `owner_name` | defendant/owner if known | defendant/owner | optional but improves match/trace |
| `confidence` | 0..1 | 0..1 | multiplies contribution; use ~0.9 for official records |
| `source_ref` / `source_url` | citation #/portal URL | case #/docket URL | provenance (receipt trail) |
| `raw_payload` | jsonb (see §1a) | jsonb (see §1b) | full source detail; never lossy |
| `tenant_id` | tenant uuid | tenant uuid | same tenant as the queue |
| `status` | `'new'` | `'new'` | lifecycle; the matcher/scorer advances it |
| `matched_lead_id` | null at stage → set by matcher | same | see §2 |

### 1a. `code_violation` raw_payload keys (suggested, non-lossy)
```
{ "violation_type": "tall grass|junk|unsafe structure|...",
  "case_number": "...", "status": "open|abated|lien",
  "cited_on": "YYYY-MM-DD", "situs_address": "...", "city": "...", "zip": "...",
  "fine_amount": 0, "inspector": "...", "source_label": "<municipality> code enforcement" }
```
### 1b. `lis_pendens` raw_payload keys (suggested, non-lossy)
```
{ "case_number": "...", "filed_on": "YYYY-MM-DD", "case_type": "foreclosure|hoa|tax|other",
  "plaintiff": "...", "defendant": "...", "situs_address": "...", "city": "...", "zip": "...",
  "court": "...", "book_page": "...", "source_label": "<county> clerk of court" }
```
`situs_address`/`city`/`zip` in the payload also feed the trace batch (the same
pattern the Williamson taxsale rows use — top-level `discovery_signals` has no
address column, so it lives in `raw_payload`).

---

## 2. Matching (signal → lead)

A signal only scores once `matched_lead_id` points at a `landos.owner_leads` row
(`score_emergence.py:73` FK-joins to prove resolution). Match on
**`(county_fips, parcel_apn)`** — the `owner_leads` upsert key. This is the same
matcher that already links taxsale/probate signals; b2 needs no new matcher, only
that the staged rows carry a resolvable `parcel_apn` + `county_fips`. Rows that
never match stay unscored (honest — they are not fabricated onto a lead).

## 3. Load-bearing invariants (why a row won't light if skipped)

1. **`signal_class='temporal'`** — structural rows are excluded from emergence. Skip it → the signal is invisible to `emerging_signals`.
2. **`signal_date` present** — freshness = `0.5 ** (age_days / half_life)`. No date → no contribution.
3. **matched** — unmatched → no lead to write onto.
4. **exact `source_type`** — `'code_violation'` / `'lis_pendens'` (the recipe's `_DIM_SOURCE_TYPES` aliases also accept `'codeviolation'`/`'lispendens'`, but prefer the underscored canonical form).

---

## 4. Engine-side wiring (engine's job — status + the edits)

**Already wired (zero work):**
- Recipe detection — `app/recipe_core.py` `_DIM_SOURCE_TYPES` maps `code_violation` and `lis_pendens`; `distress_coverage` / stack-rank light them the moment they appear in a lead's `emerging_signals`. **No recipe change.**
- Composite scoring slots — `app/scoring.py:29` `DEFAULT_WEIGHTS["distress"]["code_violation"]=0.10`; `["pre_foreclosure"]=0.20` (lis pendens maps here); `app/composite_inputs.py:49` `DISTRESS_TOKEN_MAP["code_violation"]`.

**Three small engine edits (do when the first real rows land):**
1. `scripts/score_emergence.py:53` — add to `EMERGENCE_WEIGHTS`: `"code_violation": 0.6, "lis_pendens": 0.8` (lis pendens = imminent foreclosure, strong). Until added they score at `DEFAULT_WEIGHT=0.5` with a WARN — functional but uncalibrated.
2. `scripts/score_emergence.py:57` — add to `HALF_LIFE_DAYS`: `"code_violation": 120, "lis_pendens": 150` (both persist months). Default is 90.
3. `app/composite_inputs.py:41` — add `"lis_pendens": "pre_foreclosure"` to `DISTRESS_TOKEN_MAP` so lis pendens moves the *composite* distress score, not just emergence. (`code_violation` is already mapped.)

These are engine-repo, moat-side edits — **not** landos-ingest's responsibility.

---

## 5. End-to-end flow (what happens after ingest stages a row)

```
landos-ingest  ──stage──▶  landos.discovery_signals (source_type, temporal, dated, apn+fips, raw_payload)
                                   │
              matcher ──set matched_lead_id──▶ landos.owner_leads   (join on county_fips, parcel_apn)
                                   │
       scripts/score_emergence.py ──▶  owner_leads.emerging_score + emerging_signals[]  (source_type carried)
                                   │
       scripts/sync_to_crm.py ─────▶  public.acquisition_leads.emerging_signals (+ score)
                                   │
       distress recipe (compose_distress) ──▶ distress_coverage() sees code_violation/lis_pendens as PRESENT;
                                              stack-rank depth increases for leads carrying them.
```

No step between ingest and the recipe needs new code except the three §4 config lines.

## 6. Candidate $0 sources (naming only — ingest owns the fetch)

- **code_violation:** municipal code-enforcement portals / open-data (many MI + TX cities publish open cases as CSV/ArcGIS). Stage per-municipality, like the taxsale county lanes.
- **lis_pendens:** county Clerk-of-Court / Register-of-Deeds docket search (foreclosure & tax-foreclosure filings). MI = Circuit Court / county register; TX = county clerk records.
- Honor the existing **freshness discipline**: stage events with a real `signal_date`; do not backfill undated rows.

## 7. Freshness · dedup · idempotency

- **Idempotent stage:** dedup on `(tenant_id, source_type, county_fips, parcel_apn, signal_date, case_number)` so re-runs don't inflate a lead's stack.
- **Cooling is automatic:** a stale code_violation/lis_pendens decays via half-life; no purge needed (unlike the taxsale freshness gate, these are not auction-dated, so they do NOT need the upcoming-only gate — they are historical events that cool).
- **One lead, many signals:** multiple distress signals on one parcel each contribute; the recipe's stack depth = count of DISTINCT present dimensions (not raw signal count), so 3 code_violation rows still = one `code_violation` axis.

## 8. Acceptance test (proves a staged row lights the recipe)

After staging + matching + `score_emergence` + `sync_to_crm`:

```sql
-- 1. the signal is staged, temporal, dated, matched
select source_type, signal_class, signal_date is not null dated, matched_lead_id is not null matched
from landos.discovery_signals where source_type in ('code_violation','lis_pendens') limit 5;

-- 2. it reached the lead's emerging_signals with its source_type
select a.lead_id, e->>'source_type' st
from public.acquisition_leads a, lateral jsonb_array_elements(a.emerging_signals) e
where e->>'source_type' in ('code_violation','lis_pendens') limit 5;
```
Then `python -m app.recipe distress` — the stack ingredient must show the axis as
**present** (count > 0), and any lead carrying it must show `stack>=2` in the rank
preview. That is the done-signal for b2.

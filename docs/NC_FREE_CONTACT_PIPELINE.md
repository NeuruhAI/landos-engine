# NC FREE CONTACT PIPELINE

T20C · the canonical $0 NC contact-intelligence pipeline for DeedSonar / LandOS.

This document is the answer to "how do we clean a new NC estate?". It replaces
reconstructing the method from chat history. The machine-readable twin is
`contracts/free_contact_refresh.v1.json`; the operational delta rules live in
`docs/FREE_CONTACT_REFRESH_CONTRACT.md`.

**PHONE PRESENT ≠ CALLABLE.** Every product row leaves this pipeline with
`callability_status=NOT_AUTHORIZED`. Authorization is a separate, later lane.

**PROVIDER_SPEND_USD = $0.00.** No Twilio Lookup, Tracerfy, BatchData, paid
skiptrace, paid DNC, paid people-search, or paid phone validation is reachable
from this code path — `app/contact/official_fetch.py` refuses any URL outside
the official public-record allowlist.

---

## 1. Chain

```
CURRENT ESTATE
  → CANDIDATE NORMALIZATION      app/contact/estate_import.py, address_normalize.py
  → OWNER IDENTITY               app/contact/owner_identity.py
  → OWNER CLASS                  app/contact/owner_class.py
  → ROUTE                        (per class, below)
  → FREE SOURCE EVIDENCE         ncsbe_match.py, entity_public.py, local_owned.py
  → IDENTITY MATCH               ncsbe_match.py  (tiered, address-corroborated)
  → CONTACT MATCH                t12_append.py
  → FANOUT / AMBIGUITY CHECK     phone_graph.py
  → CONFIDENCE                   confidence.py   (transparent checklist)
  → RESOLUTION STATE             resolution.py   (every candidate, no NULLs)
  → PRODUCT OVERLAY              product_export.py
  → RECIPE YIELD                 yield_reports.py
  → REFRESH RECEIPT              delta_refresh.py
```

## 2. Inputs

| Input | Path | Change detection |
|---|---|---|
| CURRENT estate | `~/Desktop/DEEDSONAR_CONTACT_EXPORT_PREVIEW_*/{County}/master/*.csv` | identity tuple hash |
| NCSBE voter zips | `_raw/ncsbe/ncvoter*.zip` | sha256 |
| IRS EO BMF | `_raw/irs/eo_nc.csv` | sha256 |
| IRS Form 990 | `_raw/irs/*990*` | sha256 |
| Estate-case import | `_raw/estate/*.csv` (operator-supplied, lawful) | sha256 |
| SOS listing import | `_raw/sos/*.csv` (operator-supplied, lawful) | sha256 |
| Local owned evidence | prior T11/T12/T16/T19F artifact dirs | identity-safe sweep |
| Prior HIGH append | `phone_append_combined.csv` | reused for unchanged identities |
| Prior resolution ledger | `CANDIDATE_CONTACT_RESOLUTION.csv` | prior id set + tuple hashes |

## 3. Current estate discovery

`identity_tuple.master_csv_paths()` globs the per-county `master/*_master_*.csv`
files. Counts are discovered, never hardcoded — the historical 20,734 is a
past observation, not a constant.

`ESTATE_REVISION` = sha256 over (filename, file digest) for every master CSV,
sorted. It identifies the estate snapshot, not the match run.

## 4. Identity tuple

`identity_tuple.identity_tuple_hash(row)` = sha256 over the uppercased,
whitespace-collapsed join of:

```
candidate_id | owner_name | mailing_address | mailing_city | mailing_state
| mailing_zip | property_address_raw | property_address_normalized | property_zip
```

`candidate_id` alone is insufficient: a revised estate may reuse an id under a
different owner. Raw source values are preserved; normalization is for
comparison only. Comparison is deterministic — no LLM, no fuzzy match.

## 5. Owner routing

| Class | Free route |
|---|---|
| `LIVING_PERSON` / `MULTI_PERSON` | NCSBE person match, address-corroborated |
| `HEIRS` | living heir on the roll; decedent is never a target |
| `ESTATE` / `DECEASED` | requires `ESTATE_IMPORT` to reach a living representative |
| `TRUST` | trustee identity first; a trust name never enters person matching |
| `LLC` / `CORPORATION` / `OTHER_ENTITY` | `SOS_IMPORT` → officer/agent, then NCSBE |
| `NONPROFIT` | IRS EO BMF + Form 990 filed contact |
| `GOVERNMENT` | not a seller contact; terminal |
| `UNKNOWN` | triage |

Hard rules: entity names never run through voter-person matching; a decedent is
never a phone target; a registered agent or attorney phone is never labeled as
an owner phone.

## 6. Match tiers

- `TIER_A` — exact person + core mailing-address corroboration.
- `TIER_B` — person + partial/secondary address corroboration.
- `TIER_C` — weaker corroboration. **Never auto-appends.** It stays held until a
  separate precision court records precision ≥ 0.98 on n ≥ 40.
- Name-only never appends, at any tier.

## 7. Confidence

`confidence.score_contact()` is a scored checklist, not a model: identity tier,
address corroboration, name strength, common-name penalty, source reliability,
recency, independent-source corroboration, fanout penalty.

- `HIGH` (≥ 80) — enters the append candidate set.
- `MEDIUM` (55–79) — manual review only, never auto-appended.
- `REVIEW` (< 55, or any hard block) — never appended.

Hard blocks return `REVIEW` immediately: deceased target, confidential record,
ambiguous identity, phone invalid under the numbering plan. Thresholds are never
lowered to manufacture a bigger phone count.

## 8. Fanout

`phone_graph.analyze_phone_graph()` detects one phone reaching many candidates,
one phone reaching many unrelated owner identities, and shared
business/household numbers. ≥ 3 distinct owners forces `REVIEW`; 2 distinct
owners or ≥ 5 candidates applies a penalty. Ambiguity holds — a phone is never
fanned across candidates because the names look similar.

## 9. Resolution states

Every CURRENT candidate ends in exactly one explicit state. There is no implicit
NULL and no generic success merely because a number exists.

| State | Meaning |
|---|---|
| `PHONE_APPENDED_HIGH` | HIGH-confidence evidenced contact attached |
| `IDENTITY_MATCHED_VOTER_PHONE_ABSENT` | person found, record carries no phone |
| `TIER_C_HELD_NOT_APPENDED` | match found, deliberately held |
| `AMBIGUOUS_REJECTED` | more than one plausible identity |
| `COMMON_NAME_REJECTED` | name too common to separate |
| `NO_ADDRESS_CORROBORATED_NCSBE_MATCH` | no corroborated person match |
| `DECEASED_OWNER_NEEDS_HEIR_IDENTITY` | needs a living evidenced identity |
| `HEIR_ON_ROLL_NO_ACCEPTED_PHONE` | heir identified, no accepted phone |
| `ENTITY_NEEDS_LAWFUL_SOS_IMPORT` | needs operator-supplied SOS listing |
| `NONPROFIT_NO_990_PHONE` | EO matched, no filed contact |
| `TRUST_EXCLUDED_FROM_VOTER_PERSON_MATCH` | trustee identity required first |
| `GOVERNMENT_NOT_A_SELLER_CONTACT` | terminal, by policy |
| `UNRESOLVED` | owner string not yet classifiable |

## 10. Free source custody

`official_fetch.ensure_official_source()` is cache-first and hash-pinned. A file
already on disk is reused; a digest that differs from the pinned receipt is
recorded as `SOURCE_DRIFT` and never silently adopted. Downloads are restricted
to the official allowlist (NCSBE bulk, IRS SOI, IRS e-Postcard/990). Anything
else returns `REFUSED_NOT_OFFICIAL`. The receipt is `free_source_custody.json`.

There is no NC SOS interactive automation. SOS data enters only as an
operator-supplied file.

## 11. Delta refresh

| Case | Action |
|---|---|
| `UNCHANGED` identity | reuse prior HIGH evidence; do not rematch |
| `CHANGED` identity | rematch that candidate only |
| `NEW` candidate | match |
| `DROPPED` candidate | absent from CURRENT product; historical receipt retained |

Emitted counts: `candidates_total/new/changed/unchanged/dropped/rematch`,
`previous_high_reused`, `new_high_appended`, `medium_held`, `review_held`,
`held_tier_c`, `local_safe_matches`, `classified_total`, plus
`provider_spend=$0.00` and `production_mutated=NO`.

This is what prevents rescanning the whole estate on every revision.

## 12. Recipe provenance

Every contact and product row carries `primary_recipe`, `recipe_version`,
`score`, `score_version`, `global_rank`, and `qualification_state` when the
estate supplies them. Finding a phone **never** rewrites an opportunity score.
Three things stay distinct: **opportunity quality**, **contact resolution**,
**callability**.

## 13. Contact provenance

No naked phone numbers. Every accepted contact retains WHO (`matched_person`),
WHY (`why_matched`), SOURCE (`phone_source`, `source_row_id`, `source_hash`),
`observed_at`, `confidence_category`/`confidence_score`, `phone_role`,
`fanout_candidates`, `candidate_id`/`apn`/`county`/`fips`, and the recipe
provenance above. The runner raises rather than emitting a contact missing any
field in `t12_append.CONTACT_PROVENANCE_FIELDS`.

## 14. Yield reports

Aggregate, no PII — safe to publish:

- `RECIPE_CONTACT_YIELD.csv` — recipe × candidates/qualified/research,
  identity-resolved, appended, medium, review-held, unresolved, fill rate,
  high-confidence rate, county breakdown.
- `RECIPE_OWNER_CLASS_YIELD.csv` — recipe × owner class, same measures.
- `COUNTY_FREE_CONTACT_YIELD.csv` — county × the same measures plus owner-class cuts.

`yield_reports.recipe_intelligence()` derives the top/lowest recipes by fill and
high-confidence rate and the heaviest heirs/entity recipes. These are analytical
read-outs only; they never alter a recipe score.

## 15. Still-missing queue

`CURRENT_STILL_MISSING_PHONE.csv` is operational and actionable: candidate/APN/
county/owner/class/addresses, recipe/rank/score/qualification, plus
`resolution_state`, `unresolved_reason`, `attempted_sources`,
`last_attempted_at`, and `next_safe_free_route` — so the same failure is never
rediscovered from scratch.

## 16. Callability boundary

`callability_status=NOT_AUTHORIZED` on every row. This pipeline does not dial,
SMS, email, contact sellers, run DNC, or authorize callability. It does not
import contact rows into DeedSonar production.

## 17. PII / Git boundary

Committed: code, synthetic-fixture tests, docs, contracts, `.gitignore`.
Never committed: real phone datasets, candidate contact exports, resolution
ledgers, still-missing queues, NCSBE raw bulk files, runtime DB, credentials,
`.env`. Products land on `~/Desktop/DEEDSONAR_FREE_CONTACT_CANONICAL`; non-PII
receipts land in the evidence directory. The aggregate yield CSVs carry no
names, phones, APNs, or candidate ids — `yield_columns_are_aggregate()` asserts
this and a test enforces it.

## 18. Replay

```bash
# delta refresh (default) — rematch only new/changed identities
.venv/bin/python scripts/run_nc_free_phone_enrichment.py --skip-census

# full rebuild — every living-person identity re-matched against NCSBE
.venv/bin/python scripts/run_nc_free_phone_enrichment.py --full-rematch

# tests
.venv/bin/python -m pytest tests/test_nc_free_contact_canonical.py \
                          tests/test_nc_free_phone_enrichment.py
```

`--skip-census` is valid only while the NCSBE zip digests in
`03_COUNTY_PHONE_CENSUS.csv` still match disk. If any digest drifts, omit it.

## 19. Failure recovery

| Symptom | Cause | Action |
|---|---|---|
| `SOURCE_DRIFT` in `free_source_custody.json` | an official input changed on disk | re-census, re-run without `--skip-census` |
| `candidates_rematch` unexpectedly equals the whole estate | prior ledger missing or unreadable | confirm `--prior-*` paths; expect a long run |
| runner raises `contact missing provenance` | an append row lost a provenance field | fix the producing route; never strip the check |
| `EVERY_CURRENT_NC_CANDIDATE_CLASSIFIED=NO` | ledger shorter than the estate | assertion fires in-run; do not publish the product |
| phone count jumps sharply | a threshold or tier gate was weakened | diff `confidence.py` and the TIER_C hold |

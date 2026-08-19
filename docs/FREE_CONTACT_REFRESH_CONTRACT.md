# FREE CONTACT REFRESH CONTRACT

T20C · canonical $0 NC public-record contact intelligence.

Machine contract: `contracts/free_contact_refresh.v1.json`.

## Purpose

Classify **every CURRENT NC candidate** (not only Qualified) and re-enrich only **new or changed** identities after an estate revision. Never duplicate append rows. Never call paid providers. Never mutate production. Never treat a present phone as callable.

## Trigger (manual)

Delta refresh is the default. Operator runs:

```
python scripts/run_nc_free_phone_enrichment.py --skip-census
```

`--skip-census` is allowed only when NCSBE zip SHA256 in `03_COUNTY_PHONE_CENSUS.csv` still matches the on-disk zips. If any zip hash drifts, omit the flag (re-census + re-hash). Do not re-download unless a file is missing or hash-mismatched.

`--skip-local` skips the identity-safe local-file sweep. Use it only on an unchanged estate whose prior court already recorded LOCAL_SAFE_PHONE_MATCHES=0.

`--full-rematch` rebuilds every living-person identity against NCSBE. Use it only when the delta ledger is missing or a court has failed.

`--prior-append` / `--prior-missing` default to the previous product directory's `phone_append_combined.csv` and `CURRENT_STILL_MISSING_PHONE.csv`.

Run order may prioritize Qualified, then high score, then high rank. Match outcomes remain per-candidate deterministic.

No scheduler is installed by this lane.

## Inputs

| Input | Path | Change detection |
|---|---|---|
| CURRENT estate | `~/Desktop/DEEDSONAR_CONTACT_EXPORT_PREVIEW_*/{County}/master/*.csv` | `candidate_id` + owner/address tuple hash (`app.contact.identity_tuple`) |
| Prior HIGH append | `phone_append_combined.csv` | kept for unchanged identities |
| Prior missing / resolution ledger | `CURRENT_STILL_MISSING_PHONE.csv` / `CANDIDATE_CONTACT_RESOLUTION.csv` | prior CURRENT id set + unresolved reasons |
| NCSBE zips | `~/neuruh-workspace/T19F_DEEDSONAR_FREE_PHONE_ENRICHMENT_*/_raw/ncsbe/ncvoter*.zip` | sha256 |
| IRS EO BMF | `_raw/irs/eo_nc.csv` | sha256 |
| Estate import | `_raw/estate/*.csv` excluding template | file sha256 |
| SOS import | `_raw/sos/*.csv` excluding template | file sha256 |

## Delta rule

Let `E` be the new estate, `P` the previous HIGH append (`phone_append_combined.csv`).

1. **Unchanged identity** — same `candidate_id` and same owner+address tuple → keep prior HIGH row; do not rematch.
2. **Changed identity** — rematch that candidate only.
3. **New candidate_id** — match.
4. **Dropped candidate_id** — omit from new append; do not delete historical receipt.

Every CURRENT row receives an explicit `contact_resolution_state`. Phone-present is not sufficient.

Every resulting contact retains: WHO (`matched_person`) · WHY MATCHED · SOURCE · OBSERVED_AT · CONFIDENCE · PHONE ROLE · CANDIDATE · RECIPE · SCORE/RANK.

Emit a receipt JSON (`refresh_receipt.json`):

```
estate_revision
ncsbe_zip_sha256[]
candidates_total
candidates_new
candidates_changed
candidates_unchanged
high_appended
held_tier_c
local_safe_matches
classified_total
provider_spend=$0.00
production_mutated=NO
```

## Yield files (outside Git when they carry PII)

Aggregate (no phones / no names / no APNs):

- `RECIPE_CONTACT_YIELD.csv`
- `RECIPE_OWNER_CLASS_YIELD.csv`
- `COUNTY_FREE_CONTACT_YIELD.csv`

PII product (Desktop / evidence, never Git):

- `CURRENT_STILL_MISSING_PHONE.csv`
- `CANDIDATE_CONTACT_RESOLUTION.csv`
- `phone_append_combined.csv`

## Invariants

- Name-only never appends.
- TIER_C never auto-appends unless a later court file records PRECISION ≥ 0.98 on n≥40.
- Decedent never a phone target.
- Entity never through voter-person match.
- Registered agent / attorney phones never labeled owner.
- libphonenumber = numbering-plan only.
- PHONE PRESENT ≠ CALLABLE. Product `callability_status=NOT_AUTHORIZED` until a later DNC lane.
- Append CSVs and PII exports stay outside Git.
- No SOS interactive automation.
- No paid Tracerfy, BatchData, Twilio Lookup, or people search.
- No seller contact, SMS, email, or dial.

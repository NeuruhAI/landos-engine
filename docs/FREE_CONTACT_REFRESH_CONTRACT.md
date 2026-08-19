# FREE CONTACT REFRESH CONTRACT

T19F · $0 NC public-record contact intelligence.

## Purpose

Re-enrich only **new or changed** CURRENT identities after an estate revision. Never duplicate append rows. Never call paid providers. Never mutate production.

## Trigger (manual)

Operator runs:

```
python scripts/run_nc_free_phone_enrichment.py --skip-census
```

`--skip-census` is allowed only when NCSBE zip SHA256 in `03_COUNTY_PHONE_CENSUS.csv` still matches the on-disk zips. If any zip hash drifts, omit the flag (re-census + re-hash). Do not re-download unless a file is missing or hash-mismatched.

No scheduler is installed by this lane.

## Inputs

| Input | Path | Change detection |
|---|---|---|
| CURRENT estate | `~/Desktop/DEEDSONAR_CONTACT_EXPORT_PREVIEW_*/{County}/master/*.csv` | `candidate_id` set + `owner_name`/`mailing_*`/`property_address_*` tuple hash |
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

Emit a receipt JSON:

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
provider_spend=$0.00
production_mutated=NO
```

## Invariants

- Name-only never appends.
- TIER_C never auto-appends unless a later court file records PRECISION ≥ 0.98 on n≥40.
- Decedent never a phone target.
- Entity never through voter-person match.
- Registered agent / attorney phones never labeled owner.
- libphonenumber = numbering-plan only.
- PHONE PRESENT ≠ CALLABLE. Product `callability_status=NOT_AUTHORIZED` until a later DNC lane.
- Append CSVs stay outside Git.
- No SOS interactive automation.

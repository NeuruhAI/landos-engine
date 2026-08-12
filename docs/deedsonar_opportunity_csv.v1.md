# deedsonar_opportunity_csv.v1 — versioned portable opportunity schema

Produced by `scripts/export_opportunities.py` (T4E Opportunity File Factory).
PRIMARY OBJECT: the PROPERTY OPPORTUNITY. Contact fields are OPTIONAL and never
determine inclusion. Column ORDER is part of the contract.

## Truth invariants
- **UNKNOWN ≠ ZERO/FALSE/NO.** An unknown value is an EMPTY cell. Never a
  fabricated `0` (`mortgage_balance`, `estimated_equity`, `property_value`),
  never a fabricated `false` (`absentee/vacancy/lien/code_violation` flags),
  never a guessed mailing state.
- Recipe assignment, scores, versions, and the global rank are copied verbatim
  from the canonical ranked universe (recipe-engine `rank_nc_curative_raw_v3`).
  Nothing is recomputed or invented at export time.
- Global ranking IS canonically defined for the current universe
  (`rank` 1..N under `nc_curative_rank.v3`); if a future source lacks it,
  `global_rank`/`global_score` export as `NOT_DEFINED`, never a local ordering.

## Columns (58, in order)
| group | columns | source / semantics |
|---|---|---|
| identity | external_lead_id, property_key, parcel_apn | `property_key` = `<STATE>:<county_fips>:<parcel_apn>` (the engine mint's property_id convention); external_lead_id mirrors it |
| geography | county, county_fips, property_state | state derived from FIPS prefix (37→NC) |
| situs | property_address, property_city, property_zip | verbatim |
| owner | owner_name, owner_mailing_address/_city/_state/_zip | verbatim; blank = unknown |
| recipe | recipe_id, recipe_name, recipe_version, scoring_version | recipe_name mirrors recipe_id — no canonical display names exist upstream |
| recipe rank | recipe_rank, recipe_score, priority_tier | recipe_rank contiguous 1..N inside each per-recipe file; recipe_score = curative_priority_score |
| global rank | global_rank, global_score | canonical `rank` / score, or NOT_DEFINED |
| signals | signal_count, signal_codes, signal_summary | count, source_types, trigger_classes |
| stack | stack_count, stacked_recipes | DESCRIPTIVE ONLY: canonical data assigns exactly one recipe per property (router, first match wins) → stack_count=1; no cross-recipe strategy is invented |
| reasons | primary_opportunity_reason, secondary_opportunity_reasons | derived from canonical score_reasons ordered by points desc |
| economics | property_value, assessed_value, estimated_equity, estimated_equity_pct, mortgage_balance | market_value/equity/mortgage currently UNKNOWN → empty |
| tax | tax_delinquent_amount, tax_years_due | tax_balance, pipe-joined years |
| flags | foreclosure_flag, probate_heir_flag, absentee_owner_flag, out_of_state_owner_flag, vacancy_flag, lien_flag, code_violation_flag | lowercase true/false when known; EMPTY when not canonically computed (absentee, vacancy, lien, code_violation) |
| evidence | evidence_summary, evidence_count, source_names, source_refs, confidence, contradictions | source_types+court+parcel_match; signal_count; source_families; source_urls; confidence/contradictions empty (no canonical per-row values) |
| contact (OPTIONAL) | phone_1, phone_2, email_1, contact_status | never gates inclusion; contact_status = canonical phone_status (NOT_ENRICHED) |
| provenance | source_run_id, scoring_run_id, export_run_id, schema_version, created_at, exported_at | source_run_id = sha256 prefix of the source CSV; scoring_run_id = NOT_DEFINED (no canonical scoring run-id artifact exists); created_at = ranked-universe run date |

## Deterministic ordering
Per-recipe files: `recipe_score` desc → canonical `global_rank` asc → `property_key` asc;
`recipe_rank` assigned 1..N after the sort. Master: `global_rank` asc. Top-N slices are
emitted only when the recipe's FULL universe exceeds N (a slice equal to FULL would be
a duplicate; a shorter one would be arbitrary truncation).

## Companion DeedSonar import file
`DEEDSONAR_IMPORT_READY_MASTER.csv` re-expresses the master through DeedSonar's own
canonical county-import headers (perfect auto-map) with an EXPLICIT
`source_row_id` = property_key (cross-file-stable identity) and every v1 column
appended (they ride verbatim into `acquisition_leads.source_raw_payload` — the only
metadata home DeedSonar offers today). The `dnc` column is deliberately absent:
an unknown DNC fact is never exported.

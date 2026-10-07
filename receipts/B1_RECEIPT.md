# B1 RECEIPT — Estate import readiness

```
STEP=B1
STATE=PREPARED
IMPORTER_CONTRACT_DOCUMENTED=YES
VALIDATOR_WRITTEN=YES
SYNTHETIC_FIXTURE_PASSES=YES
OPERATOR_GUIDE=YES
REAL_PERSON_DATA_USED=NO
COURT_SYSTEM_CONTACTED=NO
DNC_MUTATED=NO
OUTBOUND=NO
PROVIDER_SPEND=$0.00
```

Preparation only. No file was dropped into `_raw/estate/`. No enrichment run.
No NCSBE scan. No DeedSonar projection. No DNC write. No outbound. No spend.

---

## Importer contract (from `app/contact/estate_import.py`)

The loader has never been given a data file. It glob-loads `{raw_dir}/estate/*.csv`
except names ending `template.csv`. Encoding UTF-8, comma delimiter, header is
the first row, `source_date` stored as text.

| Item | Exact value |
|---|---|
| Path | `_raw/estate/` (`{raw_dir}/estate/` where `--raw-dir` is the enrichment `_raw` dir) |
| Filename | `{county_slug}_estate_{YYYY-MM-DD}.csv` e.g. `wake_estate_2026-08-28.csv` |
| Encoding | UTF-8, no BOM |
| Delimiter | comma |
| Header | first row = `ESTATE_IMPORT_COLUMNS` |
| Date | `source_date` = `YYYY-MM-DD` |
| Living fields | `applicant_name`, `co_applicant_name`, `personal_representative_name`, `heir_name` only |
| Never a target | `decedent_name`; attorney via `attorney_name` (provenance) or relationship `ATTORNEY`/`COUNSEL`/`LAWYER`/`ESQ` (row skipped) |
| Authority | this importer does **not** write `LEGAL_AUTHORITY` |

---

## What landed

- `docs/ESTATE_IMPORT_OPERATOR_GUIDE.md` — Wake eCourts is interactive HTML, not
  this CSV. Operator transcribes Register of Actions parties. Never script
  nccourts.gov / the Portal.
- `app/contact/estate_import_validate.py` + `scripts/validate_estate_import.py`
  — column-level PASS/FAIL, `imported: NO`.
- `tests/fixtures/estate/wake_estate_2026-08-28.csv` — synthetic names only
  (ALDERNEY / MAPLEWOOD / FAIRHAVEN / RIVERBEND / STONECROFT / NORTHFORK /
  CEDARHILL / WILLOWBEND). 5 rows → 6 `EstateCasePerson` records, each with
  `heir_person_id` `HEIR_…` and `person.role=NAMED_HEIR_ON_ROLL`.
- `tests/test_estate_import_readiness.py`

Validator on the fixture (this lane):

```
imported: NO
rows: 5
preview_people: 6
OVERALL PASS
```

Tests: `.venv/bin/python -m pytest tests/test_estate_import_readiness.py
tests/test_nc_free_phone_enrichment.py::test_estate_import_preserves_relationship_and_blocks_attorney -q`
→ **18 passed**.

---

## Downstream flow (documented, not executed)

```
_raw/estate/wake_estate_YYYY-MM-DD.csv
  → load_estate_import_csv / parse_estate_import_rows
  → join property_candidate_id to CURRENT estate master
  → NCSBE last-name index across the seven CURRENT counties
       Wake ncvoter92 phone fill = 0.0000% (999,645 rows) — dead end for phones
       heirs often mail in Mecklenburg / Durham (21–48% fill)
  → match_person_to_voters
       TIER_A exact mailing (county-agnostic) is the cross-county path
       TIER_B requires voter county == property county (fails for a Charlotte
              heir of a Wake parcel)
       TIER_C held; name-only never appends
  → VALID_US phone only; attorney phones never owner/heir
  → HIGH append candidate; callability_status=NOT_AUTHORIZED
  → product overlay, then scripts/project_contacts_to_deedsonar.py
       HIGH only; never acquisition_leads.phone_*; never callable
```

A family label on this CSV is a court-stated identity fact, not
`LEGAL_AUTHORITY`, not a signatory, and not permission to call.

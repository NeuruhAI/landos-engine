# ESTATE PDF FIXTURE COURT — NEURUH-LANDOS-PDF-FIXTURE-20260902

```
MISSION=NEURUH-LANDOS-PDF-FIXTURE-20260902
STAGE=LIVE_PROVEN
STATUS=LIVE_PROVEN
BLOCKED_EXTERNAL=NO
NEXT_CHILD_MISSION_ID=NEURUH-PROPERTY-SCAN-SELLABLE-BIND-20260902
SKIP_TRACE=OFF
TRACERFY=NOT_CALLED
ATTORNEY_PHONES_ACCEPTED=NO
LEGAL_AUTHORITY_WRITTEN=NO
REAL_PII=NO
SCORING_WEIGHTS_EXPOSED=NO
LANDOS_INGEST_WRITTEN=NO
DEEDSONAR_WRITTEN=NO
RECIPE_ENGINE_WRITTEN=NO
PROPERTY_SCAN_WRITTEN=NO
FULFILLMENT_PIN=1779abf265814f5595e1153a531c4a0ee09a0faa
FULFILLMENT_PIN_UNFROZEN=NO
```

## Path proved (synthetic only)

```
AOC-E-201 PDF (tests/fixtures/estate/pdf/synthetic_aoc_e_201.pdf)
  → _raw/estate/wake_estate_2026-09-02.csv
  → ESTATE_IMPORT_COLUMNS validator OVERALL PASS, imported=NO
  → LandOS candidate synthetic-wake-estate-001 (in-memory; no DB write)
  → opportunity-intelligence recipe evaluate (court-safe projection)
  → contact_resolution_state=HEIR_ON_ROLL_NO_ACCEPTED_PHONE
     callability_status=NOT_AUTHORIZED
     phone_present=NO
     has_living_identity=YES
```

Names are the existing synthetic fixture set (ALDERNEY / MAPLEWOOD / RIVERBEND /
STONECROFT). Attorney lines ignored. Phone numbers seen on the form are refused
and do not enter the CSV. Skip-trace stays OFF. `app/opportunity_intelligence.py`
is byte-identical to fulfillment pin `1779abf2`.

## What the recipe returned (no weights)

- next_best_action: ESTABLISH_LEGAL_AUTHORITY (estate import never writes LEGAL_AUTHORITY)
- next_best_source origin: next_best_source.v3
- operator_queue: ESTATE_IDENTITY_FOUND_CONTACT_NEEDED
- system_may_contact: false
- system_may_make_offer: false
- execution_authority: NONE

Court: `pytest tests/test_estate_pdf_fixture_path.py tests/test_estate_pdf_intake.py`
→ **12 passed** (poppler 24.02.0). Related suite with import-readiness + opportunity-intelligence + public-index route → **67 passed**.

## NEXT_CHILD_MISSION_ID

`NEURUH-PROPERTY-SCAN-SELLABLE-BIND-20260902`

Owns `NeuruhAI/neuruh-property-scan` only. Recourt the LandOS PDF fixture as
LIVE_PROVEN. Do not write landos-engine, landos-ingest, deedsonar, or
recipe-engine. Do not unfreeze `1779abf265814f5595e1153a531c4a0ee09a0faa`.
Skip-trace stays OFF. Attorney phones stay refused. ready_for_public_checkout
stays false until this fixture remains LIVE_PROVEN on the named head.

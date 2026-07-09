# PRIORITY: capture assessor land/improvement/year-built into `raw_meta` — the FREE route

**Status:** t5 building the capture · **t2 (engine) HOLDING the wiring until t5 confirms the field is live**
**Source:** the ASSESSOR record — BS&A (Berrien MI) / Beacon·Schneider (St. Joseph IN), the **same source SEV is already pulled from**. NOT the free ArcGIS GIS tier (verified empty) and NOT ReportAll.
**Cost:** $0, ToU-gated. Regrid = paid fallback ONLY for expansion counties with no scrapable assessor field. **ReportAll stays unused.**

---

## Correction history (so the record is straight)

- The free **ArcGIS GIS tier** (`county_rest_attributes`, enrich_owner_chain L273) carries owner/mailing/situs only — **verified empty** of land/improvement/year (St. Clair richest = BLD_ASSESSMENT + homestead flag, both 0.0). t2 correctly found this.
- An earlier draft of this doc then wrongly pointed at **paid ReportAll**. Also wrong route.
- **Correct route (operator ruling 2026-07-08):** the land/improvement-value split + year-built live in the **assessor detail record** (BS&A / Beacon·Schneider) — the same record SEV comes from. Scrapable $0, ToU-gated. That's t5's capture.

## Ownership split

- **t5 (landos-ingest):** add an assessor-detail capture that reads land_value / improvement_value / year_built (+ last-sale + homestead/senior exemption) from the assessor record already read for SEV, ToU-gated, $0. Stage into `landos.owner_leads.raw_meta`. **Confirm to t2 when the field is available.**
- **t2 (landos-engine):** on t5's confirm, wire the `raw_meta → adapter` mapping (coalesce-existing-first, honest-null). **HOLDING until then** — no wiring, no spend.

## The capture contract — exact `raw_meta` keys the adapters already read

t2's three disarmed detectors read these keys verbatim (checked against the adapters — do
not invent keys). t5 stages the assessor fields; t2 maps to these on confirm:

| assessor field | `raw_meta` key (target) | arms | reader |
|---|---|---|---|
| land assessed value | `land_value` | lot_value_arbitrage (numerator) | `lot_value_arbitrage.load_parcel_records` |
| improvement/building value | `improvement_value` | lot_value_arbitrage (→ total = land+improvement) | (t2 wires total) |
| total assessed / SEV *(already present)* | `sev` | lot_value_arbitrage (denominator today) | same |
| year built | `yr_blt` | lot_value_arbitrage **+** aging_owner_luxury | both `load_parcel_records` |
| last sale date | `last_sale_date` | lot_value_arbitrage **+** aging_owner_luxury (→ tenure) | both (t2 computes tenure) |
| senior exemption flag | `senior_exemption` | aging_owner_luxury (65+ proxy) | `aging_owner_luxury.load_parcel_records` |
| homestead/PRE flag | `vintage.exemption_flags` (list, ≥2 vintages) | exemption_drop | `exemption_drop.load_roll_records` |

**Rules (t2 wiring, when GO'd):** coalesce-existing-first (never overwrite a populated value
with empty); honest-null (missing/blank → omit or null, never a coerced `0` — a `0` land value
fabricates a `0.0` land_ratio). Writes to `landos.owner_leads.raw_meta` only, never
`public.acquisition_leads` (single-writer).

## Arm expectation (honest)

- **lot_value_arbitrage, aging_owner_luxury:** arm from a single assessor capture IF the record
  carries land_value + yr_blt (+ sale for tenure) AND the parcel is in a luxury zip.
- **exemption_drop:** the capture seeds vintage #1; it arms only after a **second** capture over
  time (the sleeper diffs consecutive vintages). One run cannot arm it.
- **Proof = re-census.** After t5's field lands and t2 wires, `census_live` for the three adapters
  flips to `covered: true` with real counts. Do not claim an arm without that re-census.

See: `app/discovery/{lot_value_arbitrage,aging_owner_luxury,exemption_drop}.py`,
`app/normalizers/reportall_owner.py` (ReportAll unused), `scripts/enrich_owner_chain.py`.

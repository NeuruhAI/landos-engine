# PRIORITY: capture ReportAll assessor fields into `raw_meta` — arms TWO detectors at once

**Status:** OPEN · highest-leverage next fetch for the enrich pipeline
**Owner:** `scripts/enrich_owner_chain.py` (landos-engine) — ingest-side
**Cost:** $0 (the fields are ALREADY fetched; we discard them)
**Unblocks:** `lot_value_arbitrage` **and** `exemption_drop` — one capture, two armed signals

---

## The one-line ask

The per-parcel attribute payload we already pull during enrichment
(`county_rest_attributes`, populated by the ReportAll / county-REST parcel fetch and
cached at **`scripts/enrich_owner_chain.py` ~L273**) carries the assessor facts two
disarmed detectors need — and we **throw them away** after reading them. Stow them
into `landos.owner_leads.raw_meta` alongside the vintage. That single change arms
both detectors with **no adapter code change**.

Same discard pattern the exemption sleeper already documented: the attributes are
fetched, briefly cached, never persisted.

## Why this is the priority (leverage)

Two shipped-but-disarmed detectors are each blocked on the **same** missing capture:

| Detector | Blocked on (from `raw_meta`) | Arms when captured |
|---|---|---|
| `lot_value_arbitrage` (luxury teardown) | `land_value`, `yr_blt`, `last_sale_date` | land_ratio computable → candidates surface in luxury zips |
| `exemption_drop` (pre-probate death) | `exemption_flags` at ≥2 vintages | occupancy-exemption drop diffs → sleeper fires |

`sev` (total assessed) is **already** in `raw_meta`. The luxury-zip basis is now
seeded (`option_sets field_key='luxury_zip'`, apg-land-crm mig 066) and read live by
`load_luxury_zips()`. So the **only** thing standing between "disarmed everywhere" and
"armed" is persisting the fields below.

## The capture contract (exact keys the adapters already read)

Write these keys into `landos.owner_leads.raw_meta` from `county_rest_attributes`:

```
raw_meta.land_value       ← ReportAll mkt_val_land  (or land_assessed)   # numeric
raw_meta.sev              ← total assessed           (ALREADY captured)   # numeric
raw_meta.yr_blt           ← ReportAll yr_blt                              # 4-digit year
raw_meta.last_sale_date   ← ReportAll sale_date      (YYYY-MM-DD prefix)  # → tenure
```

For the exemption sleeper, additionally stow at **≥2 time points** (the vintage-capture
guard at `enrich_owner_chain.py:283`, where the `vint` dict is built):

```
vintage.exemption_flags   ← homestead/PRE/senior/disabled-vet flags        # list[str]
vintage.sale_recorded     ← bool (a sale between vintages suppresses the drop)
```

### What already consumes these (proof the wiring is done on the read side)

- `app/discovery/lot_value_arbitrage.py::load_parcel_records` SELECTs
  `raw_meta->>'land_value'`, `'sev'`, `'yr_blt'`, `'last_sale_date'` and computes
  `tenure_years` from the sale date. Nothing to change there when the keys land.
- `app/discovery/exemption_drop.py::load_roll_records` reads `raw_meta->'vintage'`;
  `diff_exemptions` diffs `exemption_flags` across consecutive vintages.
- Census gates (`census_live` on both) already report the gap today and will report
  `covered: true` automatically once the keys are present.

## Honesty guardrails (do NOT fake to force an arm)

- **Numbers only, never a coerced 0.** A blank/`TBA` land value must land as absent
  (key omitted or null), not `0` — a `0` land value fabricates a 0.0 land_ratio. The
  adapters' guarded parse drops non-numerics to `None`; keep the ingest side honest too.
- **`exemption_flags` absence ≠ "no exemptions."** Only write the key when the source
  actually reports exemption state; a missing key is not-captured (silence), which is
  what keeps the sleeper from firing a false drop.
- **`land_value` and `sev` must be the same basis** for the ratio to mean anything
  (both assessed, or both market). ReportAll `mkt_val_land`/`mkt_val_tot` are a matched
  pair; MI `sev` is assessed. If mixing, prefer the matched ReportAll pair for the
  land ratio and keep `sev` for the equity model.

## Definition of done

1. Enrich run stows `land_value`, `yr_blt`, `last_sale_date` (and `exemption_flags`
   at ≥2 vintages) into `raw_meta` for the enriched parcels.
2. `lot_value_arbitrage.census_live` reports `covered: true` for ≥1 county and surfaces
   candidates in the seeded luxury zips.
3. `exemption_drop.census_live` reports ≥1 county with `vintages_with_exemption_col ≥ 2`.
4. No adapter code change required — verified by the existing tests (they prove the
   diff/ratio logic against synthetic records already).

See also: `app/discovery/lot_value_arbitrage.py`, `app/discovery/exemption_drop.py`,
apg-land-crm `066_option_sets_luxury_zip.sql`.

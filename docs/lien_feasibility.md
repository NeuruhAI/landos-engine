# Lien data feasibility — Berrien County, MI (STEP 0 probe)

**Date:** 2026-06-04 · **Tenant:** 1840ac90-54bd-4b3d-8baf-227c8d84ab5a
**Question:** Is lien data bulk-fetchable with a **parcel key** (APN) for Berrien County,
so it can become an APN-keyed `lien` signal that stacks with `tax_delinquent`?

## Verdict: NO — not bulk-fetchable with a parcel key. No normalizer built.

Per the STEP 0 rule, we do **not** build a normalizer against unreachable data and do
**not** fabricate lien records. Re-probed live 2026-06-04; confirms earlier discovery.

| Avenue | Status | Evidence / why |
|---|---|---|
| Berrien GIS ArcGIS REST (`gis.berriencountymi.gov/server/rest/services`) | **no lien layer** | Services = BCRD_GIS_Map, Berrien_Locator, Find_my_Commissioner, Municipalities_Basemap, Orthoimagery, Parcels_Future, PASER (+ folders Hosted, Utilities). 0 lien/code/assessment/special/nuisance layers. `Parcels_Future` carries only PIN, house#, Acres. |
| Register of Deeds (`berrien.mi.publicsearch.us`, GovOS/Kofile; `regofdeeds.berriencounty.org` CountyFusion) | **index-search only** | React SPA; `api.publicsearch.us` does not resolve (no public bulk API). Indexed by **name/document**, not parcel. Copies paid ($1/pg + $5 CC). No bulk export. |
| City of Benton Harbor special assessments / mowing / nuisance liens — BS&A (`bsaonline.com/?uid=971`) | **parcel-by-parcel only** | JS app, 302 redirect-loop / bot-blocked to scripted fetch. No bulk export or API. Whole-roll = FOIA to City Treasurer. |
| Vacant-property registration roll | **not public** | Ordinance only *proposed* (Feb 2024), adoption unconfirmed; no published registry. |

## Decision
- **No `lien` normalizer.** A real lien source here would require a FOIA bulk request
  (City special-assessment roll) or a paid records API — both out of scope this pass.
- The correlation layer + curable `environmental_flag` gate are already proven live on a
  real `brownfield + environmental_part201` stack (APN 11-53-1300-0026-00-1); liens were
  the attempt at a **tax-axis** stack specifically, which this data environment can't
  support today.

## Next real shot at a tax-axis stack (one-row path)
When tax-sale.info posts the **2026 main Berrien county catalog** (watch
https://www.tax-sale.info/ for "Berrien → Ready to Browse"), add one row to
`BERRIEN_TAX_CATALOGS` in `scripts/ingest_signals.py` (line ~31) and re-run
`python -m scripts.ingest_signals`:

```python
(<catalog_id>, "Berrien main county — Aug 6 2026 sale", 2026),
```

That ingests the full 2026 tax-foreclosure roll (hundreds of parcels) — the most likely
source of a parcel that is **both** tax-delinquent **and** a brownfield/Part 201 site.

## Out of scope / later
- FOIA bulk request for the BH special-assessment (mowing/nuisance) roll.
- Paid lien/records API (ATTOM/DataTree/etc.) — a separate cost decision.
- SLBA session-gated source (headless/CARTO).

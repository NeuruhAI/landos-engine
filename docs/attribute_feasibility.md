# Parcel-attribute feasibility — Berrien County, MI (probe, no build)

**Date:** 2026-06-04 · **Tenant:** 1840ac90-54bd-4b3d-8baf-227c8d84ab5a
**Question:** Are Berrien parcel ATTRIBUTES (owner, mailing address, zoning, assessed/
taxable value, land-use class, year built) reachable in any **structured/bulk** form
keyed to APN — the data behind 5 blocked capabilities (probate/owner matching, comps,
dev_fit sharpening, deceased-owner signals, disposition)?
**Discipline:** read-only probe. No normalizer, no DB changes, no fabricated data.

## Verdict (BLUF)
**Yes — affordably, but NOT free and NOT county-direct-bulk.** The free/public county
channels (BS&A, Beacon, Berrien GIS, MI open data) are **all geometry-only or
parcel-by-parcel HTML**. The unblock is a **low-cost commercial aggregator —
ReportAll USA** ($300 one-time Berrien export, or low-cost API w/ free 30-day trial),
APN-keyed, schema covers owner+mailing+value+zoning+land-use+year_built+sale_price.
This is the opposite of the lien probe (nothing) and the autonomous-comps probe
(nothing): **owner/attribute data is a deliberate cheap-paid decision, not a dead end.**

## Per-source findings

| Source | Owner/value/zoning, bulk & APN-keyed? | Evidence | Feed mode |
|---|---|---|---|
| **1. BS&A Online** (`uid=797` county, `uid=971` city) | **No** — assessment system of record (owner/SEV/taxable/class/year built) but **parcel-by-parcel HTML, bot-blocked** | live probe → **HTTP 302 redirect-loop** on both uids; no API/export | NOT AT ALL (auto); SEMI-AUTO (VA per-parcel) or **FOIA** the roll |
| **2. Beacon / Schneider** (`AppID=346`) | **No** — HTML per-parcel viewer | live probe → **HTTP 403 Forbidden** | NOT AT ALL (auto); SEMI-AUTO (VA) |
| **3. Berrien GIS ArcGIS REST** | **No (countywide)** — strictly PIN + geometry | `Parcels_Future` (86,243 parcels) fields = PIN, AltPin, **Propaddrnu (house# only), Acres** — **no owner/zoning/value**. The only attribute-rich layer, `BCRD_GIS_Map/2` (ownername, adjsev, propclass, legal), has **just 89 records** (Road-Dept subset, not countywide). | NOT AT ALL for attributes |
| **4a. MI statewide open data** (MGF/DTMB, gis-michigan) | **No** — statewide parcel layer is **internal-only**; open portal = geometry/PIN | DTMB "MGF parcels for internal use only"; no public owner/value/zoning layer found | NOT AT ALL |
| **4b. Berrien County GIS dept order** | partial — sells **"all attributed information" @ $0.50/parcel** (20-parcel min), but per-parcel-priced manual order; SEV inclusion/format unverified | berriencounty.org/410 | SEMI-AUTO (VA/one-time order) |
| **4c. ReportAll USA** ⭐ | **YES** — Berrien 86,327 parcels, **owner 99.5%**; schema: `owner, mail_*, mkt_val_land/bldg/tot, land_use_class, zoning, year_built, sale_price, trans_date, acreage` | reportallusa.com/store/Michigan/26021 ; api-standard-data-dictionary | **AUTONOMOUS** (API or $300 CSV/SHP/GeoPackage export; free 30-day trial) |
| **4d. Regrid/LandGrid** (already known) | partial — owner + land value + land-use (MI); **zoning/year-built/assessed less certain**; mailing = paid add-on | regrid.com/michigan-parcel-data | AUTONOMOUS (thinner on value/zoning) |
| **4e. CoreLogic / First American / LightBox** | yes but **enterprise/paid**, contract-only | — | overkill unless multi-county |

## Caveats (verify before committing)
- **ReportAll `mkt_val_*` ≠ Michigan SEV/taxable.** It's market value (SEV ≈ 50% of true cash value). For *true* assessor SEV/taxable, layer in a **one-time Berrien County order or FOIA** of the digital assessment roll (VA/one-time).
- **Schema presence ≠ Berrien population.** The national API dictionary lists zoning/value/year_built, but the Berrien store listing didn't enumerate them. **Confirm `zoning`, `mkt_val_*`, `year_built`, `sale_price` actually populate for Berrien (incl. vacant land) during the free trial** before paying.
- ReportAll also carries `sale_price`/`trans_date` → could partially serve **comps** too (subject to the same land-population check from the comps probe).

## What this unblocks (if ReportAll verifies for Berrien)
| Capability | Field(s) needed | ReportAll covers? |
|---|---|---|
| dev_fit sharpening (replace 0.5 proxy) | zoning, land_use_class, year_built, value | ✅ (verify pop.) |
| probate / owner matching | owner, mail_name, mail_address | ✅ (99.5% owner) |
| deceased-owner signals | owner + mailing (for cross-ref) | ✅ owner side; death data still external |
| disposition / builder-buyer match | owner, land_use_class, value | ✅ |
| comps (partial) | sale_price, trans_date, acreage | ⚠️ verify land coverage |

## Recommendation (cheapest path)
1. **ReportAll free 30-day trial** → query ~10–20 known Berrien parcels incl. vacant land → verify owner/zoning/value/year_built/sale_price populate. **$0.**
2. If it verifies → **$300 one-time Berrien CSV export** (or low-cost API). One-time attribute-enrichment keyed by APN (the existing `address_to_apn` resolver already yields APNs to join on). This is the single highest-leverage unblock — it feeds dev_fit, owner/probate, disposition, and partially comps from one source.
3. **For true SEV/taxable only** (if market-value approximation is insufficient) → one-time county order ($0.50/parcel) or FOIA the assessment roll (VA).
4. **Skip** enterprise (CoreLogic/First American/LightBox) unless going multi-county.
5. **Do not auto-build an enrichment normalizer** until the trial proves Berrien population.

## Strategic note
This is the **inverse** of liens (no source) and autonomous comps (no source): owner/
attribute data **is** affordably reachable (~$300 or a cheap API). It's a deliberate
paid decision — not free open data, not a dead end. The free public channels
(BS&A/Beacon/GIS/MI-open-data) are confirmed **not bulk** for these fields.

*Unverified flags: ReportAll exact API pricing (sales-quoted) + Berrien population of zoning/value/year_built/sale_price; whether the county $0.50/parcel file includes SEV/taxable; CoreLogic/First American/LightBox Berrien specifics.*

## Out of scope (decide after report)
Building any enrichment normalizer; comps; outreach; paid subscription (your call).

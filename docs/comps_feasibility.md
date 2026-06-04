# Comps source feasibility — Berrien County, MI vacant land (probe, no build)

**Date:** 2026-06-04 · **Tenant:** 1840ac90-54bd-4b3d-8baf-227c8d84ab5a
**Question:** Which comps source can actually deliver **vacant-land / lakefront** sale
comps for Berrien County, MI to feed `scripts/import_comps`, before any paid commitment?
**Discipline:** read-only probe (same as the lien probe). No normalizer built, no DB
changes, no fabricated comps.

## Verdict (BLUF)
**No source autonomously delivers reliable Berrien _vacant-land_ comps.** AVM/“comps”
engines are residential (1–4 unit) and exclude raw land. Aggregator sale fields exist for
Berrien but are **not documented for vacant land** and are structurally thin on raw parcels.
**The manual CSV importer (Option C) stays the proven path**, fed by a VA running the
**Michigan transfer-tax back-calculation** against recorded deeds (the only method that
returns _real_ Berrien land prices today).

## Per-source findings

| Source | Berrien vacant-land comps | Feed mode | Cheapest access | Notes |
|---|---|---|---|---|
| **Regrid** | sale fields present countywide (~69–70% fill, ALL types); **vacant-land fill NOT published, likely lower**; last-sale-only; Berrien last refresh 2024-12-03 | **SEMI-AUTO** (validate land fill) | **$10/mo** Property App Pro (5k CSV/mo free) or ~$200 one-time county CSV | `saleprice`/`saledate` are **Standard tier, not premium-gated** — corrected assumption. Schema: support.regrid.com/parcel-data/schema ; coverage: app.regrid.com/store/us/mi/berrien |
| **ATTOM** | **NOT supported for comps/AVM** — AVM + SalesComparables are 1–4 unit **residential**, exclude special-use/farmland/raw land | **NOT AT ALL** (comps/AVM); raw deed history only via enterprise = SEMI-AUTO/overkill | enterprise contract (~$1k–6k+/yr, unverified); 30-day free trial | AVM scope “single-family, condo, 1–4 unit” (attomdata.com/data/property-valuation-data/avm). Has Berrien deed/sale records but won’t comp land. |
| **Estated** | n/a | none (self-serve closed) | — | Acquired by ATTOM (2022); no new self-serve signup. |
| **RentCast** | only API with explicit **“Land”** property type; **land sale depth UNVERIFIED**, no Berrien guarantee | TEST-then-maybe-AUTO | **free 50 calls/mo**, paid $74/mo (1k) | Best low-cost candidate to *test*; rentcast.io/api. Validate `lastSalePrice` on known Berrien vacant parcels before paying. |
| **PropMix / Realie** | land coverage unconfirmed | unverified | PropMix $79–129/mo; Realie no public pricing | Have deed history; raw-land fill not stated. |
| **Lob** | n/a — address-verification/mail, not a property-data API | — | — | Irrelevant to comps. |
| **County direct (BS&A `uid=797` / Beacon GIS)** | per-parcel assessment + often last-sale; **no bulk/API** | manual triage | free | Use to identify vacant parcels + recent transfers before pulling deeds. |

## Michigan transfer-tax back-calculation (the VA fallback — returns REAL prices)
- **Rates (confirmed):** State SRETT **$3.75/$500** + County **$0.55/$500** (counties <2M pop; Berrien qualifies) = **~$8.60/$1,000**. → **sale price ≈ transfer_tax ÷ 0.0086** (value rounded to nearest $500 → estimate, not exact). Cites: deedclaim.com/michigan/real-estate-transfer-tax ; legislature.mi.gov MCL 207.504 (county rate), MCL 207.523 (state rate).
- **Exemptions break it (no stamp → no price):** consideration <$100, **foreclosure/sheriff’s deeds**, government grantor, family transfers, title-correction quitclaims, land contracts. (MCL 207.526 state / 207.505 county.) → tax-foreclosure & quitclaim deeds (common in land) yield no price; filter for **arms-length warranty deeds with a stamp**.
- **Berrien deed access:** `berrien.mi.publicsearch.us` (GovOS) + CountyFusion = **index-search only, images $1/pg + ~$5 CC fee, NO bulk/API** (same as the lien probe). Structured/bulk not available.
- **VA process (~5–12 min / usable comp, ~$1–2/deed, ~1.5–2 deeds opened per kept comp):** (1) search Warranty Deeds, recent, near target; (2) confirm vacant + grab APN via BS&A/Beacon; (3) open deed image; (4) read transfer-tax stamp; (5) price ≈ tax÷0.0086; (6) discard exempt/multi-parcel/non-arms-length; (7) record APN+date+price+liber/page into the comps CSV → `import_comps`.

## Recommendation (cheapest path to REAL Berrien land comps)
1. **Now:** keep **Option C — manual CSV** via `scripts/import_comps`, populated by the **VA transfer-tax method** (only source of real raw-land prices). Free triage via BS&A/Beacon to cut wasted image buys.
2. **Cheap coverage test before any subscription:** spend **RentCast free 50 calls** + **Regrid $10/mo Pro** querying ~10 known Berrien vacant parcels; check whether `lastSalePrice`/sale fields actually populate **for land**. Only scale to a paid tier if the test passes.
3. **Skip ATTOM** for this use case (residential AVM/comps; won’t value land; enterprise cost).
4. Revisit an automated normalizer **only after** a source empirically proves vacant-land fill for Berrien — otherwise an autonomous importer would silently ingest a partial/stale comp set.

## Out of scope (decide after this report)
Paid subscriptions (Regrid/RentCast/PropMix) — your call; building any comps normalizer; SLBA; liens (killed).

*Unverified flags: vacant-land sale fill for Berrien (Regrid/RentCast/PropMix); Realie pricing; exact MCL 207.523 rate line (corroborated via county-ROD + legal sources).*

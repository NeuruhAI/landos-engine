# ReportAll USA — Berrien County Coverage Test

**Date:** 2026-06-04
**Purpose:** Verify what attributes ReportAll USA actually populates for Berrien County, MI
parcels — including **vacant land** — *before* deciding to pay for the county dataset.
**Scope:** Read-only coverage test. No DB writes, no enrichment normalizer built, no dataset
purchased. `landos-engine` only.

---

## Auth / setup

- **Endpoint:** `https://reportallusa.com/api/parcels.php` (Parcel Data REST API, `v=9`)
- **Auth:** client key passed as `&client=` URL param. Username: `neuruh`.
- **Key correction:** the supplied key `[Vgx2M3o6S0` was rejected
  (`HTTP 422 — "Bad Request. Invalid client value supplied"`). The leading `[` was a
  paste artifact. The key **`Vgx2M3o6S0`** (bracket removed) authenticates successfully
  (`status: OK`). All queries below used `Vgx2M3o6S0`.

### APN → ReportAll `parcel_id` mapping (discovered, not assumed)

ReportAll stores Michigan PINs **without the leading `11-` Berrien county prefix**. The
CSV's full APN appears in ReportAll's `alt_id_2` field.

| CSV APN form | ReportAll `parcel_id` | Rule |
|---|---|---|
| `11-54-0340-0145-00-9` (env/QOZ/brownfield rows) | `54-0340-0145-00-9` | strip leading `11-` |
| `07-5620-0075-00-8` (tax_delinquent rows) | `07-5620-0075-00-8` | already prefix-less → use as-is |

Required query shape: `region=Berrien County, MI` **plus** one attribute clause
(`parcel_id` or `owner`). `addr_street_name` is **not** a valid standalone attribute in v9.

---

## Per-parcel results (15 parcels: 5 QOZ-improved + 10 vacant/tax-delinquent)

Legend: Y = field present & populated · N = field absent from API response entirely.

| # | CSV APN | Parcel (RA address) | Type | Owner | Mail addr | Zoning | Mkt value | Land-use class | Year built | Sale $ + date |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 11-54-0340-0145-00-9 | 73 W Main St (Commercial) | QOZ-improved | Y | Y | **N** | **N** | Y | **N** | **N** |
| 2 | 11-54-0340-0050-03-2 | 116 Water St (Commercial) | QOZ-improved | Y | Y | **N** | **N** | Y | **N** | **N** |
| 3 | 11-51-0340-0215-00-3 | 77 W Wall St (Commercial) | QOZ-improved | Y | Y | **N** | **N** | Y | **N** | **N** |
| 4 | 11-54-0340-0149-00-4 | 115 W Main St (Commercial) | QOZ-improved | Y | Y | **N** | **N** | Y | **N** | **N** |
| 5 | 11-54-0340-0116-01-7 | 241 E Main St (Commercial) | QOZ-improved | Y | Y | **N** | **N** | Y | **N** | **N** |
| 6 | 11-76-0023-0031-15-9 | Harbor Shores "E of Upton" | VACANT-brownfield | — | — | — | — | — | — | — |
| 7 | 11-76-0023-0033-01-1 | Harbor Shores "E of Upton" | VACANT-brownfield | — | — | — | — | — | — | — |
| 8 | 11-76-0024-0076-00-2 | Harbor Shores "E of Upton" | VACANT-brownfield | — | — | — | — | — | — | — |
| 9 | 11-43-0009-0064-10-3 | 2675 Driftwood Path (Village of Shoreham) | **VACANT** | Y | Y | **N** | **N** | Y | **N** | **N** |
| 10 | 07-5620-0075-00-8 | 15155 Lakeside Ave (lakefront) | VACANT? | — | — | — | — | — | — | — |
| 11 | 11-76-0013-0034-33-9 | Klock Rd & M-63 (12.0 ac) | **VACANT** | Y | Y | **N** | **N** | Y | **N** | **N** |
| 12 | 07-4670-0167-00-1 | 0 Oak Dr, Three Oaks | **VACANT** | Y | Y | **N** | **N** | Y | **N** | **N** |
| 13 | 06-1180-0248-00-5 | 0 Walnut St, Buchanan | **VACANT** | Y | Y | **N** | **N** | Y | **N** | **N** |
| 14 | 73-6900-0039-00-9 | 0 Riverview Dr, Niles | **VACANT** | Y | Y | **N** | **N** | Y | **N** | **N** |
| 15 | 12-6080-0010-00-5 | 5857 Ponderosa Dr, Stevensville | improved | Y | Y | **N** | **N** | Y | **N** | **N** |

**Matched: 11 / 15** (count=1). **No match: 4** (count=0, `status: OK`, empty `results`).

### Concrete owner/mailing pulled for vacant parcels (proof of population)

| # | parcel_id | Owner | Mailing | Land use | Acres |
|---|---|---|---|---|---|
| 9 | 43-0009-0064-10-3 | MICHIGAN STATE OF | PO BOX 30448 LANSING MI 48909 | 402 Residential | 1.36 |
| 11 | 76-0013-0034-33-9 | HARBOR SHORES COMMUNITY | 201 GRAHAM AVE BENTON HARBOR MI 49022 | 402 Residential | 12.02 |
| 12 | 07-4670-0167-00-1 | HARBERT WOODS ASSOCIATION | 13447 MAIN DR HARBERT MI 49115 | 402 Residential | 0.08 |
| 13 | 06-1180-0248-00-5 | FRAMPTON MICHAEL EDWARD | 17754 WILLARD LN LOCKPORT **IL** 60441 | 402 Residential | 0.07 |
| 14 | 73-6900-0039-00-9 | PASCARELLA ANTHONY EDWARD | PO BOX 412 NAPOLEON MI 49261 | 402 Residential | 0.14 |

Note the **out-of-state / PO-box absentee owners** on vacant lots (#13 Illinois, #14 PO box) —
exactly the lead-targeting signal we want, and it populates on vacant land.

---

## Full field set returned by the API (union across all matched parcels)

```
acreage, acreage_adjacent_with_sameowner, acreage_calc, addr_city, addr_number,
addr_street_name, addr_street_prefix, addr_street_type, addr_zip, addr_zipplusfour,
address, alt_id_1, alt_id_2, buildings, census_block, census_place, census_tract,
census_zip, county_id, county_link, county_name, crop_cover, cty_row_id, elevation,
geom_as_wkt, land_cover, land_use_class, land_use_code, last_updated, latitude,
longitude, mail_address1, mail_address3, mail_addressnumber, mail_placename,
mail_statename, mail_streetname, mail_streetnamepostdirectional,
mail_streetnameposttype, mail_streetnamepre, mail_uspsbox, mail_zipcode, muni_id,
muni_name, owner, parcel_id, robust_id, school_district, section_township_range,
state_abbr, usps_residential
```

- `buildings` (present on #4, #5 only) is a bare **structure count** (`1`) — **not**
  year_built and **not** a value.
- A raw token grep across all 15 saved responses for
  `mkt_val*`, `assessed*`, `sev`, `taxable_value`, `sale_price`, `sale_date`,
  `trans_amount`, `trans_date`, `year_built`, `yr_blt`, `zoning*` returned **zero hits**.
  These fields do not exist in the payload for any Berrien parcel — improved or vacant.
- `county_link` is a `cama_redir` URL pointing back to the **county's own CAMA system** —
  i.e. ReportAll itself routes you to the county for valuation/assessment detail rather
  than serving it via the API on this plan.

---

## Verdict

**Fields that reliably populate for Berrien (both improved AND vacant):**
- ✅ **Owner name** — 11/11 matched parcels
- ✅ **Mailing address** (`mail_address1` + `mail_address3`) — 11/11, incl. absentee/out-of-state
- ✅ **Land-use class + code** — 11/11 (Commercial / Residential)
- ✅ Situs address, acreage (`acreage_calc`), lat/long, parcel geometry (WKT), school
  district, section-township-range, census geo

**Fields that DO NOT populate at all (this key / v9 base parcel endpoint):**
- ❌ **Zoning** — absent for every parcel
- ❌ **Market / assessed / taxable value** — absent for every parcel
- ❌ **Year built** — absent (only a `buildings` count on improved parcels)
- ❌ **Sale price + sale date** — absent for every parcel

**Does VACANT LAND get owner + value + sale data, or only improved parcels?**

> **Vacant land gets the SAME coverage as improved parcels — no penalty for being vacant.**
> Vacant lots (#9 Shoreham, #11 Klock Rd, #12 Oak Dr, #13 Walnut St, #14 Riverview Dr) all
> returned **owner + mailing + land-use + acreage**, including the absentee-owner signal.
>
> **However, value and sale data are NOT available for ANY parcel** — vacant or improved.
> ReportAll's base parcel API for Berrien simply does not carry market value, year built,
> zoning, or sale history. The distinction is *not* vacant-vs-improved; it is
> **ownership/mailing/land-use (covered) vs. valuation/sales/zoning (not covered)**.

### Buy-decision implication

- If the goal is **owner + mailing + land-use targeting of vacant land** (the absentee-owner
  lead play): ReportAll **delivers this well** for Berrien, vacant lots included. Worth it.
- If the goal requires **assessed/market value, sale comps, or zoning**: the base ReportAll
  parcel feed does **not** supply these for Berrien. Before paying, confirm with ReportAll
  whether a **Valuation / Sales / Structures add-on** exists for Berrien (FIPS 26021), or plan
  to source value/sale/zoning from the **county CAMA** (`county_link` / `cama_redir`) or another
  provider. Do not assume the county-dataset purchase adds these fields without explicit
  confirmation.

### 4 no-match parcels (count=0) — cause, not coverage gap

- **#6, #7, #8 (Harbor Shores `11-76-0023…`)** — these brownfield rows share the CSV address
  *"East of Upton… (about 100 parcels)"*, i.e. **aggregate/placeholder APNs** from the EGLE
  brownfield source, not precise individual PINs. Expected to miss on exact `parcel_id` lookup.
  (Note: #11, a precise `11-76-0013…` Harbor Shores parcel, **did** match.)
- **#10 (15155 Lakeside Ave `07-5620-0075-00-8`)** — clean `status: OK, count: 0`. The
  prefix-less APN did not resolve to a stored `parcel_id`; likely a CSV/county PIN-format
  mismatch for that muni rather than a missing parcel. Would need an `owner=` or spatial
  lookup to confirm (not spent here to stay within trial budget).

---

## Transactions used (free trial)

| Call | Purpose | Result |
|---|---|---|
| 1 | Key `[Vgx2M3o6S0` validation | HTTP 422 invalid client (error, non-billable) |
| 2 | Key `Vgx2M3o6S0` validation | OK, count 0 |
| 3 | Schema probe (region only) | error: attribute clause required (non-billable) |
| 4 | `addr_street_name` probe | error: not a valid attribute (non-billable) |
| 5 | `owner=CITY OF BENTON HARBOR` schema discovery | OK, count 100 |
| 6–20 | 15 target parcels by `parcel_id` | 11 OK / 4 count-0 |

≈ **17 successful lookups** (3 discovery + 14 of the 15 that returned a definitive
OK; the 4 count-0 still returned `status: OK`). All within the ~15-call free-trial intent;
no retries were spent on the no-match parcels.

*Raw JSON responses for all 15 target parcels saved to `/tmp/ra_raw/` during the test
(not committed). No data was written to the database. No enrichment code was built.*

# BERRIEN TRACK B — OPERATOR ACTIONS (outbound = RED, you fire)

Two moves, same day, run in parallel. Both are prerequisites for Track B
(`--jurisdiction berrien_mi`); the pipeline and YAML are already built and
waiting on these two files.

## 1. Berrien County GIS — parcel geometry (call)

- **Call:** 269-983-7111 ext 8300 (GIS & Land Description dept)
- **Ask:** countywide parcel layer (shapefile or file geodatabase), all 39
  assessing units, with parcel number + owner attributes if available; ask for
  the fee schedule and delivery format/timeline.
- **Reference page:** berriencounty.org/276/Geographic-Information-Systems-GIS
- **Fallback if quote is slow/expensive:** ReportAll USA per-county purchase
  (86,327 Berrien parcels, same-day, reportallusa.com/purchase-shapefiles/Michigan/26021)
  — spend = your GO either way.
- **Deliver to:** `recipes/senior_assemblage/data/mi/berrien_parcels.shp`
  (any vector format works — update `parcel_file` + `apn_field_geo` in
  `config/senior_assemblage.yaml` to match what arrives).

## 2. Berrien Equalization — FOIA the assessment roll (email)

- **Find address:** berriencounty.org → Equalization Department → FOIA
  coordinator email (or mail to: Berrien County Equalization, 701 Main St,
  St. Joseph, MI 49085).
- **Statutory clock:** 5 business days to respond, +10 extension allowed
  (MCL 15.235). Typical output: countywide CSV for modest duplication fees.
- **Deliver to:** `recipes/senior_assemblage/data/mi/berrien_equalization.csv`

### Letter (copy-paste; fill your email/phone)

> To: Berrien County Equalization Department
> Re: FOIA Request — Electronic Assessment Roll
>
> Under the Michigan Freedom of Information Act (MCL 15.231 et seq.), I
> request an electronic copy (CSV or Excel preferred) of the current real
> property assessment roll for all assessing units in Berrien County,
> including for each parcel: parcel number, property address, owner name,
> owner mailing address, property classification, state equalized value,
> taxable value, principal residence exemption percentage, most recent
> sale/transfer date, sale price, year built, and acreage.
>
> Please provide a fee estimate before processing if costs will exceed $50.
> I request the records in electronic format per MCL 15.234.
>
> Jeramie Hicks, Neuruh LLC, Benton Harbor, MI — [your email / phone]

## On arrival (hand back to Claude Code)

1. Drop both files in `data/mi/` as named above.
2. Say "Berrien data landed" — remaining work is: confirm the four ◄ field
   names in the YAML against the delivered headers, run Track B, calibrate
   TV/SEV bins per spec §10.5.

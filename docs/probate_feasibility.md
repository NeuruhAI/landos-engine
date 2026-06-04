# Probate / deceased-owner data feasibility — St. Joseph IN · Berrien MI · Elkhart IN

**Date:** 2026-06-04 · **Type:** READ-ONLY probe (same discipline as
`lien_feasibility.md` / `comps_feasibility.md`). Nothing built, no tables created, no
data fabricated. Findings only.

**Question:** Is probate / deceased-owner data **reachable** AND **matchable to parcel
owners** in `owner_leads` for the three target counties — enough to justify building a
motivated/probate axis, or should we pivot (manual/VA, or buy a paid list)?

**Why it matters:** probate is the highest-conviction *motivated* signal (a death forces
disposition), but the data is scattered and — critically — most death sources carry **no
property link**, so the whole question hinges on *matchability*, not mere availability.

---

## Bottom line (verdict)

- **Indiana (St. Joseph + Elkhart)** county probate is the **best buildable** source: both
  counties are on the statewide Odyssey/MyCase portal, probate/estate cases are public,
  and **Indiana law forces real-property into the estate inventory** — giving a
  *property-keyed* match, not a name-only guess.
- **Berrien MI** probate is **PARTIAL/weak to build**: no API, name-search only, and MI
  inventories are filed only for *supervised* estates — most are informal, so the property
  link is usually absent.
- **Obituaries** = name-only (no property) → low-confidence enrichment, not a primary join.
- **Public SSDI/DMF** = **DEAD-END** for fresh leads (3-year federal embargo) and has no
  property link ever.
- **Paid vendor (USLeadList)** covers all three counties turnkey with the death→property→heir
  match already solved — the "buy the moat" option.

---

## Source table — reachable / partial / dead-end

### 1. County probate court records (the gold — estate filings name the deceased and, in IN, list the real property)

| County | Status | Evidence / access |
|---|---|---|
| **St. Joseph IN** | **REACHABLE (manual) · PARTIAL (programmatic)** | All St. Joseph courts on statewide **Odyssey**, public via **MyCase** (`mycase.in.gov`, `public.courts.in.gov/mycase`) — Circuit Court Go-Live 2/4/2013; Superior Courts 2009–2013. Free name/case search; **non-confidential probate/estate cases are public** (Circuit Court handles probate). Estate **inventory is statutorily required** to list real property by plat/survey description + FMV + homestead (Ind. Code 29-1-12) → **property link exists**. Pleadings must carry party name + address. *Programmatic:* MyCase UI is reCAPTCHA + ToU-restricted (no scraping); structured access is the **IOCS Bulk Data** path (application + Data Use Agreement). |
| **Elkhart IN** | **REACHABLE (manual) · PARTIAL (programmatic)** | All 7 Elkhart courts on Odyssey (Circuit + 6 Superior Go-Live 9/4/2012). Same MyCase public search; same IC 29-1-12 inventory rule; same IOCS bulk path. |
| **Berrien MI** | **PARTIAL** | Free civil/family docket search at `webinquiry.courts.michigan.gov/WIGroups/Agree?Name=BERRIEN`; probate file search by name/estate#/case# via county Probate Court page (`berriencounty.org/727`). **No statewide API**; MiFILE is e-filing only. **Inventory (form PC577) is required only for *supervised* estates / conservatorships** — Michigan estates are predominantly *informal/unsupervised*, so a court-filed inventory (and thus the property description) is **frequently absent**. Manual, per-case. |

*Indiana bulk-data economics (IOCS, Office of Judicial Administration):* one-time request
$88.00/hr (allowed once/year); or **monthly file-drop $0.01 per closed case / $0.10 per
open or new case**; "messaging" method updates ~every 15 min. Requires a signed Data Use
Agreement. This is the sanctioned structured feed covering both Indiana counties.

### 2. Obituary sources

| Source | Status | Evidence / access |
|---|---|---|
| **Legacy.com** (all 3 markets) | **PARTIAL — name-only, no property** | Covers **South Bend Tribune** (St. Joseph IN), **The Elkhart Truth** (Elkhart IN), **The Herald-Palladium** (Berrien/St. Joseph MI). Browse + search pages exist; the search endpoint takes structured params (`affiliateId`, `lastname`, `daterange`, e.g. `legacy.com/obituaries/search?daterange=Last30Days&affiliateId=…&lastname=…`). Fields: name, age, city, dates, surviving family. **No official API; ToS prohibits scraping + bot protection.** **No property link → name-based matching only.** |
| Local funeral homes | PARTIAL | Many syndicate into Legacy.com; individual home sites are unstructured HTML, no feeds. |

### 3. Death records (SSDI / Death Master File)

| Source | Status | Evidence / access |
|---|---|---|
| **Public/Open-Access DMF (NTIS)** | **DEAD-END for fresh leads** | 2013 law imposed a **3-calendar-year embargo**: deaths within the last 3 years are withheld from the Open Access DMF and available only via the **Limited Access DMF certification program**. So the *recent* deaths that matter for probate timing are not publicly available. **No property link ever.** |
| Historical SSDI (≤~2014) | Available but useless here | On genealogy platforms (Ancestry, FamilySearch, etc.) — far too stale for live leads, still no property link. |
| MI / IN state death indexes | PARTIAL/restricted | State vital-records death indexes are not openly bulk-downloadable for recent deaths; access is restricted. No property link. |

### 4. Paid probate-list vendors (note only — did NOT sign up)

| Vendor | Status | Evidence / access |
|---|---|---|
| **USLeadList** | **REACHABLE (paid)** | Claims coverage of "virtually every county" incl. all three targets; ~**2,289 IN** + **3,217 MI** pre-probate records/month. Vendor-stated pricing **~$99–$299 per county / month** (6/12-mo commitments cheaper; bulk historical as low as ~$0.35/lead, min orders). Delivers **heir/family phone + email**, and limits investors per county. *Pre-probate = death already matched to the owned property + heirs* — the vendor does the join. |
| ProbateData.com (and similar) | REACHABLE (paid) | Multi-county aggregators in the same model; alternative quotes if pursuing the buy path. |

---

## Matchability assessment (this decides everything)

**Join keys we hold in `owner_leads`:** `owner_name`, `owner_mailing_address/city/state/zip`,
`parcel_apn`, `situs_address`, `county_fips`.

**The crux:** a death source is only useful if a deceased person can be tied to a parcel
owner with confidence. Two regimes:
- **Property-linked sources** (court estate inventory, paid pre-probate list) match on
  **owner_name AND the real-property description/APN** → two keys → defeats the common-name
  problem → high confidence.
- **Name-only sources** (obituary, SSDI) match on `owner_name` (+ maybe city) → one key →
  the **common-name problem is severe** ("MARY JOHNSON", "JAMES SMITH") → high false-positive
  rate without an address/property corroborator → low confidence, manual review required.

**Timing wrinkle:** the high-value window is **pre-probate / freshly deceased**, when title
is still in the decedent's name — then the death record's name = `owner_leads.owner_name`
directly. Once probate transfers title, the owner becomes "ESTATE OF …" / an heir (our
absentee layer already flags `TRUST`/entity owners), and the direct name match degrades.

### Ranking by matchability (not just availability)

1. **USLeadList (paid)** — *highest*. Vendor pre-matches deceased → owned parcel → heirs with
   contact info, across all three counties. Turnkey; common-name problem solved on their side.
   Cost is the only tradeoff.
2. **Indiana county probate — St. Joseph + Elkhart** — *highest buildable*. IC 29-1-12 forces
   the real-property legal description into the public estate inventory → match on **decedent
   name + property description** (resolvable to APN via our parcel data) → two-key, high
   confidence. Free manual on MyCase; structured/scalable via IOCS bulk DUA.
3. **Berrien MI probate** — *medium/low*. Informal estates dominate → property usually not in a
   court-filed inventory → frequently **name-only**, manual, no API.
4. **Obituaries (all 3 counties)** — *low*. Name + city only, no property → high false-positive
   join to parcels. Useful as **enrichment/corroboration** (DOD, heirs, city) layered on a
   property-linked match, not as a primary key.
5. **SSDI/DMF** — *dead-end*. Fresh deaths embargoed; name + last-ZIP only; no property; stale.

---

## Recommendation

**Best single first build (free/owned path): Indiana county probate court records via MyCase,
starting with ONE Indiana county (St. Joseph or Elkhart).** Rationale: Indiana statute puts
the real-property description into the *public* estate inventory, which is exactly the
property key that makes a death matchable to a parcel with confidence — the thing every other
free source lacks. Both counties are already on the statewide portal.

- **Start manual / VA-assisted** to validate the real-world match rate against `owner_leads`
  (pull recent estate filings on MyCase → decedent name + property description → match to APN).
  Do **not** scrape MyCase (reCAPTCHA + ToU).
- **Scale via the IOCS Bulk Data DUA** (monthly file-drop, $0.01–$0.10/case) once the match
  rate justifies it — this is the sanctioned automation path for both Indiana counties.
- **Berrien MI is the weakest to build** (informal estates, no API, supervised-only
  inventories). For Michigan coverage, prefer the **paid vendor** or treat **obituary + name**
  as low-confidence enrichment pending manual review.

**Buy-vs-build:** if speed-to-moat across *all three* counties matters — especially Berrien,
where building is hard — **USLeadList (~$99–299/county/mo, match pre-solved)** is the faster
route. If owning the pipeline and Indiana-first economics matter, build off MyCase/IOCS for
the two Indiana counties and buy/skip Michigan.

**Honest framing:** a v1 is **initially a manual/VA process** to prove the match rate.
Full automation requires *either* the IOCS bulk DUA (Indiana, structured monthly feed) *or* a
paid vendor. Public scraping of MyCase and Legacy.com is **not** a sanctioned automation path.

---

## What a v1 probate signal would actually require to wire in

**Accuracy note:** this repo has **no motivated framework today** — there is no
`app/motivated/` package and no `motivated_*` columns on `owner_leads` (the only "motivated"
references are honesty comments, e.g. *absentee ≠ motivated*). So a v1 does **not** wire into a
motivated axis — it wires into the mechanism that already exists.

**The existing integration point is the distress signal stack.** The distressed axis reads
`landos.signals` by `parcel_apn`, and `app/score_absentee.py`'s `DISTRESS_SIGNALS` set
**already includes `"probate"`**. So a parcel-keyed probate signal would stack automatically
through the existing `signal_overlap` fusion + correlation boost — **no scorer-math change
needed** for a lower-lift v1, and it would compound with the absentee axis (a deceased,
out-of-state owner is a top-tier lead).

**Required, in order:**
1. **A property-keyed probate source** — a death tied to an APN. Indiana estate inventory
   (property description → APN via assessor/our parcel data) or the paid pre-probate list
   provide this directly. Obituary/SSDI alone do **not** (name-only) and would additionally
   need a name→owner resolver with address corroboration + a manual-review threshold.
2. **A `ProbateNormalizer`** under `app/normalizers/` following the `base.py` contract
   (fetch → normalize → upsert + audit-log), emitting rows into `landos.signals` shaped like
   `{parcel_apn, signal_type:"probate", signal_strength, signal_date, raw_meta:{decedent,
   case_no, filing_date, heirs}}` — the same pattern as `tax_sale_info.py`.
3. **A match step** producing the `parcel_apn`: decedent name + property legal description →
   APN (join against our parcel/owner data), with a confidence score and a manual-review queue
   for name-only candidates (common-name guard).
4. **(Optional, higher-lift)** a dedicated `motivated_score` column/axis if probate must be
   scored separately from distress; otherwise it rides the existing distress overlap (lowest
   lift, recommended for v1).

**Net:** the engine is already *ready to consume* a probate signal — the entire build is
**getting parcel-keyed probate rows**, which is precisely the matchability problem above. That
is why the recommendation is to start with the one source (Indiana county inventory) that
hands us the property key, validated manually before any automation spend.

---

## Sources
- [Indiana MyCase / Odyssey public case search](https://mycase.in.gov/) · [public.courts.in.gov/mycase](https://public.courts.in.gov/mycase/)
- [Which Indiana courts use Odyssey/MyCase (St. Joseph + Elkhart confirmed)](https://www.in.gov/courts/help/odyssey-courts/)
- [Indiana Judicial Branch — Searching MyCase (public vs confidential)](https://www.in.gov/courts/help/mycase/)
- [Indiana IOCS Bulk Data Applications + fees](https://www.in.gov/courts/iocs/statistics/bulk-data/)
- [Ind. Code 29-1-12 — Inventory (real property in estate inventory)](https://law.justia.com/codes/indiana/2010/title29/ar1/ch12.html)
- [St. Joseph County IN Probate Court](https://www.sjcindiana.gov/1988/Probate-Court) · [Elkhart County IN courts](https://www.in.gov/courts/local/elkhart-county/)
- [Berrien County MI Probate Court](https://www.berriencounty.org/727/Probate-Court) · [MI WebInquiry (Berrien)](https://webinquiry.courts.michigan.gov/WIGroups/Agree?Name=BERRIEN) · [MI inventory form PC577](https://www.courts.michigan.gov/siteassets/forms/scao-approved/pc577.pdf) · [Berrien estate inventory page](https://www.berriencounty.org/1358/Inventory)
- [Legacy.com — South Bend Tribune](https://www.legacy.com/us/obituaries/southbendtribune/browse) · [Elkhart Truth](https://www.legacy.com/us/obituaries/elkharttruth/browse) · [Herald-Palladium](https://www.legacy.com/us/obituaries/heraldpalladium/browse)
- [SSDI/DMF 3-year embargo + access](https://legalclarity.org/what-happened-to-the-social-security-death-index/) · [SSA Death Information / DMF request](https://www.ssa.gov/dataexchange/request_dmf.html)
- [USLeadList — Indiana](https://usleadlist.com/probate/IN) · [Michigan](https://usleadlist.com/probate/MI)

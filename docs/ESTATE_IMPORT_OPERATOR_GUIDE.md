# Estate import operator guide (Wake County)

Lane B1 · preparation for disconnect 3 · `app/contact/estate_import.py`

This is the contact-pipeline importer. It has never been given a file. It does
not scrape eCourts, does not write `LEGAL_AUTHORITY`, does not mutate DNC, and
does not set any phone callable. A family match is not heirship.

**Wake is why this file exists.** 317 of 322 tier-A owners parse as
HEIRS/DECEASED, and all 322 tier-A properties sit in Wake County. The Wake
NCSBE voter file carries **0 phone values across 999,645 rows (0.0000% fill)**,
re-verified on the 2026-08-24 bytes. Matching a Wake-resident identity cannot
produce a phone from that source. Resolved heirs often live in Mecklenburg or
Durham, where fill is 21–48%. The estate file's living-person **mailing
address** is the cross-county match key.

---

## 1. Absolute prohibitions

- Do **not** script, scrape, or automate `nccourts.gov` or the eCourts Portal
  (`portal-nc.tylertech.cloud`). Scripted clients receive HTTP 403. Smart Search
  sits behind a human-verification challenge that re-arms about every ten
  minutes. The download, where one exists, is an interactive POST carrying an
  anti-forgery token — **one operator click per county, never scripted**.
- Do **not** solve a CAPTCHA. Stop and resume later.
- Do **not** contact anyone. Do not dial, SMS, or email.
- Do **not** spend on any provider.
- Do **not** mix this file with `import_packages/estate/estate_import_template.csv`.
  That is a different contract (party-graph writeback). This lane uses
  `_raw/estate/` and `ESTATE_IMPORT_COLUMNS` only.

---

## 2. What a human actually gets from Wake eCourts

eCourts does **not** emit a CSV matching this importer. T30A drove the Portal
in an ordinary browser and recorded:

| What the Portal shows | What this importer needs |
|---|---|
| Smart Search result list (case number, case type, parties) | `estate_case_number`, `decedent_name` |
| Register of Actions — party **roles** | living names into `applicant_name` / `co_applicant_name` / `personal_representative_name` / `heir_name`, plus `relationship` |
| Filing date / case status | provenance only (`source_date` is the day **you** transcribed, ISO) |
| Documents | often **not** downloadable anonymously; settling authority may need a clerk copy. This importer does not assert authority. |

There is no bulk "estate extract" button that produces `_raw/estate/*.csv`.
The operator transcribes living parties from the Register of Actions (and from
the clerk file when the Portal does not publish an address) into the CSV
below.

Portal (ordinary browser only):

`https://portal-nc.tylertech.cloud/Portal/`

Wake Clerk of Superior Court, Estates Division — public terminals / written
copy requests when the Portal does not publish the mailing address or the
appointment document. Inspection is $0.00; per-page copy fees may apply.

### How to search (T30A procedure, do not "improve" it)

1. Open the Portal in an ordinary browser. Never script it.
2. If a human-verification challenge appears, **stop**. Do not solve it in an
   agent session.
3. Advanced Filtering Options: **uncheck "All Locations (default)" first**,
   then check Wake, then visually confirm both states. Checking a county while
   All Locations remains checked is a silent no-op.
4. Search `LAST, FIRST` first. Not the full middle name — Smart Search matches
   the middle token literally.
5. Narrow with `LAST, FIRST M` only when volume is unusable.
6. Raise items-per-page rather than paging; estate (`E`) cases are scattered.
7. Open the matching case's **Register of Actions** for party roles. The index
   row alone does not say who is the personal representative.
8. Match standard: full name agreement, case county is the **property's**
   county (Wake) or a documented reason it is not, case type is an estate
   proceeding **for this decedent** (not a case on which they are merely a
   party). Same surname + similar first name + an estate in another county is
   coincidence, not evidence.

A located case is **not** legal authority. Letters testamentary / letters of
administration live in the file. This importer records living identity for
NCSBE matching. It never writes `LEGAL_AUTHORITY`.

eCourts coverage begins at the county go-live date. Older Wake estates may
exist only in the clerk's own index. A public-index no-match does **not**
prove that no estate exists.

---

## 3. Exact file contract

Pinned in `app/contact/estate_import.py`. The dry-run validator scores this
without importing.

| Item | Exact value |
|---|---|
| Drop path | `_raw/estate/` — i.e. `{raw_dir}/estate/` where `--raw-dir` defaults to `~/neuruh-workspace/T19F_DEEDSONAR_FREE_PHONE_ENRICHMENT_20260818/_raw` |
| Filename | `{county_slug}_estate_{YYYY-MM-DD}.csv` · example: `wake_estate_2026-08-28.csv` |
| County slug | lowercase CURRENT NC county (`wake`, `mecklenburg`, `durham`, …) |
| Date in the filename | the day the file was shaped (ISO calendar date), not the case filing date |
| Forbidden names | `estate_import_template.csv`, any `*template.csv`, any name starting `template` (the runner skips these) |
| Encoding | UTF-8, **no BOM** |
| Delimiter | comma `,` (not tab, not semicolon) |
| Header | **first row**, exact names below, recommended in this order |
| `source_date` | `YYYY-MM-DD` (ISO-8601 calendar date), stored as text |
| One row | one living person (or several **sharing the same mailing**). Split rows when mailings differ. Never a row whose only person is the decedent. |

### Required header columns (`ESTATE_IMPORT_COLUMNS`)

```
property_candidate_id, apn, county, fips, decedent_name, estate_case_number,
applicant_name, co_applicant_name, personal_representative_name, heir_name,
relationship, mailing_address, mailing_city, mailing_state, mailing_zip,
attorney_name, source_document, source_date
```

| Column | Required on a data row | What to put |
|---|---|---|
| `property_candidate_id` | yes | CURRENT estate `candidate_id`, verbatim. Join key. |
| `apn` | yes (identity) | parcel APN from the estate row |
| `county` | yes | `Wake` (title case, matching the estate master) |
| `fips` | yes | `37183` for Wake |
| `decedent_name` | yes | as the roll / petition names the decedent. **Not a living-person field.** |
| `estate_case_number` | yes | the court's file number. Without this there is no court record. |
| `applicant_name` | one of the four living fields | `LAST, FIRST [MIDDLE]` as the **record** names them |
| `co_applicant_name` | one of the four | same |
| `personal_representative_name` | one of the four | same |
| `heir_name` | one of the four | same. Do not invent an heir from a household surname. |
| `relationship` | recommended | court-stated role: `APPLICANT`, `CO_APPLICANT`, `PERSONAL_REPRESENTATIVE`, `HEIR`, `EXECUTOR`, `ADMINISTRATOR`, or a family label **only when the estate file itself states it**. Empty → defaults from which name field was filled. |
| `mailing_address` / `_city` / `_state` / `_zip` | yes | the **living person's** mailing as the court file shows it. `mailing_state` is 2-letter uppercase. This is the Mecklenburg/Durham match key. |
| `attorney_name` | no | provenance only. **Never** copied into a living-name field. |
| `source_document` | yes | e.g. `NC_ECOURTS_REGISTER_OF_ACTIONS` or the AOC form / clerk copy id |
| `source_date` | yes | `YYYY-MM-DD` of the transcription |

Living people are emitted **only** from the four name fields. `attorney_name`
is ignored as a person. Rows whose `relationship` is `ATTORNEY`, `COUNSEL`,
`LAWYER`, or `ESQ` are skipped entirely.

`LEGAL_AUTHORITY` is refused in this file. That class belongs on the
party-graph importer, and only when `source_family` is a court family. A
shared surname, a household, or a tax-bill recipient is not an executor.

Name shape: `LAST, FIRST` or `LAST, FIRST M`. That is what
`parse_owner_name`'s comma parser reads most reliably.

---

## 4. How to shape the Portal / clerk facts into the CSV

1. Take the Wake slice of the still-missing / HEIRS-DECEASED worklist. Copy
   `candidate_id`, `apn`, `county`, `fips`, and the roll `owner_name` (as
   `decedent_name` after stripping a trailing `HEIRS` only in your notes — in
   the CSV, write the decedent as the petition names them).
2. Search eCourts as §2. Confirm the estate is **this** decedent in **Wake**.
3. From the Register of Actions, list every **living** party with a role.
   Omit the decedent. Omit counsel as a living person (optional
   `attorney_name` only).
4. For each living party, one CSV row:
   - put their name in **exactly one** of the four living fields that matches
     their role (applicant → `applicant_name`, PR →
     `personal_representative_name`, named heir → `heir_name`, …);
   - set `relationship` to that role, or to the family label **the file
     states**;
   - copy their mailing from the file, not from the parcel situs, and not
     from a voter-file guess;
   - set `estate_case_number`, `source_document`, `source_date`.
5. Two living people with two mailings → two rows sharing
   `property_candidate_id` and `estate_case_number`.
6. Save as UTF-8 CSV, no BOM, comma-delimited, filename
   `wake_estate_YYYY-MM-DD.csv`.
7. Dry-run **before** dropping:

```
python scripts/validate_estate_import.py --file /path/to/wake_estate_YYYY-MM-DD.csv
```

Every column prints `header=PASS/FAIL`, populated/empty counts, and
`format=PASS/FAIL`. `imported: NO` is the point of this command.

8. Only after OVERALL PASS, copy the file to:

```
~/neuruh-workspace/T19F_DEEDSONAR_FREE_PHONE_ENRICHMENT_20260818/_raw/estate/wake_estate_YYYY-MM-DD.csv
```

Then re-check placement:

```
python scripts/validate_estate_import.py --file <that-path> --expect-placement
```

Do not run the free-phone enrichment, NCSBE match, or DeedSonar projection
from this lane. Those are later, and they still do not authorize calling.

---

## 5. Downstream flow (documented, not executed)

This lane does not run the chain. When an operator later drops a passing
file, the **existing** contact system — not a second one — does this:

```
_raw/estate/{county}_estate_{YYYY-MM-DD}.csv
    → load_estate_import_csv  (skip *template.csv)
    → parse_estate_import_rows
         living people only; person.role = NAMED_HEIR_ON_ROLL
         EstateCasePerson.relationship = court role
         heir_person_id = HEIR_ + sha256(candidate|case|name|relationship)[:16]
         decedent is never emitted
    → join property_candidate_id to CURRENT estate master row
         (no join → that person is skipped; the candidate must already exist)
    → NCSBE last-name index across the seven CURRENT counties
         (not Wake-only: Wake phone fill is 0.0000%)
    → match_person_to_voters(person, property_addr, heir_mailing, property_county, voters)
         TIER_A = exact core address match (county-agnostic) — this is the
                  cross-county path (Wake parcel, Mecklenburg/Durham mailing)
         TIER_B = house+street+zip AND voter county == property county
                  (a Charlotte heir of a Wake parcel will not pass TIER_B)
         TIER_C = held, never auto-appended
         name-only never appends
    → phone accepted only if numbering-plan VALID_US
         attorney / counsel phones never labelled owner/heir
    → confidence.score_contact  HIGH enters the append candidate set
         MEDIUM / REVIEW / TIER_C held
         callability_status stays NOT_AUTHORIZED
         dnc_status stays UNKNOWN_NOT_EVALUATED unless a later DNC lane
    → product overlay (phone_append_combined.csv, resolution ledger)
    → DeedSonar projection (scripts/project_contacts_to_deedsonar.py)
         HIGH contacts only, into parties / party_phones / contact_evidence
         NEVER into acquisition_leads.phone_* (those require DNC-clear)
         NEVER callable
```

Wake-specific implication: a perfectly matched **Wake** voter still yields
`IDENTITY_MATCHED_VOTER_PHONE_ABSENT`. The yield from this file is living
parties whose **mailing** corroborates a Mecklenburg / Durham / other
filled-county voter at TIER_A.

Family relationship on this CSV is a court-stated label for identity
matching. It is not `LEGAL_AUTHORITY`, not a signatory, and not permission
to call.

---

## 6. Synthetic fixture (no real person data)

`tests/fixtures/estate/wake_estate_2026-08-28.csv` is the contract example.
All names are synthetic (`ALDERNEY`, `MAPLEWOOD`, `FAIRHAVEN`, `RIVERBEND`,
`STONECROFT`, `NORTHFORK`, `CEDARHILL`, `WILLOWBEND`). Do not replace them
with real parties. Real Wake estate files stay outside Git, in `_raw/estate/`.

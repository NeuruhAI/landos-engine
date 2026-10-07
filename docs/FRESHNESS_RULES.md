# Freshness rules (B1, 2026-09-02) — rules, not ML

Every contact-bearing field on a row carries five stamps: `source`, `source_as_of` (the date the source says the value is true), `observed_at` (when DeedSonar read it), `confidence`, `provenance_url`. `staleness_days = today − source_as_of`. **Unknown stays unknown** — a field without `source_as_of` is STALE by rule and reported as unknown; it is never given a default date.

| field | FRESH (≤ days) | AGING (≤ days) | STALE |
|---|---|---|---|
| phone | 90 | 365 | > 365 d, future, or unknown as-of |
| mailing_address (validated) | 180 | 365 | idem |
| email | 180 | 365 | idem |
| heir_link | 365 | 730 | idem |
| owner_name | 365 | 730 | idem |
| assessed_value | 365 | 730 | idem |
| arrears | 90 | 365 | idem |
| deed | 365 | 730 | idem |

Row `freshness_score`: FRESH when every present contact field is FRESH; AGING when none is STALE; STALE otherwise (including a row with no contact field at all).

Code: `app/freshness.py` (`field_freshness`, `row_freshness_score`, `FRESHNESS_RULES`) in landos-engine; the DeedSonar page/export RPC mirrors the same table (`deedsonar_freshness_v1`) so screen == SQL == CSV.

Where `source_as_of` comes from today:
- phone (party graph): `party_phones.provenance.source_observed_at` (NCSBE file date); Tracerfy: the trace file's date (`import_resolved --as-of` / file-name stamp)
- mailing address: the import batch's `as_of` (deed/tax roll date) until USPS validation stamps its own date (T9)
- assessed value / arrears: the Aug-26 scrub file date (`scrub_facts` overlay `source_as_of`)
- heir link: `contact_evidence.observed_at` / the estate file's filing date when parsed from an AOC PDF
- owner name: the batch `as_of`

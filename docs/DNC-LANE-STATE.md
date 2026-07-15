# DNC lane state — closed

Closed at `2026-07-15T03:09:22Z` on
`fix/dnc-queue-2353-recovery`. Queue 2353 was recovered without a new
submission, new spend, or deploy. The deferred tax-sale promotion lane was not
touched.

## Final census

This is the exact 48-target queue cohort, not a projection.

| Census point | Verified-clean | Unknown | Suppressed | Callable |
| --- | ---: | ---: | ---: | ---: |
| Before recovery | 0 | 46 | 2 | 0 |
| After intake and sync | 43 | 1 | 4 | 43 |

The honest callable result is **43**.

Tracerfy returned 48 canonical results: 45 clean and 3 DNC. Their database
shape was:

- 43 clean results linked to spine rows and became verified-clean/callable.
- 2 DNC results linked to spine rows and became hard suppressions.
- 3 results had no spine row: 2 clean and 1 DNC. They were not projected or
  used to infer permission. Their prior fail-closed CRM verdicts remain in
  force, yielding one unknown and two suppressed rows.

Thus 45 provider-clean results were reconciled, but only 43 were eligible for
the sole-writer sync. The other two were deliberately not described as
"synced."

## Paid-queue recovery

- Queue: `2353` (already paid; estimated original charge `$0.96`).
- Resume mode: GET-only `--resume-dnc-queue 2353`; no `--apply`, no
  `--confirm-spend`, and no provider submission call.
- Provider result: 48 unique canonical phones, 45 clean, 3 DNC.
- Root cause of the original 47/47 mismatch: submitted values used mixed
  10/11-digit NANP representation while returned values used 10 digits. The
  canonical sets matched 48/48 with no duplicates, missing values, or extras.
- Receipt:
  `data/incoming/tracerfy_dnc_unknown_48_queue2353.csv`
- Receipt manifest:
  `data/incoming/tracerfy_dnc_unknown_48_queue2353.csv.manifest.json`
- Receipt SHA-256:
  `bcb698a975d18bf56869b7ef384c319c0a635daa1addbbc129e359ec34da4809`

The receipt and manifest are gitignored because they contain or describe
sensitive contact data. They are reproducible from paid queue 2353 using the
pushed GET-only recovery path; the SHA above pins the accepted artifact.

## Intake result

The authorized intake committed run
`a28c3b64-02da-435c-be62-5bcc9d0f0b50` at
`2026-07-15T03:05:46.067557Z`:

- 45 linked spine verdicts updated.
- 43 existing linked clean-phone receipts refreshed.
- 2 linked DNC-only targets marked `do_not_call`.
- 3 targets skipped because no spine row exists.
- 0 phone numbers added; provider DNC digits were never added by intake.
- Existing phone entries on hard-suppressed leads were preserved under the
  AI-never-deletes rule, but are not projected or callable.

Intake manifest:
`data/intake_tracerfy_phones_20260715T030546Z.manifest.json` (gitignored and
also recorded in the database run log).

## Sole-writer sync result

The sync was scoped to the 45 linked owner leads. Its read-only dry-run found
zero callable leads without seller contacts. The authorized apply then
reported:

- `phones_projected=43`
- `non-callable_kept_phoneless=2`
- `dnc_suppressed=2`
- Contact bridge: `noop=6`, `left=43`, `retracted=39`
- Callable-contact orphans after the transaction: 0

The bridge retracted stale spine-owned seller-contact phones for non-callable
rows. Human-entered contacts were not deleted or rewritten.

## Recovery rails now in code

- Queue identity must match before inline rows or a download URL are accepted.
- Canonical submitted/returned phone sets must be unique and identical before
  receipt creation.
- Mismatch output includes queue ID and exact canonical missing/extra sets.
- Resume mode is mutually exclusive with paid submission/apply flags.
- Intake fails closed, never stores a DNC result digit, and marks only exact
  receipt-clean stored phone entries as clean.
- Extra stored phones remain unchecked and are excluded from CRM projection.
- CRM projection requires explicit `dnc_status='clear'`; unknown is never
  coerced to clean.

## Verification

- Targeted DNC/recovery/intake/sync suite: `60 passed`.
- `git diff --check`: passed.
- Receipt membership: 48 canonical matches, 0 missing, 0 extra, 0 duplicate
  keys.
- Post-sync callable-contact orphans: 0.

## Published code

- `764500b` — `fix(dnc): reconcile paid queues and fail closed on unknown`
- `2ce33c7` — `fix(dnc): project only receipt-clean phone evidence`

The final documentation commit is the branch tip recorded by the final push
sweep. All code and documentation are remote-backed. No new Tracerfy spend and
no deploy occurred in this closeout.

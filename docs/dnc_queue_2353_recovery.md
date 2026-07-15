# DNC queue 2353 recovery runbook

Queue 2353 is an already-paid Tracerfy DNC scrub. It must never be submitted
again. Recovery is a queue GET plus download of the existing result, followed by
an immutable local receipt. It does not write either database.

Recovery and the authorized intake/sync are complete. See
[`DNC-LANE-STATE.md`](DNC-LANE-STATE.md) for the final receipt, exact census,
and remote-backed handoff. The commands below preserve the original recovery
gates for audit and reproduction.

## Proven state

- Queue envelope: `id=2353`, `pending=false`, `rows_uploaded=48`,
  `phones_checked=48`, `credits_deducted=48`.
- Result: 48 unique rows, 45 clean and 3 DNC.
- Submitted representation: 47 leading-`1` eleven-digit values and one
  ten-digit value.
- Returned representation: 48 ten-digit values.
- Raw comparison: one overlap, 47 missing, 47 extra.
- Canonical NANP comparison: 48 overlaps, zero missing, zero extra, zero
  collisions.
- Pre-recovery database census: 46 unknown, 2 hard-DNC, 0 callable. Queue
  recovery and receipt creation do not change that census.

The original failure was local representation mismatch, not queue confusion,
ordering, pagination, or an incomplete result.

## Code rails

- A DNC queue response must carry one unambiguous identity matching the requested
  queue before an inline result or download URL is accepted.
- The canonical submitted and returned phone sets must be unique and identical.
- A mismatch prints `queue_id`, exact canonical missing/extra sets, row count,
  and duplicate keys before receipt creation.
- `--resume-dnc-queue` is mutually exclusive with `--apply` and
  `--confirm-spend`; its code path cannot call the paid submission function.
- Intake retains DNC-only target evidence without storing DNC phone digits.
- Existing spine phones receive clean evidence only for canonical numbers in the
  receipt. Additional stored phones are preserved but remain unchecked and are
  excluded from CRM projection; a missing receipt-clean number still aborts.
- CRM projection requires `dnc_status='clear'`; unknown remains NULL and
  non-callable. Existing TRUE is sticky, and the operator smoke fixture can
  never be automated to FALSE.

## Worktree and branch

```text
/Users/jeramiehicks/Documents/New project/worktrees/landos-engine-dnc-2353
fix/dnc-queue-2353-recovery
base: origin/main at e81b859
```

Do not use the dirty shared worktree at
`/Users/jeramiehicks/neuruh-repos/NeuruhAI/landos-engine`.

## Verify and publish the code first

```bash
set -euo pipefail
cd '/Users/jeramiehicks/Documents/New project/worktrees/landos-engine-dnc-2353'
set -a
source /Users/jeramiehicks/neuruh-repos/NeuruhAI/landos-engine/.env
set +a

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  /Users/jeramiehicks/neuruh-repos/NeuruhAI/landos-engine/.venv/bin/pytest -q \
  tests/test_scrub_crm_phone_unknowns.py \
  tests/test_tracerfy_dnc_queue_guard.py \
  tests/test_intake_tracerfy_phones.py \
  tests/test_sync_dnc_projection.py \
  tests/test_seller_contact.py \
  tests/test_source_guard.py

git diff --check
git add docs/dnc_queue_2353_recovery.md \
  scripts/intake_tracerfy_phones.py \
  scripts/scrub_crm_phone_unknowns.py \
  scripts/skip_trace_tracerfy.py \
  scripts/sync_to_crm.py \
  tests/test_intake_tracerfy_phones.py \
  tests/test_scrub_crm_phone_unknowns.py \
  tests/test_seller_contact.py \
  tests/test_source_guard.py \
  tests/test_sync_dnc_projection.py \
  tests/test_tracerfy_dnc_queue_guard.py
git commit -m 'fix(dnc): reconcile paid queues and fail closed on unknown'
git push -u origin fix/dnc-queue-2353-recovery
```

Targeted verification currently passes 60 tests. The full suite has one known,
unrelated baseline failure in `test_load_parcel_geom.py` because Berrien's
mineral-scout source does not start with `arcgis:`.

## Zero-charge queue recovery

This is the only authorized provider command. It contains no `--apply` and no
`--confirm-spend`.

```bash
set -euo pipefail
cd '/Users/jeramiehicks/Documents/New project/worktrees/landos-engine-dnc-2353'
set -a
source /Users/jeramiehicks/neuruh-repos/NeuruhAI/landos-engine/.env
set +a
: "${TRACERFY_API_KEY:?TRACERFY_API_KEY missing}"

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  /Users/jeramiehicks/neuruh-repos/NeuruhAI/landos-engine/.venv/bin/python -B \
  -m scripts.scrub_crm_phone_unknowns \
  --resume-dnc-queue 2353 \
  --cap-usd 2.00 \
  --output '/Users/jeramiehicks/Documents/New project/worktrees/landos-engine-dnc-2353/data/incoming/tracerfy_dnc_unknown_48_queue2353.csv'
```

Expected terminal receipt: queue 2353, 48 matched phones, 45 clean, 3 DNC. If
identity or membership differs, the command writes no CSV or manifest.

## Print-stop after receipt

Receipt creation does not authorize database writes. Present the intake dry-run
before any apply:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. \
  /Users/jeramiehicks/neuruh-repos/NeuruhAI/landos-engine/.venv/bin/python -B \
  -m scripts.intake_tracerfy_phones \
  --csv '/Users/jeramiehicks/Documents/New project/worktrees/landos-engine-dnc-2353/data/incoming/tracerfy_dnc_unknown_48_queue2353.csv'
```

The aggregate provider result does not reveal how 45 clean and 3 DNC outcomes
split between the 45 linked spine targets and three CRM-native targets. The
dry-run is the required evidence. Do not run intake `--apply`, CRM sync, or a
post-sync census without a new operator authorization.

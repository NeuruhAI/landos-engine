# Operator writeback — the one path human research enters the graph

The Qualified work queue tells an operator exactly which record to pull and what
fact to capture. Until T30 it also told him to write the answer into
`import_packages/estate/estate_import_template.csv`, and **that file did not
exist**. The instruction named a destination that was not there, so research
that got done had nowhere to land — which is the same as research that never
happened.

This is that destination, and `scripts/import_operator_estate_research.py` is
the only thing that reads it.

## One row per (candidate, person, role)

Not one row per estate file. A file that names an executor and three heirs is
four rows sharing an `estate_file_number`, because four separate relationships
are being asserted and each carries its own evidence.

| column | required | what it means |
|---|---|---|
| `candidate_id` | yes | from the queue's last column. Verbatim. |
| `decedent_name` | yes | as the parcel roll writes it, so the row can be checked against the record it came from |
| `county` | yes | the county whose Clerk holds the file |
| `estate_file_number` | for authority | the file's own identifier, e.g. `24 E 1234` |
| `estate_status` | no | open / closed / administered |
| `filing_date` | no | `YYYY-MM-DD` |
| `person_name` | yes | the person as the RECORD names them |
| `relationship_role` | yes | `EXECUTOR`, `ADMINISTRATOR`, `PERSONAL_REPRESENTATIVE`, `HEIR`, `COOWNER`, `TRUSTEE`, `OTHER_VERIFIED_RELATION` |
| `relationship_class` | yes | `LEGAL_AUTHORITY` only when the estate record itself confers it. Otherwise `ROLE_STATED_ON_RECORD`, `PROPERTY_INTEREST`, or `FAMILY`. |
| `family_relation` | only for `FAMILY` | `SPOUSE`, `CHILD`, `SIBLING`, `PARENT`, `NAMED_SURVIVOR`, … |
| `source_family` | yes | `ESTATE_IMPORT`, `PROBATE_INDEX`, or `OPERATOR_MANUAL_PUBLIC_RECORD` |
| `source_name` | yes | the office, e.g. `WAKE_CLERK_OF_SUPERIOR_COURT_ESTATES` |
| `source_locator` | yes | how another person finds the same record again |
| `observed_at` | yes | `YYYY-MM-DD`, the day the record was read |
| `confidence` | yes | `HIGH` when the record states it plainly; `REVIEW` when it is inferred |
| `evidence_note` | yes | one sentence: what the record actually said |
| `operator` | yes | who read it |

## The rule the importer enforces, not the one it hopes for

`relationship_class = LEGAL_AUTHORITY` is **refused** unless `source_family` is
one of `ESTATE_IMPORT`, `PROBATE_INDEX`, `OPERATOR_MANUAL_PUBLIC_RECORD`. The
same refusal is a database trigger, so it holds even if this importer is
bypassed.

A shared surname is not an heirship. A person at the same address is not an
heir. The party who receives the tax bill is not an executor. Any of those may
be recorded — as `FAMILY` or `ROLE_STATED_ON_RECORD`, which is what they are.

## Running it

```
python scripts/import_operator_estate_research.py import_packages/estate/estate_import_template.csv          # validate only, default
python scripts/import_operator_estate_research.py import_packages/estate/estate_import_template.csv --apply
python scripts/project_household_identity.py --apply     # reflect it in the read model
python scripts/refresh_next_best_source.py --apply ...   # re-route
```

Notes may accompany evidence. Notes are never the evidence.

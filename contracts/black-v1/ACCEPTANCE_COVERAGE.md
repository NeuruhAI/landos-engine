# BLACK V1 offline acceptance coverage

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/run_black_acceptance.py
```

The runner emits one `PASS` or `FAIL` line per ID and exits nonzero on any
failure. It reads only the byte-pinned files under `contracts/black-v1/`.

| IDs | Offline coverage |
|---|---|
| `AT-EV-01..03` | Schema required/extra/type checks, self-excluding content hash, tamper sensitivity, entity PII-key rejection |
| `AT-UW-01..04` | Schema and Pydantic parse, honest null with no comps, v2 projection envelope, mandatory human underwriting gate for offer/contract |
| `AT-AP-01..03` | Schema parse, human approval for irreversible actions, expiry boundary; mint is not an accepted automated action |
| `AT-OR-01..03` | Schema parse, synthetic/test calibration exclusion, explicit deviations |
| `AT-CR-01..03` | Schema parse, eligible-only sample count, immutable n=30 claim and n=100 change thresholds, no auto-apply |
| `AT-MF-01..03` | Fixed section order, LandOS receipt-style section/chain recompute, body/hash/order tamper failure |
| `AT-SS-01` | Pinned BLACK observation that production payload count was zero; no production query |
| `AT-SS-02` | Pinned scope: `pipeline_stage_audit` targets stage definitions, not lead drag |

Not runnable in this offline/no-production lane:

| IDs | Required later evidence |
|---|---|
| `AT-SS-03` | Read-only CRM count proving production outcome filters |
| `AT-SS-04` | Operating scheduler receipt, only after approved activation |
| `AT-SS-05` | Export from a later approved live proof bundle for PII scan |

The absence of those three external-state checks is not reported as a pass. Their
activation belongs to WHITE/Phase 2 and the operator-approved live-case runbook.

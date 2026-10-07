"""Operator CLI (read-only except `run`): plan a ladder, inspect a job, verify a ledger.

    python -m app.free_first plan --fact contact.phone --state NC --county 37183
    python -m app.free_first verify --ledger ~/.neuruh/landos-engine/free-first/ledger.jsonl
    python -m app.free_first job --ledger <path> --job ffj_...
    python -m app.free_first knowledge
"""
from __future__ import annotations

import argparse
import json
import sys

from .knowledge import knowledge_snapshot, plan_ladder
from .ledger import FreeFirstLedger
from .policy import exhaustion


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.free_first")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--fact", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--county", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--ledger", required=True)
    j = sub.add_parser("job")
    j.add_argument("--ledger", required=True)
    j.add_argument("--job", required=True)
    sub.add_parser("knowledge")
    a = ap.parse_args(argv)
    if a.cmd == "plan":
        out = plan_ladder(a.fact, state=a.state, county_fips=a.county).to_dict()
    elif a.cmd == "verify":
        out = FreeFirstLedger(a.ledger).verify()
    elif a.cmd == "job":
        state = FreeFirstLedger(a.ledger).job_state(a.job)
        out = {"job_id": state.job_id, "fact": state.fact, "target": state.opened["target"],
               "jurisdiction": state.opened["jurisdiction"], "exhaustion": exhaustion(state).to_dict(),
               "attempts": len(state.attempts), "human_outcomes": len(state.human_outcomes),
               "authorizations": len(state.authorizations),
               "paid_decisions": [{"allow": d["allow"], "provider": d["provider"], "reasons": d["reasons"]}
                                  for d in state.paid_decisions],
               "spent_usd": state.spent_usd()}
    else:
        out = knowledge_snapshot()
    json.dump(out, sys.stdout, indent=2, sort_keys=True, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

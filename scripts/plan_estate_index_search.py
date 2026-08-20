#!/usr/bin/env python3
"""T30A §10 — drive the next estate search FROM the captured procedure.

This is the replay test made executable. It does not browse. It answers, for one
decedent, the questions an operator or a later lane would otherwise have to
rediscover: should this search run at all, in which county, with which query
first, when to widen, which outcomes are possible, what evidence each outcome
may create, and what happens next.

It reads the procedure from `app.resolution.source_procedure` -- the single
canonical record -- and the banked run state from the private root. It invents
nothing.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contact.owner_identity import parse_owner_name
from app.contact.household import alternate_surnames, decedent_surnames  # noqa: F401
from app.resolution.source_procedure import (
    procedure_for,
    supports_replay_suppression,
)

PRIVATE = Path.home() / ".neuruh/private/deedsonar/wake-estates"


def name_variants(owner_raw: str) -> list:
    """Ordered as the procedure requires: shortest useful form FIRST."""
    op = parse_owner_name(owner_raw)
    seen, out = set(), []
    for group_first in [p for i, p in enumerate(op.people)]:
        last, first, mid = group_first.last, group_first.first, group_first.middle
        if not (last and first):
            continue
        for form in (f"{last}, {first}",
                     f"{last}, {first} {mid[:1]}".strip(),
                     f"{last}, {first} {mid}".strip()):
            form = " ".join(form.split()).rstrip(",").strip()
            if form and form not in seen:
                seen.add(form)
                out.append(form)
        break  # primary reading only; the alternate is a weaker claim
    return out


def plan(rank: int) -> dict:
    proc = procedure_for("PROBATE_INDEX")
    if proc is None:
        return {"error": "no procedure learned for PROBATE_INDEX"}

    targets = json.loads((PRIVATE / "00_TARGETS_GRADE_A.json").read_text())
    findings = json.loads((PRIVATE / "01_ECOURTS_FINDINGS.json").read_text())
    row = next((r for r in targets if r["global_rank"] == rank), None)
    if row is None:
        return {"error": f"rank {rank} not in the target manifest"}

    owner_raw = row["owner_name"]
    variants = name_variants(owner_raw)
    canonical = variants[0] if variants else ""

    # PRECHECK, straight from the procedure. Keyed on the DECEDENT, because one
    # decedent can hold several parcels -- the lesson this court paid for.
    prior = [f for f in findings["decedents"]
             if f["decedent_search"].upper() == canonical.upper()]
    already = bool(prior)
    suppress = any(supports_replay_suppression(
        f.get("verdict", "").replace("ECOURTS_PUBLIC_SEARCH=", ""))
        for f in prior)

    same_decedent_other_ranks = [r["global_rank"] for r in targets
                                 if r["global_rank"] != rank
                                 and name_variants(r["owner_name"])[:1] == variants[:1]]

    return {
        "procedure": f"{proc.name} v{proc.version}",
        "taught_by": proc.taught_by,
        "rank": rank,
        "candidate_id": row["candidate_id"],
        "county": row["source_county"],
        "county_scope_required": True,
        "county_scope_trap": (
            "UNCHECK 'All Locations (default)' FIRST, then check "
            f"{row['source_county']} County, then VISUALLY CONFIRM both."
        ),
        "owner_condition": row["owner_condition"],
        "trigger_met": row["owner_condition"] in
                       ("DECEASED", "HEIRS_UNRESOLVED", "ESTATE_OPEN"),
        "precheck": {
            "decedent_already_searched": already,
            "prior_verdicts": [f.get("verdict") for f in prior],
            "replay_suppressed": suppress,
            "other_candidates_same_decedent": same_decedent_other_ranks,
        },
        "should_search": (not suppress) and row["owner_condition"] in
                         ("DECEASED", "HEIRS_UNRESOLVED", "ESTATE_OPEN"),
        "first_query": canonical,
        "widen_order": variants[1:],
        "widen_rule": (
            "Only if the first query returns nothing. Never start with the full "
            "middle name: Smart Search matches the middle token literally."
        ),
        "inconclusive_rule": (
            "If the grouped result set reports the 200-case cap, the answer is "
            "SEARCH_INCONCLUSIVE, not PUBLIC_INDEX_NO_MATCH."
        ),
        "possible_results": list(proc.result_states),
        "evidence_permitted_by_result": {
            "OFFICIAL_ESTATE_CASE_FOUND":
                "a case link, plus LEGAL_AUTHORITY ONLY if the case names an "
                "executor / administrator / personal representative",
            "PUBLIC_INDEX_NO_MATCH":
                "structured negative memory; no party, no relationship",
            "AMBIGUOUS_MATCH": "nothing written to the graph",
            "SEARCH_INCONCLUSIVE": "nothing written; not negative evidence",
            "HUMAN_VERIFICATION_REQUIRED": "nothing written; hand to the operator",
            "OFFICIAL_COPY_REQUIRED": "the case link; authority stays unresolved",
        },
        "next_action_by_result": {
            "OFFICIAL_ESTATE_CASE_FOUND": "open the Register of Actions for party ROLES",
            "PUBLIC_INDEX_NO_MATCH":
                "route to ESTATE_NO_PUBLIC_CASE_FOUND: the county's own estate "
                "index and the recorded deed chain",
            "AMBIGUOUS_MATCH": "corroborate against county + deed before asserting",
            "SEARCH_INCONCLUSIVE": "re-scope and re-run; do not record a negative",
            "HUMAN_VERIFICATION_REQUIRED":
                "preserve candidate/decedent/query/stage and RESUME after the "
                "operator clears it",
            "OFFICIAL_COPY_REQUIRED": "generate an official-copy request packet",
        },
        "must_never": [
            "solve the human-verification challenge",
            "script the Portal",
            "match on surname + similar given name alone",
            "promote a family or household relative to LEGAL_AUTHORITY",
            "record a challenge or an unscoped search as negative evidence",
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rank", type=int, required=True)
    args = ap.parse_args()
    print(json.dumps(plan(args.rank), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

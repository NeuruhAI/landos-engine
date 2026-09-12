#!/usr/bin/env python3
"""T30 §12 — the ONE path human research enters the graph.

The Qualified work queue has always told an operator which office to visit,
which record to pull and which fact to capture. What it could not tell him was
where to put the answer, because the file it named did not exist. Research with
no destination is research that did not happen.

This reads `import_packages/estate/estate_import_template.csv` and writes the
same three tables every automated supply writes -- contact_evidence, parties,
candidate_parties -- so a fact a human found and a fact a script found are the
same KIND of thing in the graph, distinguishable only by their source family,
which is exactly the distinction that should survive.

THE REFUSAL THAT MATTERS. `relationship_class = LEGAL_AUTHORITY` is rejected
unless the row's source family adjudicates. It is checked here so the operator
gets a readable error naming the row, and again by a database trigger so the
rule holds even when this script is not the one writing.

Default is validate-only. Nothing is written without --apply.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.contact_authority import contact_dsn_or_exit
from app.resolution.contract import (
    CONFIDENCE_CATEGORIES,
    FAMILY_RELATIONS,
    LEGAL_AUTHORITY_SOURCE_FAMILIES,
    RELATIONSHIP_CLASSES,
    RELATIONSHIP_ROLES,
    ContractViolation,
    Evidence,
)

REQUIRED = ("candidate_id", "person_name", "relationship_role",
            "relationship_class", "source_family", "source_name",
            "source_locator", "observed_at", "confidence", "evidence_note",
            "operator")


def _s(row: dict, key: str) -> str:
    return " ".join(str(row.get(key) or "").strip().split())


def validate(rows: list, known_candidates: set) -> tuple[list, list]:
    """Every refusal names the row and says what the record would have to be."""
    ok, bad = [], []
    for i, row in enumerate(rows, start=2):   # header is line 1
        errs = []
        for col in REQUIRED:
            if not _s(row, col):
                errs.append(f"{col} is required")
        cid = _s(row, "candidate_id")
        if cid and cid not in known_candidates:
            errs.append(f"candidate_id {cid} is not a current candidate")
        role = _s(row, "relationship_role").upper()
        if role and role not in RELATIONSHIP_ROLES:
            errs.append(f"relationship_role {role!r} is not in the vocabulary")
        klass = _s(row, "relationship_class").upper()
        if klass and klass not in RELATIONSHIP_CLASSES:
            errs.append(f"relationship_class {klass!r} is not in the vocabulary")
        fam = _s(row, "source_family").upper()
        if klass == "LEGAL_AUTHORITY" and fam not in LEGAL_AUTHORITY_SOURCE_FAMILIES:
            errs.append(
                f"LEGAL_AUTHORITY refused: source_family {fam!r} does not "
                f"adjudicate. Only {sorted(LEGAL_AUTHORITY_SOURCE_FAMILIES)} do. "
                f"If the record merely STATES the role, the class is "
                f"ROLE_STATED_ON_RECORD.")
        rel = _s(row, "family_relation").upper()
        if rel and rel not in FAMILY_RELATIONS:
            errs.append(f"family_relation {rel!r} is not in the vocabulary")
        if rel and klass != "FAMILY":
            errs.append("family_relation is only meaningful on a FAMILY relationship")
        conf = _s(row, "confidence").upper()
        if conf and conf not in CONFIDENCE_CATEGORIES:
            errs.append(f"confidence {conf!r} is not in the vocabulary")
        obs = _s(row, "observed_at")
        if obs:
            try:
                datetime.strptime(obs[:10], "%Y-%m-%d")
            except ValueError:
                errs.append(f"observed_at {obs!r} is not YYYY-MM-DD")
        if errs:
            bad.append({"line": i, "candidate_id": cid,
                        "person_name": _s(row, "person_name"), "errors": errs})
            continue
        ok.append(row)
    return ok, bad


def build(rows: list, tenant_by_candidate: dict) -> list:
    out = []
    for row in rows:
        cid = _s(row, "candidate_id")
        person = _s(row, "person_name").upper()
        obs = _s(row, "observed_at")[:10] + "T00:00:00Z"
        note_parts = [_s(row, "evidence_note")]
        if _s(row, "estate_file_number"):
            note_parts.append(f"estate file {_s(row, 'estate_file_number')}")
        if _s(row, "decedent_name"):
            note_parts.append(f"decedent {_s(row, 'decedent_name')}")
        note_parts.append(f"recorded by {_s(row, 'operator')}")
        ev = Evidence(
            source_family=_s(row, "source_family").upper(),
            source_name=_s(row, "source_name").upper(),
            source_record_id=_s(row, "estate_file_number"),
            source_locator=_s(row, "source_locator"),
            field="party_identity",
            raw_value=_s(row, "person_name"),
            normalized_value=person,
            match_method="OPERATOR_READ_OFFICIAL_RECORD",
            confidence=_s(row, "confidence").upper(),
            observed_at=obs, retrieved_at=obs,
            notes="; ".join(p for p in note_parts if p),
        )
        out.append({
            "tenant_id": tenant_by_candidate[cid],
            "import_row_id": cid,
            "canonical_name": person,
            "kind": "person",
            "relationship_role": _s(row, "relationship_role").upper(),
            "relationship_class": _s(row, "relationship_class").upper(),
            "family_relation": _s(row, "family_relation").upper() or None,
            "confidence": _s(row, "confidence").upper(),
            "evidence_id": ev.evidence_id,
            "evidence": ev,
            "notes": ev.notes,
        })
    return out


def apply_rows(conn, rows: list) -> dict:
    stats = {}
    with conn.cursor() as cur:
        cur.execute("""
            create temp table _t30_op_ev (like public.contact_evidence including defaults)
              on commit drop;
            create temp table _t30_op_rel (
              tenant_id uuid, import_row_id uuid, kind text, canonical_name text,
              relationship_role text, relationship_class text, family_relation text,
              confidence text, evidence_id text, notes text) on commit drop;
        """)
        for r in rows:
            e = r["evidence"]
            cur.execute("""
                insert into public.contact_evidence (
                  evidence_id, tenant_id, source_family, source_name, source_record_id,
                  source_locator, field, raw_value, normalized_value, match_method,
                  confidence, observed_at, retrieved_at, notes)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (evidence_id) do nothing
            """, (r["evidence_id"], r["tenant_id"], e.source_family, e.source_name,
                  e.source_record_id, e.source_locator, e.field, e.raw_value,
                  e.normalized_value, e.match_method, e.confidence, e.observed_at,
                  e.retrieved_at, e.notes))
            cur.execute("""
                insert into public.parties (tenant_id, kind, canonical_name)
                values (%s,%s,%s) on conflict (tenant_id, kind, canonical_name) do nothing
            """, (r["tenant_id"], r["kind"], r["canonical_name"]))
            cur.execute("""
                insert into public.candidate_parties (
                  tenant_id, import_row_id, party_id, relationship_role,
                  relationship_class, family_relation, confidence, evidence_id, notes)
                select %s, %s, p.party_id, %s, %s, %s, %s, %s, %s
                  from public.parties p
                 where p.tenant_id = %s and p.kind = %s and p.canonical_name = %s
                on conflict (tenant_id, import_row_id, party_id, relationship_role)
                  do update set relationship_class = excluded.relationship_class,
                                family_relation = excluded.family_relation,
                                confidence = excluded.confidence,
                                evidence_id = excluded.evidence_id,
                                notes = excluded.notes,
                                updated_at = now()
            """, (r["tenant_id"], r["import_row_id"], r["relationship_role"],
                  r["relationship_class"], r["family_relation"], r["confidence"],
                  r["evidence_id"], r["notes"],
                  r["tenant_id"], r["kind"], r["canonical_name"]))
        stats["rows_written"] = len(rows)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?",
                    default=str(ROOT / "import_packages/estate/estate_import_template.csv"))
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(json.dumps({"error": f"no such file: {path}"}, indent=1))
        return 2
    rows = list(csv.DictReader(path.open(newline="", encoding="utf-8")))
    if not rows:
        print(json.dumps({"path": str(path), "rows": 0,
                          "note": "template is empty -- nothing to import, which is "
                                  "the correct state until an operator has read a record"},
                         indent=1))
        return 0

    # DEEDSONAR_DATABASE_URL only (never DATABASE_URL); the retired legacy DB is refused (exit 2).
    with psycopg.connect(contact_dsn_or_exit(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute("select candidate_id::text c, tenant_id::text t "
                        "from public.deedsonar_current_candidates_v1")
            tenant_by_candidate = {r["c"]: r["t"] for r in cur.fetchall()}
        ok, bad = validate(rows, set(tenant_by_candidate))
        report = {"path": str(path), "rows": len(rows), "valid": len(ok),
                  "refused": len(bad), "refusals": bad, "applied": False}
        if bad:
            print(json.dumps(report, indent=1))
            return 1
        try:
            built = build(ok, tenant_by_candidate)
        except ContractViolation as exc:
            report["contract_violation"] = str(exc)
            print(json.dumps(report, indent=1))
            return 1
        if args.apply:
            report.update(apply_rows(conn, built))
            conn.commit()
            report["applied"] = True
            report["next"] = ["scripts/project_household_identity.py --apply",
                              "scripts/refresh_next_best_source.py --apply"]
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

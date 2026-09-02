"""Per-row route attempts (FR-1 / FR-2 / FR-5): one row per (route, candidate, run) in
public.free_route_attempts, written as the DB owner. Dry-run by default: the rows
are returned, not written. A receipt file is the source of truth; the table is
its projection for the operator UI and the court."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any, Dict, Iterable, List, Optional

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


@dataclass
class Attempt:
    import_row_id: str
    route_id: str
    run_id: str
    outcome: str            # HIT | NO_HIT | HALTED | ERROR
    source: str
    source_as_of: Optional[str]
    observed_at: str
    receipt_path: Optional[str] = None
    detail: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self); d["detail"] = d.get("detail") or {}; return d


INSERT_SQL = """
insert into public.free_route_attempts
  (tenant_id, import_row_id, route_id, run_id, outcome, source, source_as_of, observed_at, receipt_path, detail)
values (%(tenant)s, %(import_row_id)s, %(route_id)s, %(run_id)s, %(outcome)s, %(source)s, %(source_as_of)s, %(observed_at)s, %(receipt_path)s, %(detail)s)
on conflict (tenant_id, import_row_id, route_id, run_id) do update
  set outcome = excluded.outcome, source_as_of = excluded.source_as_of, observed_at = excluded.observed_at,
      receipt_path = excluded.receipt_path, detail = excluded.detail
"""


def write_attempts(attempts: Iterable[Attempt], *, database_url: Optional[str], apply: bool = False,
                   connect=None) -> Dict[str, Any]:
    """Write attempts to the spine (apply=True) or count them (dry-run). `connect` is
    injectable for tests; defaults to psycopg.connect."""
    rows = [a.to_dict() for a in attempts]
    summary = {"attempts": len(rows), "written": 0, "apply": apply,
               "by_outcome": {}, "by_route": {}}
    for r in rows:
        summary["by_outcome"][r["outcome"]] = summary["by_outcome"].get(r["outcome"], 0) + 1
        summary["by_route"][r["route_id"]] = summary["by_route"].get(r["route_id"], 0) + 1
    if not apply or not rows:
        return summary
    if not database_url:
        raise RuntimeError("apply requested but no database_url")
    if connect is None:
        import psycopg  # noqa: WPS433 — only when writing
        from psycopg.types.json import Jsonb
        connect = psycopg.connect
    else:
        Jsonb = lambda v: json.dumps(v)  # noqa: E731 — test transport
    with connect(database_url) as conn, conn.cursor() as cur:
        for r in rows:
            cur.execute(INSERT_SQL, {"tenant": TENANT, **{k: r[k] for k in ("import_row_id", "route_id", "run_id", "outcome", "source", "source_as_of", "observed_at", "receipt_path")}, "detail": Jsonb(r["detail"])})
            summary["written"] += 1
        conn.commit()
    return summary

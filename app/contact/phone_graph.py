"""Duplicate / household / multi-property phone graph. Counts only — no PII."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, Iterable, List


def analyze_phone_graph(rows: Iterable[dict]) -> dict:
    by_phone: Dict[str, List[dict]] = defaultdict(list)
    for row in rows:
        phone = (row.get("phone") or "").strip()
        if not phone:
            continue
        by_phone[phone].append(row)

    unique = len(by_phone)
    dup_rows = 0
    multi_property = 0
    high_fanout = 0
    for phone, items in by_phone.items():
        cands = {r.get("candidate_id") for r in items}
        owners = {(r.get("owner_name") or "").strip().upper() for r in items}
        if len(items) > len(cands):
            dup_rows += len(items) - len(cands)
        if len(cands) > 1:
            multi_property += 1
        if len(cands) >= 5 or len(owners) >= 3:
            high_fanout += 1

    return {
        "UNIQUE_PHONES": unique,
        "DUPLICATE_PHONE_ROWS": dup_rows,
        "MULTI_PROPERTY_PHONES": multi_property,
        "HIGH_FANOUT_PHONES": high_fanout,
        "fanout_by_phone": {
            phone: {
                "candidates": len({r.get("candidate_id") for r in items}),
                "owners": len({(r.get("owner_name") or "").strip().upper() for r in items}),
            }
            for phone, items in by_phone.items()
        },
    }


def fanout_for(phone: str, graph: dict) -> tuple[int, int]:
    info = (graph.get("fanout_by_phone") or {}).get(phone) or {}
    return int(info.get("candidates") or 1), int(info.get("owners") or 1)

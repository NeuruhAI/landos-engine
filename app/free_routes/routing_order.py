"""I4: the Recipe Engine receipt's WHY components decide which free route to try
first for a row. Encoded, testable, and read by the free-routes queue."""
from __future__ import annotations

from typing import Dict, Iterable, List

#: component / condition keyword -> ordered routes
ROUTE_RULES: List[tuple] = [
    (("probate", "deceased", "estate", "heir"), ["T1", "T2", "T3", "T4", "I3"]),
    (("entity", "llc", "corp", "trust", "inc"), ["T5", "T8", "T7"]),
    (("absentee", "out_of_state", "mailing_state"), ["I1", "T7", "T9", "T8"]),
    (("tax", "delinquen", "arrears", "foreclos"), ["T8", "T7", "T6"]),
]
DEFAULT_ORDER = ["T6", "T8", "T9", "T7", "T10"]


def route_order_for(components: Iterable[str], owner_condition: str = "", owner_class: str = "") -> List[str]:
    """Deterministic route order from receipt component names plus the row's owner
    condition/class; the generic routes follow, deduplicated, never dropped."""
    keys = [str(c).lower() for c in components] + [owner_condition.lower(), owner_class.lower()]
    order: List[str] = []
    for needles, routes in ROUTE_RULES:
        if any(n in k for k in keys for n in needles):
            for r in routes:
                if r not in order:
                    order.append(r)
    for r in DEFAULT_ORDER:
        if r not in order:
            order.append(r)
    return order

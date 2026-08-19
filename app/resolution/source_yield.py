"""What each source has actually produced, by the cut that matters.

The router chooses from owner condition, which is right and is not enough: a
route that has never worked in a county should not outrank one that has. This
module measures what happened, so a later lane can weight the router by it.

It is descriptive on purpose. Nothing here promotes a recipe, changes a
threshold or reorders a route -- it counts, and the counting is the input
someone else is allowed to act on.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from typing import Dict, Iterable, List, Sequence, Tuple

YIELD_VERSION = "source_yield.v1"

YIELD_COLUMNS = [
    "dimension", "key", "source",
    "attempts", "identities_resolved", "contacts_resolved",
    "high_confidence", "review", "no_result",
    "identity_rate", "contact_rate",
    "manual_effort", "cost_usd",
]


@dataclass
class Cell:
    attempts: int = 0
    identities_resolved: int = 0
    contacts_resolved: int = 0
    high_confidence: int = 0
    review: int = 0
    no_result: int = 0
    manual_effort: str = "AUTOMATIC"
    cost_usd: str = "0.00"

    @property
    def identity_rate(self) -> float:
        return round(self.identities_resolved / self.attempts, 6) if self.attempts else 0.0

    @property
    def contact_rate(self) -> float:
        return round(self.contacts_resolved / self.attempts, 6) if self.attempts else 0.0

    def row(self, dimension: str, key: str, source: str) -> dict:
        out = asdict(self)
        out.update(dimension=dimension, key=key, source=source,
                   identity_rate=self.identity_rate, contact_rate=self.contact_rate)
        return {c: out[c] for c in YIELD_COLUMNS}


#: One row per (dimension, key, source). `dimension` names the cut, so a single
#: table answers "which source works for entities" and "which source works in
#: Wake" without four differently-shaped files.
DIMENSIONS = ("owner_class", "county", "recipe", "resolution_stage")


def build_yield(rows: Sequence[dict]) -> List[dict]:
    """Rows are one per candidate-attempt, already joined to their outcome.

    Required keys: source, owner_class, county, recipe, resolution_stage,
    identity_resolved (bool), contact_resolved (bool), confidence, manual.
    """
    cells: Dict[Tuple[str, str, str], Cell] = defaultdict(Cell)
    for row in rows:
        source = str(row.get("source") or "UNKNOWN")
        for dim in DIMENSIONS:
            key = str(row.get(dim) or "UNKNOWN")
            cell = cells[(dim, key, source)]
            cell.attempts += 1
            if row.get("identity_resolved"):
                cell.identities_resolved += 1
            if row.get("contact_resolved"):
                cell.contacts_resolved += 1
            conf = str(row.get("confidence") or "").upper()
            if conf == "HIGH":
                cell.high_confidence += 1
            elif conf == "REVIEW":
                cell.review += 1
            if not row.get("identity_resolved") and not row.get("contact_resolved"):
                cell.no_result += 1
            if row.get("manual"):
                cell.manual_effort = "MANUAL"
    return [cells[k].row(*k) for k in sorted(cells)]


def rank_sources(yield_rows: Iterable[dict], dimension: str, key: str) -> List[dict]:
    """Sources for one cut, best identity rate first, then contact rate.

    Ties break on attempts, so a source with one lucky hit does not outrank one
    with a hundred.
    """
    subset = [r for r in yield_rows if r["dimension"] == dimension and r["key"] == key]
    return sorted(subset,
                  key=lambda r: (-r["identity_rate"], -r["contact_rate"], -r["attempts"]))

"""Source performance: which source actually earns the next attempt.

T20D measured yield per source once, by hand, for one lane. The router needs it
as a standing fact, sliced the three ways that change the answer: owner class
(an LLC and a decedent are not the same problem), county (Wake's voter file has
no phone column at all), and recipe (a heir-property recipe produces a different
population than a tax-distress one).

Every counter here is derived from observed rows. Nothing is estimated, and a
source that was never attempted reports attempted=0 rather than a zero yield --
"never tried" and "tried and failed" are different facts and the router must not
confuse them.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Dict, Iterable, List, Optional, Sequence

YIELD_ENGINE_VERSION = "source_performance.v1"

YIELD_COLUMNS = [
    "slice_kind", "slice_value", "source_family",
    "attempted", "identity_resolved", "living_party_resolved",
    "phone_found", "email_found", "high_confidence", "held",
    "false_positive", "manual_effort", "cost_usd",
    "identity_rate", "phone_rate", "note",
]


@dataclass
class YieldCell:
    slice_kind: str
    slice_value: str
    source_family: str
    attempted: int = 0
    identity_resolved: int = 0
    living_party_resolved: int = 0
    phone_found: int = 0
    email_found: int = 0
    high_confidence: int = 0
    held: int = 0
    false_positive: int = 0
    manual_effort: str = "AUTOMATIC"
    cost_usd: str = "0.00"
    note: str = ""

    @property
    def identity_rate(self) -> float:
        return round(self.identity_resolved / self.attempted, 6) if self.attempted else 0.0

    @property
    def phone_rate(self) -> float:
        return round(self.phone_found / self.attempted, 6) if self.attempted else 0.0

    def to_row(self) -> dict:
        d = asdict(self)
        d["identity_rate"] = self.identity_rate
        d["phone_rate"] = self.phone_rate
        if not self.attempted:
            d["note"] = (d["note"] or
                         "NEVER ATTEMPTED for this slice - not a zero yield")
        return d


def _key(kind: str, value: str, source: str):
    return (kind, value or "UNKNOWN", source)


def build_yield(
    records: Sequence[dict],
    *,
    manual_sources: Optional[Dict[str, str]] = None,
) -> Dict[str, List[dict]]:
    """Build the three yield slices from resolution rows.

    `records` are the flat per-candidate resolution rows: they carry
    owner_class, county, primary_recipe, the source family attempted, whether an
    identity and a living party were resolved, and whether a phone landed.
    """
    manual_sources = manual_sources or {}
    cells: Dict[tuple, YieldCell] = {}

    def cell(kind: str, value: str, source: str) -> YieldCell:
        k = _key(kind, value, source)
        if k not in cells:
            cells[k] = YieldCell(
                slice_kind=kind, slice_value=value or "UNKNOWN", source_family=source,
                manual_effort=manual_sources.get(source, "AUTOMATIC"),
            )
        return cells[k]

    for rec in records:
        source = rec.get("source_family") or "NCSBE_PUBLIC_VOTER_FILE"
        slices = (
            ("OWNER_CLASS", rec.get("owner_class") or "UNKNOWN"),
            ("COUNTY", rec.get("county") or "UNKNOWN"),
            ("RECIPE", rec.get("primary_recipe") or "UNKNOWN"),
        )
        for kind, value in slices:
            c = cell(kind, value, source)
            c.attempted += 1
            if rec.get("identity_resolved"):
                c.identity_resolved += 1
            if rec.get("living_party_resolved"):
                c.living_party_resolved += 1
            if rec.get("phone_found"):
                c.phone_found += 1
            if rec.get("email_found"):
                c.email_found += 1
            if rec.get("high_confidence"):
                c.high_confidence += 1
            if rec.get("held"):
                c.held += 1

    out: Dict[str, List[dict]] = defaultdict(list)
    for (kind, value, source), c in sorted(cells.items()):
        out[kind].append(c.to_row())
    return out


def recipe_contact_intelligence(records: Sequence[dict]) -> List[dict]:
    """Recipe -> identity / living-party / free-contact / estate / entity / trust.

    This is what tells the operator which recipe produces reachable people
    rather than merely interesting parcels -- and it is deliberately NOT fed
    back into the opportunity score.
    """
    agg: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for rec in records:
        r = rec.get("primary_recipe") or "UNKNOWN"
        a = agg[r]
        a["candidates"] += 1
        a["qualified"] += 1 if str(rec.get("qualification_state", "")).startswith("QUALIFIED") else 0
        a["identity_resolved"] += 1 if rec.get("identity_resolved") else 0
        a["living_party_resolved"] += 1 if rec.get("living_party_resolved") else 0
        a["free_contact"] += 1 if rec.get("phone_found") else 0
        oc = rec.get("owner_class") or ""
        a["estate_rows"] += 1 if oc in {"DECEASED", "ESTATE", "HEIRS"} else 0
        a["entity_rows"] += 1 if oc in {"LLC", "CORPORATION", "OTHER_ENTITY", "NONPROFIT"} else 0
        a["trust_rows"] += 1 if oc == "TRUST" else 0

    rows = []
    for recipe, a in sorted(agg.items()):
        n = a["candidates"] or 1
        rows.append({
            "primary_recipe": recipe,
            "candidates": a["candidates"],
            "qualified": a["qualified"],
            "identity_resolved": a["identity_resolved"],
            "living_party_resolved": a["living_party_resolved"],
            "free_contact": a["free_contact"],
            "estate_rows": a["estate_rows"],
            "entity_rows": a["entity_rows"],
            "trust_rows": a["trust_rows"],
            "identity_yield": round(a["identity_resolved"] / n, 6),
            "living_party_yield": round(a["living_party_resolved"] / n, 6),
            "free_contact_yield": round(a["free_contact"] / n, 6),
            "estate_rate": round(a["estate_rows"] / n, 6),
            "entity_rate": round(a["entity_rows"] / n, 6),
            "trust_rate": round(a["trust_rows"] / n, 6),
            # Outcome columns exist so the schema is stable when operator
            # outcomes arrive. They are NOT invented here.
            "conversation": "NOT_MEASURED",
            "appointment": "NOT_MEASURED",
            "contract": "NOT_MEASURED",
            "close": "NOT_MEASURED",
        })
    return rows


RECIPE_INTEL_COLUMNS = [
    "primary_recipe", "candidates", "qualified",
    "identity_resolved", "living_party_resolved", "free_contact",
    "estate_rows", "entity_rows", "trust_rows",
    "identity_yield", "living_party_yield", "free_contact_yield",
    "estate_rate", "entity_rate", "trust_rate",
    "conversation", "appointment", "contract", "close",
]

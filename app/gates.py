"""
gates.py — deal-killer gates + curable flags.

HARD-KILL gates zero the final score and segregate the parcel (a true deal-breaker
for an acquisition): title_uncured, land_bank_rofr, occupied.

CURABLE flags are surfaced (and cited) but do NOT kill the deal — they're risks to
budget/cure: environmental_flag (e.g. an EGLE Part 201 site → curable with Phase II
+ cleanup, not an automatic pass). This reflects that brownfield/contamination is
often a managed, fundable redevelopment path, not a hard stop.

Pure.
"""
from typing import Any, Dict, List

HARD_KILL_GATES: List[str] = ["title_uncured", "land_bank_rofr", "occupied"]
CURABLE_FLAGS: List[str] = ["environmental_flag"]
GATE_KEYS: List[str] = HARD_KILL_GATES + CURABLE_FLAGS


def evaluate_gates(flags: Dict[str, Any]) -> Dict[str, Any]:
    """Return {killed, gates_failed (hard kills), flags (curable)}."""
    killed = [k for k in HARD_KILL_GATES if bool(flags.get(k))]
    flagged = [k for k in CURABLE_FLAGS if bool(flags.get(k))]
    return {"killed": bool(killed), "gates_failed": killed, "flags": flagged}

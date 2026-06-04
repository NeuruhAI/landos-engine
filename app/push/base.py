"""
base.py — the generic push interface.

THE RULE: the engine NEVER writes to a downstream destination (APG or anything
else) directly. Only a PushAdapter does. Swapping destinations = swapping adapters,
not editing the engine — the engine just hands leads to push().

Contract:
  map_lead(lead)        -> destination-shaped row (field mapping lives in the adapter)
  validate_destination()-> raise if the destination is not confirmed/configured
  _connect()            -> open the destination connection (adapter-specific)
  _write_rows(conn,rows)-> write to the destination, return count
  push(leads, do_push)  -> orchestrates: map -> (gated print OR real write) -> result

GATING: push() with do_push=False (the default everywhere) NEVER connects and NEVER
writes — it prints the adapter's disabled_message and returns a gated PushResult.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class PushResult:
    target: str
    attempted: int
    pushed: int
    gated: bool
    pushed_ids: List[str] = field(default_factory=list)
    notes: str = ""


class PushAdapter(abc.ABC):
    target_id: str  # e.g. "apg" — recorded as owner_leads.push_target

    # ---- per-destination contract -------------------------------------------
    @abc.abstractmethod
    def map_lead(self, lead: Dict[str, Any]) -> Dict[str, Any]:
        """Map a landos owner_lead row to the destination's row shape."""

    @abc.abstractmethod
    def validate_destination(self) -> None:
        """Raise RuntimeError if the destination table/schema is not confirmed."""

    @abc.abstractmethod
    def _connect(self):
        """Open and return the destination connection. Raise if not configured."""

    @abc.abstractmethod
    def _write_rows(self, conn, rows: List[Dict[str, Any]]) -> int:
        """Write mapped rows to the destination. Return the number written."""

    @abc.abstractmethod
    def disabled_message(self, n: int) -> str:
        """The message printed when the push is gated off."""

    # ---- shared orchestration (NOT overridden) -------------------------------
    def push(self, leads: List[Dict[str, Any]], *, do_push: bool = False) -> PushResult:
        rows = [self.map_lead(l) for l in leads]  # mapping is always safe (pure)
        if not do_push:
            print(self.disabled_message(len(leads)))
            return PushResult(target=self.target_id, attempted=len(leads),
                              pushed=0, gated=True, notes="gated: do_push=False")
        # real path — every guard must pass before a single row is written
        self.validate_destination()
        conn = self._connect()
        try:
            n = self._write_rows(conn, rows)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        ids = [str(l.get("lead_id")) for l in leads]
        return PushResult(target=self.target_id, attempted=len(leads), pushed=n,
                          gated=False, pushed_ids=ids, notes="pushed")

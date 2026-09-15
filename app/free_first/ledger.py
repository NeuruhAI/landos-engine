"""The FREE-FIRST ledger: one append-only, hash-chained JSONL file.

State is never stored; it is replayed. A job's whole history -- the ladder it was
given, every free-source attempt, every human step, every spend authorization,
every paid decision and settlement -- lives here, so a retry, a restart, a second
worker or an autonomous continuation reads exactly what the first worker wrote.

Nothing is rewritten. A retry appends a new attempt; a stale attempt stays on
record (it only stops counting toward exhaustion); a job re-opened by a later run
lands on the SAME job id (identity is tenant + fact + target + county, never a run
id), so no run can start a paid path from a clean slate. An edited, reordered or
truncated file refuses to open.

Many processes write one file, so every append takes an exclusive flock and
re-verifies the tail under it (two unlocked writers would fork the sequence).
"""
from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .provenance import canonical_json, sha256_hex, utc_now_iso

LEDGER_VERSION = "landos.free_first.ledger.v1"
GENESIS_SHA = sha256_hex("landos-free-first-ledger-genesis-v1")
ENTRY_KINDS = ("JOB_OPENED", "LADDER_EXTENDED", "ATTEMPT", "HUMAN_OUTCOME", "SPEND_AUTHORIZED",
               "PAID_DECISION", "PAID_SETTLED", "JOB_CLOSED")
ENTRY_FIELDS = frozenset({"version", "seq", "kind", "job_id", "at", "body", "prev_sha", "entry_sha"})


class LedgerCorrupt(RuntimeError):
    """The chain does not verify. There is no repair path; the file is evidence."""


class UnknownJob(KeyError):
    """No JOB_OPENED entry for this job id."""


def job_id_for(*, tenant_id: str, fact: str, target_key: str, county_fips: str) -> str:
    """Deterministic: the same question about the same target is always the same job."""
    return "ffj_" + sha256_hex({"tenant_id": tenant_id, "fact": fact, "target_key": target_key,
                                "county_fips": county_fips})[:24]


def _seal(entry: Mapping[str, Any]) -> str:
    return sha256_hex({k: v for k, v in entry.items() if k != "entry_sha"})


@dataclass
class JobState:
    job_id: str
    opened: Dict[str, Any]
    steps: List[Dict[str, Any]]
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    human_outcomes: List[Dict[str, Any]] = field(default_factory=list)
    authorizations: List[Dict[str, Any]] = field(default_factory=list)
    paid_decisions: List[Dict[str, Any]] = field(default_factory=list)
    settlements: List[Dict[str, Any]] = field(default_factory=list)
    closed: Optional[Dict[str, Any]] = None
    opened_entry_sha: str = ""

    @property
    def fact(self) -> str:
        return self.opened["fact"]

    @property
    def target_key(self) -> str:
        return self.opened["target"]["target_key"]

    @property
    def county_fips(self) -> str:
        return self.opened["jurisdiction"]["county_fips"]

    @property
    def state(self) -> str:
        return self.opened["jurisdiction"]["state"]

    def spent_usd(self, authorization_id: Optional[str] = None) -> float:
        return round(sum(float(s.get("cost_usd") or 0) for s in self.settlements
                         if authorization_id is None or s.get("authorization_id") == authorization_id), 6)

    def attempts_for(self, step_id: str) -> List[Dict[str, Any]]:
        return [a for a in self.attempts if a["step_id"] == step_id]

    def human_for(self, step_id: str) -> List[Dict[str, Any]]:
        return [h for h in self.human_outcomes if h["step_id"] == step_id]


class FreeFirstLedger:
    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._verify(self._read())

    # ------------------------------------------------------------------ chain
    def _read(self) -> List[Dict[str, Any]]:
        out = []
        with self.path.open(encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise LedgerCorrupt(f"{self.path}: line {n} is not JSON") from exc
        return out

    @staticmethod
    def _verify(entries: List[Dict[str, Any]]) -> tuple:
        seq, prev = 0, GENESIS_SHA
        for n, rec in enumerate(entries, 1):
            if set(rec) != ENTRY_FIELDS or rec.get("version") != LEDGER_VERSION:
                raise LedgerCorrupt(f"entry {n} has an invalid field set")
            if rec["seq"] != seq + 1:
                raise LedgerCorrupt(f"sequence break at entry {n}")
            if rec["prev_sha"] != prev:
                raise LedgerCorrupt(f"hash chain break at entry {n}")
            if _seal(rec) != rec["entry_sha"]:
                raise LedgerCorrupt(f"entry hash mismatch at entry {n}")
            if rec["kind"] not in ENTRY_KINDS:
                raise LedgerCorrupt(f"unknown entry kind {rec['kind']!r} at entry {n}")
            seq, prev = rec["seq"], rec["entry_sha"]
        return seq, prev

    def verify(self) -> Dict[str, Any]:
        seq, tip = self._verify(self._read())
        return {"version": LEDGER_VERSION, "entries": seq, "tail_sha": tip, "genesis_sha": GENESIS_SHA,
                "path": str(self.path)}

    def append(self, kind: str, job_id: str, body: Mapping[str, Any], *, at: Optional[str] = None) -> Dict[str, Any]:
        if kind not in ENTRY_KINDS:
            raise ValueError(f"unknown entry kind {kind!r}")
        json.loads(canonical_json(dict(body)))            # pure JSON or refuse
        with self.path.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                fh.seek(0)
                entries = [json.loads(line) for line in fh.read().splitlines() if line.strip()]
                seq, tip = self._verify(entries)
                entry = {"version": LEDGER_VERSION, "seq": seq + 1, "kind": kind, "job_id": job_id,
                         "at": at or utc_now_iso(), "body": dict(body), "prev_sha": tip}
                entry["entry_sha"] = _seal(entry)
                fh.seek(0, os.SEEK_END)
                fh.write(canonical_json(entry) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return entry

    def entries(self, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
        entries = self._read()
        self._verify(entries)
        return [e for e in entries if job_id is None or e["job_id"] == job_id]

    # ------------------------------------------------------------------ replay
    def job_state(self, job_id: str) -> JobState:
        state: Optional[JobState] = None
        for e in self.entries(job_id):
            kind, body = e["kind"], {**e["body"], "seq": e["seq"], "entry_sha": e["entry_sha"], "at": e["at"]}
            if kind == "JOB_OPENED":
                if state is None:
                    state = JobState(job_id, e["body"], list(e["body"]["ladder"]["steps"]),
                                     opened_entry_sha=e["entry_sha"])
                continue
            if state is None:
                raise LedgerCorrupt(f"{job_id}: {kind} before JOB_OPENED")
            if kind == "LADDER_EXTENDED":
                known = {s["step_id"] for s in state.steps}
                state.steps.extend(s for s in e["body"]["added_steps"] if s["step_id"] not in known)
            elif kind == "ATTEMPT":
                state.attempts.append(body)
            elif kind == "HUMAN_OUTCOME":
                state.human_outcomes.append(body)
            elif kind == "SPEND_AUTHORIZED":
                state.authorizations.append(body)
            elif kind == "PAID_DECISION":
                state.paid_decisions.append(body)
            elif kind == "PAID_SETTLED":
                state.settlements.append(body)
            elif kind == "JOB_CLOSED":
                state.closed = body
        if state is None:
            raise UnknownJob(job_id)
        return state

    def job_ids(self) -> List[str]:
        seen: List[str] = []
        for e in self._read():
            if e["kind"] == "JOB_OPENED" and e["job_id"] not in seen:
                seen.append(e["job_id"])
        return seen

    def has_job(self, job_id: str) -> bool:
        return any(e["kind"] == "JOB_OPENED" and e["job_id"] == job_id for e in self._read())


def iter_attempt_rows(ledger: FreeFirstLedger) -> Iterable[Dict[str, Any]]:
    """Flat attempt rows (for receipts, projections and the national read model)."""
    for e in ledger.entries():
        if e["kind"] == "ATTEMPT":
            yield {"job_id": e["job_id"], "seq": e["seq"], "entry_sha": e["entry_sha"], **e["body"]}

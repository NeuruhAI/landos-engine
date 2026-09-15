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
    """Append-only, hash-chained, flock-serialised JSONL.

    Opening the ledger and ``verify()`` re-verify the WHOLE chain from genesis. Between
    them the ledger keeps a verified view (entries, per-job index, byte offset, tip) and
    each read or append verifies only the bytes appended since, as a continuation of the
    verified tip -- so a national run is linear, not quadratic, in the ledger's length. If
    the verified prefix changed underneath (the file shrank, or the last verified line is
    no longer where it was) the whole chain is re-verified. A tampered entry can never be
    extended into a valid chain: its seal no longer matches, so the next full verification
    (every receipt calls ``verify()``) refuses the ledger."""

    def __init__(self, path: str | os.PathLike) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._reset()
        self._sync(full=True)

    # ------------------------------------------------------------------ chain
    def _reset(self) -> None:
        self._entries: List[Dict[str, Any]] = []
        self._by_job: Dict[str, List[Dict[str, Any]]] = {}
        self._offset, self._tail_line = 0, b""
        self._seq, self._tip = 0, GENESIS_SHA

    def _extend(self, data: bytes, base: int) -> None:
        """Verify and index complete lines of ``data`` (which starts at byte ``base``)."""
        end = data.rfind(b"\n") + 1                 # a writer mid-line is not yet an entry
        pos = 0
        for raw in data[:end].split(b"\n")[:-1]:
            line_end = pos + len(raw) + 1
            text = raw.decode("utf-8").strip()
            if text:
                try:
                    rec = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise LedgerCorrupt(f"{self.path}: entry {self._seq + 1} is not JSON") from exc
                self._seq, self._tip = self._verify([rec], start=(self._seq, self._tip))
                self._entries.append(rec)
                self._by_job.setdefault(rec["job_id"], []).append(rec)
                self._tail_line = raw + b"\n"
            pos = line_end
        self._offset = base + end

    def _sync(self, *, full: bool = False) -> None:
        with self.path.open("rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            if not full and self._offset and size >= self._offset:
                fh.seek(self._offset - len(self._tail_line))
                if fh.read(len(self._tail_line)) == self._tail_line:
                    if size > self._offset:
                        self._extend(fh.read(), self._offset)
                    return
            elif not full and not self._offset and size == 0:
                return
            fh.seek(0)
            data = fh.read()
        self._reset()
        self._extend(data, 0)

    def _read(self) -> List[Dict[str, Any]]:
        self._sync()
        return list(self._entries)

    @staticmethod
    def _verify(entries: List[Dict[str, Any]], start: tuple = (0, GENESIS_SHA)) -> tuple:
        seq, prev = start
        for rec in entries:
            n = seq + 1
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
        """FULL re-verification from genesis (receipts cite this)."""
        self._sync(full=True)
        return {"version": LEDGER_VERSION, "entries": self._seq, "tail_sha": self._tip, "genesis_sha": GENESIS_SHA,
                "path": str(self.path)}

    def tip(self) -> Dict[str, Any]:
        """The verified tip without a full re-verification (per-operation bookkeeping)."""
        self._sync()
        return {"version": LEDGER_VERSION, "entries": self._seq, "tail_sha": self._tip, "genesis_sha": GENESIS_SHA,
                "path": str(self.path)}

    def append(self, kind: str, job_id: str, body: Mapping[str, Any], *, at: Optional[str] = None) -> Dict[str, Any]:
        if kind not in ENTRY_KINDS:
            raise ValueError(f"unknown entry kind {kind!r}")
        json.loads(canonical_json(dict(body)))            # pure JSON or refuse
        with self.path.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                self._sync()                              # other writers' entries, verified, under the lock
                if os.fstat(fh.fileno()).st_size != self._offset:
                    # under the lock no writer is mid-line: trailing bytes are a writer that died mid-write
                    raise LedgerCorrupt(f"{self.path}: trailing partial entry after seq {self._seq}")
                entry = {"version": LEDGER_VERSION, "seq": self._seq + 1, "kind": kind, "job_id": job_id,
                         "at": at or utc_now_iso(), "body": dict(body), "prev_sha": self._tip}
                entry["entry_sha"] = _seal(entry)
                line = canonical_json(entry) + "\n"
                fh.seek(0, os.SEEK_END)
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
                self._extend(line.encode("utf-8"), self._offset)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return entry

    def entries(self, job_id: Optional[str] = None) -> List[Dict[str, Any]]:
        self._sync()
        return list(self._entries) if job_id is None else list(self._by_job.get(job_id, ()))

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
        self._sync()
        return [j for j, es in self._by_job.items() if any(e["kind"] == "JOB_OPENED" for e in es)]

    def has_job(self, job_id: str) -> bool:
        self._sync()
        return any(e["kind"] == "JOB_OPENED" for e in self._by_job.get(job_id, ()))


def iter_attempt_rows(ledger: FreeFirstLedger) -> Iterable[Dict[str, Any]]:
    """Flat attempt rows (for receipts, projections and the national read model)."""
    for e in ledger.entries():
        if e["kind"] == "ATTEMPT":
            yield {"job_id": e["job_id"], "seq": e["seq"], "entry_sha": e["entry_sha"], **e["body"]}

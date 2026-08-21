"""One look at one source, in the shape the product persists.

WHY THE ENGINE WRITES THESE. Before T50A the only durable record that a source
had been checked was a sentence the router happened to leave in
`next_source_why` -- "NCSBE_PUBLIC_VOTER_FILE has already been run against this
candidate and returned nothing" -- which the product had to parse back out with
a regular expression. That sentence exists for one source family, is overwritten
the moment the router re-routes, and cannot express a POSITIVE result at all.

T30A made the gap concrete. It drove the NC eCourts Portal by hand across nine
candidates and eight decedents. The negative results could be persisted, by
extending a CHECK constraint by one enum value. The positives -- an official
estate case actually located, with a file number -- had nowhere to go, so the
most expensive result the lane produced was the one it could not write down.

WHAT AN ATTEMPT IS NOT. It is not a claim: it says a source was consulted, not
what is now true about the property. It is not a party, a relationship or an
authority; an index search that found a case creates no people. And it is not a
routing decision -- `app.resolution.router` still owns what happens next.

THE ONE RULE THIS MODULE ENFORCES. A result that is about the INSTRUMENT rather
than the record cannot be written as negative evidence. `for_negative_evidence`
refuses, by construction, rather than by the caller remembering.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional, Tuple

from .research_result import (
    GENERIC_RESULT_STATES,
    generic_result_state,
    is_evidential,
    supports_replay_suppression_generic,
)

ATTEMPT_CONTRACT_VERSION = "case_source_attempt.v1"

#: What was looked up. An attempt against a source is meaningless without it:
#: searching a deaths index "for the candidate" is not a fact, searching it for
#: a NAMED PERSON is. It is also the unit of replay suppression -- one decedent
#: can own several parcels, and T30A's nine candidates were eight decedents.
SUBJECT_KINDS: Tuple[str, ...] = ("PROPERTY", "PARCEL", "PERSON", "ENTITY", "ESTATE", "CASE")

#: The case sections a result can belong to. Mirrors CASE_DOMAINS in
#: src/lib/case/vocabulary.ts.
DOMAINS: Tuple[str, ...] = (
    "IDENTITY", "TITLE", "AUTHORITY", "CONTACT", "RELATIONSHIP",
    "TAX_DEBT", "PHYSICAL", "MARKET", "RISK", "INTERNAL",
)

ACTOR_TYPES: Tuple[str, ...] = ("OPERATOR", "ENGINE", "VENDOR", "OFFICIAL")


class AttemptRefused(ValueError):
    """Raised when a result may not be written the way the caller asked."""


@dataclass(frozen=True)
class CaseSourceAttempt:
    """One row of public.case_source_attempt, constructed safely.

    Every field the table requires, validated at construction. A malformed
    attempt fails here rather than at the database, where the error is a
    constraint name and a row number.
    """

    candidate_id: str
    subject_kind: str
    subject_ref: str
    domain: str
    source_id: str
    source_name: str
    source_family: str
    result_state: str

    tenant_id: Optional[str] = None
    crm_lead_id: Optional[str] = None
    parcel_apn: Optional[str] = None
    subject_label: Optional[str] = None
    work_unit: Optional[str] = None
    procedure: Optional[str] = None
    procedure_version: Optional[str] = None
    query: Optional[str] = None
    actor: Optional[str] = None
    actor_type: str = "ENGINE"
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    notes: str = ""
    next_action: Optional[str] = None
    cost: Optional[float] = None
    currency: Optional[str] = None
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not (self.candidate_id or "").strip():
            raise AttemptRefused("an attempt has to belong to a case")
        if not (self.subject_ref or "").strip():
            raise AttemptRefused("an attempt has to say what was looked up")
        if self.subject_kind not in SUBJECT_KINDS:
            raise AttemptRefused(f"unknown subject kind {self.subject_kind!r}")
        if self.domain not in DOMAINS:
            raise AttemptRefused(f"unknown domain {self.domain!r}")
        if self.actor_type not in ACTOR_TYPES:
            raise AttemptRefused(f"unknown actor type {self.actor_type!r}")
        for name in ("source_id", "source_name", "source_family"):
            if not (getattr(self, name) or "").strip():
                raise AttemptRefused(f"an attempt needs a {name}")
        if generic_result_state(self.result_state) is None:
            raise AttemptRefused(
                f"unrecognised result state {self.result_state!r} -- refusing to guess"
            )
        if self.cost is not None:
            if self.cost < 0:
                raise AttemptRefused("cost cannot be negative")
            if self.cost > 0 and not (self.currency or "").strip():
                raise AttemptRefused("a cost needs a currency")

    # ── the guards ────────────────────────────────────────────────────────

    @property
    def generic_state(self) -> str:
        """The result in the product's vocabulary, whichever was supplied."""
        return generic_result_state(self.result_state) or "INCONCLUSIVE"

    @property
    def is_evidential(self) -> bool:
        """True when this says something about the subject."""
        return is_evidential(self.result_state)

    @property
    def suppresses_replay(self) -> bool:
        """True when the same search should not be run again."""
        return supports_replay_suppression_generic(self.result_state)

    def for_negative_evidence(self) -> "CaseSourceAttempt":
        """Assert this attempt may be written as evidence of ABSENCE.

        Raises rather than returning a flag, so a caller cannot proceed by
        ignoring a boolean. A challenge that interrupted a session and a source
        that refused a scripted client both fail here, which is the entire
        reason the method exists.
        """
        if not self.is_evidential:
            raise AttemptRefused(
                f"{self.result_state} is about the instrument, not the record; "
                "it may never be written as evidence about the subject"
            )
        if not self.suppresses_replay:
            raise AttemptRefused(
                f"{self.result_state} leaves the question open and does not "
                "support suppressing a replay"
            )
        return self

    # ── identity ──────────────────────────────────────────────────────────

    def natural_key(self) -> str:
        """This case, this subject, this source, this query.

        Matches the unique index on public.case_source_attempt, so a caller can
        tell a duplicate from a new fact before touching the database.
        """
        return "|".join([
            self.candidate_id.strip(),
            self.subject_kind,
            self.subject_ref.strip().upper(),
            self.source_id.strip().upper(),
            (self.query or "").strip().lower(),
        ])

    def idempotency_key(self) -> str:
        """The natural key on one UTC day.

        A double submit records one attempt. The same search genuinely re-run
        tomorrow records two, because a portal's answer changes and collapsing
        those would lose a real fact.
        """
        day = (self.started_at or "")[:10] or "unknown-day"
        return f"{self.natural_key()}|{day}"

    def digest(self) -> str:
        return hashlib.sha256(self.idempotency_key().encode("utf-8")).hexdigest()[:32]

    # ── output ────────────────────────────────────────────────────────────

    def to_row(self) -> Dict[str, object]:
        """The insert payload. Column names match the migration exactly."""
        row = asdict(self)
        row.pop("evidence_refs", None)
        row["result_state"] = self.generic_state
        return {k: v for k, v in row.items() if v is not None}

    def to_dict(self) -> Dict[str, object]:
        d = asdict(self)
        d["evidence_refs"] = list(self.evidence_refs)
        d["generic_state"] = self.generic_state
        d["is_evidential"] = self.is_evidential
        d["suppresses_replay"] = self.suppresses_replay
        d["idempotency_key"] = self.idempotency_key()
        return d


INSERT_SQL = """
insert into public.case_source_attempt
  (tenant_id, candidate_id, crm_lead_id, parcel_apn, subject_kind, subject_ref,
   subject_label, work_unit, domain, source_id, source_name, source_family,
   procedure, procedure_version, query, actor_type, started_at, completed_at,
   result_state, notes, next_action, cost, currency)
values
  (%(tenant_id)s, %(candidate_id)s, %(crm_lead_id)s, %(parcel_apn)s,
   %(subject_kind)s, %(subject_ref)s, %(subject_label)s, %(work_unit)s,
   %(domain)s, %(source_id)s, %(source_name)s, %(source_family)s,
   %(procedure)s, %(procedure_version)s, %(query)s, %(actor_type)s,
   %(started_at)s, %(completed_at)s, %(result_state)s, %(notes)s,
   %(next_action)s, %(cost)s, %(currency)s)
on conflict do nothing
returning attempt_id
"""


def insert_params(attempt: CaseSourceAttempt) -> Dict[str, object]:
    """Full parameter dict for INSERT_SQL, with absent fields as None.

    to_row() drops Nones so a receipt stays readable; a parameterised insert
    needs every key present, so this fills them back in.
    """
    keys = (
        "tenant_id", "candidate_id", "crm_lead_id", "parcel_apn", "subject_kind",
        "subject_ref", "subject_label", "work_unit", "domain", "source_id",
        "source_name", "source_family", "procedure", "procedure_version", "query",
        "actor_type", "started_at", "completed_at", "result_state", "notes",
        "next_action", "cost", "currency",
    )
    row = attempt.to_row()
    return {k: row.get(k) for k in keys}


def known_result_states() -> Tuple[str, ...]:
    return GENERIC_RESULT_STATES

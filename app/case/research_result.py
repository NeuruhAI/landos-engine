"""The generic research-result vocabulary, and the seam to this engine's own.

WHY TWO VOCABULARIES. `app.resolution.source_procedure.RESULT_STATES` is
estate-shaped, because it was learned by driving the NC eCourts Portal:
OFFICIAL_ESTATE_CASE_FOUND names an estate case. The product needs the same
distinctions for a deeds index, a permit portal, a soil survey and a tax
collector, so it carries a wider set. Neither vocabulary is wrong and neither
should be forced into the other; this module is the mapping between them, and
it is exhaustive by test rather than by intention.

WHAT THE GENERIC SET IS FOR. Five of its ten states are ways of getting
nothing, and that is the whole point. On the 120 Willow file the finding that
the county death index -- running back to 1914 -- holds no record for either
owner is what REFUTED the estate premise the file was opened on. A vocabulary
that cannot tell that from "nobody looked" cannot carry the most valuable thing
in the file.

THE GUARD THIS MODULE ENFORCES. A result that is about the INSTRUMENT rather
than the record may never become negative evidence about a subject. A
human-verification challenge that interrupted a session says nothing whatever
about whether an estate exists, and a source that refused a scripted client says
nothing about whether the record is there. `source_procedure.NON_EVIDENTIAL_STATES`
holds the two the engine can produce; this holds those plus the two the product
can, and `is_evidential` is the single question every writer must ask.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Optional, Tuple

from app.resolution.source_procedure import (
    NEGATIVE_EVIDENCE_STATES,
    NON_EVIDENTIAL_STATES,
    RESULT_STATES as ENGINE_RESULT_STATES,
)

RESEARCH_RESULT_CONTRACT_VERSION = "research_result.v1"

#: The vocabulary the DeedSonar product persists on case_source_attempt. Kept
#: byte-identical to src/lib/case/vocabulary.ts RESULT_STATES; a test asserts
#: the two lists have not drifted.
GENERIC_RESULT_STATES: Tuple[str, ...] = (
    #: The source answered the question for this subject.
    "FOUND",
    #: It answered part of it. What it did not answer is still open.
    "PARTIAL",
    #: Searched properly and holds nothing. THIS IS A RESULT.
    "NO_MATCH",
    #: A candidate record exists and nothing ties it to this subject yet.
    "AMBIGUOUS",
    #: The search ran and could not be scoped tightly enough to answer.
    "INCONCLUSIVE",
    #: Located; the document that would settle it is not published.
    "OFFICIAL_COPY_REQUIRED",
    #: This source needs a person -- a call, a counter, a portal a script may
    #: not drive.
    "OPERATOR_REQUIRED",
    #: The source refused access on its own terms or controls.
    "BLOCKED",
    #: This source cannot answer this question for this subject.
    "NOT_APPLICABLE",
    #: Deliberately not run yet, by decision rather than by obstacle.
    "DEFERRED",
)

#: States that say something about the INSTRUMENT and nothing about the subject.
#: Superset of source_procedure.NON_EVIDENTIAL_STATES: the engine has no
#: equivalent of BLOCKED or DEFERRED, and both are just as non-evidential.
NON_EVIDENTIAL_GENERIC_STATES: FrozenSet[str] = frozenset({
    "OPERATOR_REQUIRED",
    "BLOCKED",
    "INCONCLUSIVE",
    "DEFERRED",
})

#: The only states that support suppressing a replay of the same search.
#: Deliberately narrow: NO_MATCH means the index was searched and is empty,
#: NOT_APPLICABLE means it could never answer. Everything else leaves the
#: question open, and an open question is a reason to look again.
REPLAY_SUPPRESSING_GENERIC_STATES: FrozenSet[str] = frozenset({
    "NO_MATCH",
    "NOT_APPLICABLE",
})

#: The seam. Exhaustive over source_procedure.RESULT_STATES; a test fails if the
#: engine adds a state and this table does not.
ENGINE_TO_GENERIC: Dict[str, str] = {
    "OFFICIAL_ESTATE_CASE_FOUND": "FOUND",
    "PUBLIC_INDEX_NO_MATCH": "NO_MATCH",
    "AMBIGUOUS_MATCH": "AMBIGUOUS",
    "SEARCH_INCONCLUSIVE": "INCONCLUSIVE",
    "HUMAN_VERIFICATION_REQUIRED": "OPERATOR_REQUIRED",
    "OFFICIAL_COPY_REQUIRED": "OFFICIAL_COPY_REQUIRED",
}


def generic_result_state(state: Optional[str]) -> Optional[str]:
    """An engine state or a generic one, narrowed to the generic vocabulary.

    Returns None rather than guessing. An unrecognised state has to surface as
    unrecognised: the alternative is a CAPTCHA quietly becoming a fact about a
    decedent.
    """
    if not state:
        return None
    key = state.strip().upper()
    if key in GENERIC_RESULT_STATES:
        return key
    return ENGINE_TO_GENERIC.get(key)


def is_evidential(state: str) -> bool:
    """True when the result says something about the SUBJECT."""
    return generic_result_state(state) not in NON_EVIDENTIAL_GENERIC_STATES


def is_positive(state: str) -> bool:
    """True when the source produced something rather than taught us something."""
    return generic_result_state(state) in {"FOUND", "PARTIAL", "OFFICIAL_COPY_REQUIRED"}


def supports_replay_suppression_generic(state: str) -> bool:
    """True when recording this means the same search should not be re-run."""
    return generic_result_state(state) in REPLAY_SUPPRESSING_GENERIC_STATES


def audit() -> Dict[str, object]:
    """What the seam covers, for a receipt. Cheap and side-effect free."""
    return {
        "contract": RESEARCH_RESULT_CONTRACT_VERSION,
        "engine_states": sorted(ENGINE_RESULT_STATES),
        "generic_states": sorted(GENERIC_RESULT_STATES),
        "unmapped_engine_states": sorted(
            set(ENGINE_RESULT_STATES) - set(ENGINE_TO_GENERIC)
        ),
        # The engine's own guards, restated over the generic set, so a drift in
        # either direction shows up in a receipt rather than in production.
        "engine_non_evidential": sorted(NON_EVIDENTIAL_STATES),
        "generic_non_evidential": sorted(NON_EVIDENTIAL_GENERIC_STATES),
        "engine_negative_evidence": sorted(NEGATIVE_EVIDENCE_STATES),
        "generic_replay_suppressing": sorted(REPLAY_SUPPRESSING_GENERIC_STATES),
    }

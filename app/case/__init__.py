"""The property-case seam between this engine and the DeedSonar product.

`app.resolution` answers "who is the living party and what should be tried
next". This package answers a narrower and more mechanical question: how does a
result the engine produced get written down in the one place the product reads
it from -- `public.case_source_attempt` -- without either side having to know
the other's vocabulary.

Nothing here scores, qualifies or authorises anything.
"""

from .research_result import (
    RESEARCH_RESULT_CONTRACT_VERSION,
    GENERIC_RESULT_STATES,
    NON_EVIDENTIAL_GENERIC_STATES,
    REPLAY_SUPPRESSING_GENERIC_STATES,
    ENGINE_TO_GENERIC,
    generic_result_state,
    is_evidential,
    is_positive,
    supports_replay_suppression_generic,
)
from .attempt import CaseSourceAttempt, ATTEMPT_CONTRACT_VERSION

__all__ = [
    "RESEARCH_RESULT_CONTRACT_VERSION",
    "GENERIC_RESULT_STATES",
    "NON_EVIDENTIAL_GENERIC_STATES",
    "REPLAY_SUPPRESSING_GENERIC_STATES",
    "ENGINE_TO_GENERIC",
    "generic_result_state",
    "is_evidential",
    "is_positive",
    "supports_replay_suppression_generic",
    "CaseSourceAttempt",
    "ATTEMPT_CONTRACT_VERSION",
]

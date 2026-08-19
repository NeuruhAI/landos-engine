"""T21 Resolution Factory — property → living party → evidence-backed contact.

The contact modules in ``app.contact`` answer "did we find a phone?". This
package answers the question the founder actually asked: for every current NC
opportunity, WHO is the legitimate living party, HOW do we know, WHAT can an
operator safely do next, and WHICH source should be tried after that.

Nothing here places a call, sends a message, or authorizes one.
"""

from .contract import (
    RESOLUTION_CONTRACT_VERSION,
    ContactPoint,
    Evidence,
    Party,
    Relationship,
    ResolutionRecord,
)

__all__ = [
    "RESOLUTION_CONTRACT_VERSION",
    "ContactPoint",
    "Evidence",
    "Party",
    "Relationship",
    "ResolutionRecord",
]

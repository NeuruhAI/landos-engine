"""How to work a source, learned from actually working it.

WHY THIS EXISTS. `sources.py` says WHAT a source is -- who holds it, what it
costs, whether a script may touch it. `build_operator_manual_queue.py` says what
to bring to the counter for one candidate. Neither says HOW to drive the source:
which query to type first, what a false match looks like, what to do when the
site asks for a human, and which of several outcomes you actually got.

T30A learned that for the NC eCourts Portal by driving it, and every one of those
lessons cost something. The middle-name lesson cost eight would-be false
negatives. The county-scope lesson cost one wasted search window. Written down,
they cost the next lane nothing.

WHAT THIS IS NOT. Not a second memory system, not a router, not an agent. A
procedure is a versioned RECORD that points at the existing contracts:
`SourceEntry` for the source, the operator playbook for the counter work, and
`router.route_with_memory` for what a result does next.

VERSIONED ON PURPOSE. Portal behaviour changes. A procedure that cannot be
superseded becomes folklore, and folklore outlives the thing it described.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from typing import Dict, List, Tuple

SOURCE_PROCEDURE_CONTRACT_VERSION = "source_procedure.v1"

#: The outcome states a research pass may produce. Closed on purpose: the whole
#: failure this vocabulary prevents is collapsing five different situations into
#: "no match" and routing them all to the same dead end.
RESULT_STATES: Tuple[str, ...] = (
    #: An official case was located AND corroborated to this decedent.
    "OFFICIAL_ESTATE_CASE_FOUND",
    #: The index was properly searched and holds no matching case. A RESULT.
    "PUBLIC_INDEX_NO_MATCH",
    #: A case exists that might be this decedent and the evidence is short.
    "AMBIGUOUS_MATCH",
    #: The search ran but could not be scoped tightly enough to answer.
    "SEARCH_INCONCLUSIVE",
    #: A person must clear a challenge before research can continue.
    "HUMAN_VERIFICATION_REQUIRED",
    #: The case was found; the document that would settle authority is not public.
    "OFFICIAL_COPY_REQUIRED",
)

#: States that are ABOUT THE INSTRUMENT, not about the record. None of these may
#: ever be written as negative evidence about a decedent: a challenge that
#: interrupted a session says nothing whatever about whether an estate exists.
NON_EVIDENTIAL_STATES = frozenset({
    "HUMAN_VERIFICATION_REQUIRED",
    "SEARCH_INCONCLUSIVE",
})

#: Only these support suppressing a replay of the same search.
NEGATIVE_EVIDENCE_STATES = frozenset({"PUBLIC_INDEX_NO_MATCH"})


@dataclass(frozen=True)
class SourceProcedure:
    """One versioned, provenance-carrying way to work one source."""
    name: str
    version: str
    source_family: str
    domain: str
    taught_by: str                     # the court that measured it
    observed_at: str
    trigger: str
    inputs: Tuple[str, ...]
    precheck: Tuple[str, ...]
    steps: Tuple[str, ...]
    match_standard: Tuple[str, ...]
    false_match_examples: Tuple[str, ...]
    result_states: Tuple[str, ...]
    evidence_law: Tuple[str, ...]
    human_boundary: str
    limitations: Tuple[str, ...]
    last_verified_behavior: str

    def to_dict(self) -> dict:
        return asdict(self)


NC_ECOURTS_ESTATE_SMART_SEARCH_V1 = SourceProcedure(
    name="NC_ECOURTS_ESTATE_SMART_SEARCH",
    version="1.0.0",
    source_family="PROBATE_INDEX",
    domain="Estate / probate / decedent authority resolution",
    taught_by="T30A Grade-A estate authority court",
    observed_at="2026-08-20",

    trigger=(
        "owner_condition is DECEASED, HEIRS_UNRESOLVED or ESTATE_OPEN, AND no "
        "relationship on the candidate carries relationship_class=LEGAL_AUTHORITY"
    ),
    inputs=(
        "candidate_id", "county + county code", "recorded owner string, raw",
        "parsed decedent from owner_identity.parse_owner_name",
        "name variants, primary reading first",
        "known living parties and their relationship_class",
        "prior source attempts", "prior negative results and their dates",
    ),
    precheck=(
        "Has this DECEDENT (not merely this candidate) already been searched? "
        "One decedent can own several parcels -- T30A's nine candidates were "
        "eight decedents, and two pairs shared an estate.",
        "Was the prior search county-scoped correctly, and was the scope VERIFIED "
        "rather than assumed?",
        "Which name variants were actually attempted? A no-match on one variant "
        "is not a no-match on the identity.",
        "Is a recorded PUBLIC_INDEX_NO_MATCH recent enough to suppress a replay?",
        "Is an official case already linked to this candidate? Then stop.",
    ),
    steps=(
        "Open the Portal in an ordinary browser. Never script it: scripted "
        "clients get HTTP 403 and defeating that control is forbidden.",
        "If a human-verification challenge appears, STOP and hand it to the "
        "operator. Never solve it. Preserve candidate, decedent, query and stage "
        "so research RESUMES rather than restarts.",
        "Before trusting any county-scoped result: open Advanced Filtering "
        "Options, UNCHECK 'All Locations (default)' FIRST, then check the county, "
        "then VISUALLY CONFIRM both states. Checking a county while All Locations "
        "remains checked is a silent no-op -- T30A ran a whole search believing it "
        "was scoped to one county and got results from four others.",
        "Search 'LAST, FIRST' first. NOT the full middle name: Smart Search "
        "matches the middle token literally. T30A measured this -- one "
        "decedent's 'LAST, FIRST MIDDLE' returned ZERO cases while 'LAST, "
        "FIRST' returned thirty-two, including the right county's records. "
        "Starting with the full name would have produced eight false "
        "negatives across the cohort.",
        "Narrow with 'LAST, FIRST M' or the full middle name only when volume is "
        "unusable. Widening loses recall; narrowing loses nothing you had.",
        "If the grouped result set hits the 200-case cap, the answer is not "
        "'no match' -- it is SEARCH_INCONCLUSIVE until scoped.",
        "Raise items-per-page rather than paging: the estate ('E') cases are "
        "scattered through the set and a per-page read will miss them.",
        "Open the matching case's Register of Actions for party ROLES. The index "
        "row alone does not say who is executor.",
        "Record every query variant actually attempted, with its scope.",
    ),
    match_standard=(
        "Full name agreement, including middle name where the roll carries one.",
        "The case's county is the PROPERTY's county, or a documented link "
        "explains why it is not.",
        "Case type is an estate ('E') proceeding for THIS decedent -- not a case "
        "on which the decedent is merely a party.",
        "Corroboration from an independent record where available (another "
        "county court record for the same full name, a matching DOB).",
    ),
    false_match_examples=(
        "An estate for the same surname and a near-identical given name in a "
        "different county. T30A rejected exactly this: an estate for a woman "
        "whose given name differed from the target by one letter, filed roughly "
        "forty miles away, with nothing tying it to the parcel. Same surname + "
        "similar first name + an estate existing is THREE coincidences, not "
        "evidence.",
        "The decedent appearing as a PARTY on somebody else's estate. T30A saw "
        "two of these. It is a family link and it is not this decedent's estate.",
        "A shortened given name treated as the same person as a fuller one "
        "('ARCHIE' vs 'ARCHIE BRANDON') on circumstantial grounds alone.",
    ),
    result_states=RESULT_STATES,
    evidence_law=(
        "obituary -> FAMILY evidence",
        "voter household / same address -> FAMILY or HOUSEHOLD evidence",
        "tax roll printing 'EXECUTOR' -> ROLE_STATED_ON_RECORD, a recorded label",
        "clerk appointment / letters testamentary / letters of administration "
        "-> LEGAL_AUTHORITY, and nothing else earns it",
        "A case being FOUND does not create authority. T30A found a real Union "
        "County estate whose only party was the decedent: a small-estate "
        "affidavit appoints no personal representative, so there was none to "
        "find -- not merely none published.",
    ),
    human_boundary=(
        "A human-verification challenge is HUMAN_INTERACTION_REQUIRED. It is not "
        "a source failure, not a no-match, and not permanent. It must never be "
        "solved by the agent, and it must never be written as negative evidence "
        "about a decedent."
    ),
    limitations=(
        "eCourts coverage begins at each county's go-live date; older estates may "
        "exist only in the county's own index. A PUBLIC_INDEX_NO_MATCH therefore "
        "never establishes that no estate exists.",
        "The Register of Actions rendered for some cases and hung for others in "
        "the same session. A view that will not load is PORTAL_BLOCKED for that "
        "case, not an absence of parties.",
        "Estate documents were not downloadable anonymously in any case T30A "
        "opened; settling authority may need an official copy request.",
    ),
    last_verified_behavior=(
        "2026-08-20: Portal home, Smart Search and Register of Actions all load "
        "in an ordinary browser. Scripted clients receive HTTP 403. Tyler's own "
        "notice states the human-verification challenge re-arms every ten minutes "
        "for anonymous users; observed to do so mid-session."
    ),
)

PROCEDURES: Dict[str, SourceProcedure] = {
    NC_ECOURTS_ESTATE_SMART_SEARCH_V1.source_family:
        NC_ECOURTS_ESTATE_SMART_SEARCH_V1,
}


def procedure_for(source_family: str) -> SourceProcedure | None:
    """The current procedure for a source family, or None if nobody has learned
    one yet. None means 'undocumented', never 'do whatever you like'."""
    return PROCEDURES.get((source_family or "").strip().upper())


def supports_replay_suppression(result_state: str) -> bool:
    """May this outcome stop the same search running again?

    Only a real negative may. An interrupted or unscoped session says nothing
    about the record, and letting it suppress a replay would bury the candidate
    on the strength of a CAPTCHA.
    """
    return (result_state or "").strip().upper() in NEGATIVE_EVIDENCE_STATES

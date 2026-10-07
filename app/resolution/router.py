"""NEXT BEST SOURCE: for every unresolved candidate, what to try next and why.

The engine already knows why a candidate failed. What it never said is what to
DO about it, in a form an operator can act on: which office, what input to
bring, what comes back, and what is in the way. A "no phone" bucket with 19,797
rows in it is not a work list. This turns it into one.

The route is chosen from OWNER CONDITION, not from the failure message. A
deceased owner needs an estate file whether the matcher rejected the name for
being common or never attempted it at all -- the remedy is a property of who
owns the parcel, not of how the last attempt failed.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from .sources import NC_COUNTIES

ROUTER_VERSION = "next_best_source.v3"


@dataclass(frozen=True)
class NextSource:
    next_source: str
    why: str
    required_input: str
    cost_usd: str
    manual_or_automatic: str
    expected_information: str
    blocking_reason: str
    queue: str

    def to_dict(self) -> dict:
        return asdict(self)


# The terminal states. Naming them stops the router inventing a route that does
# not exist just to avoid an empty cell.
NONE_FREE = "NONE_FREE"
NOT_A_SELLER = "NOT_A_SELLER"

# Operator queues. Each is a real desk of work, not a status label.
Q_CONTACT_HIGH = "CONTACT_FOUND_HIGH"
Q_ESTATE_ID_REQUIRED = "ESTATE_IDENTITY_REQUIRED"
Q_ESTATE_ID_FOUND = "ESTATE_IDENTITY_FOUND_CONTACT_NEEDED"
#: The public estate index was actually SEARCHED and the case is not in it.
#: This is not the same desk as "nobody has fetched the estate file yet", and
#: sending it to that desk wastes the trip -- the operator arrives to collect a
#: file the statewide index says does not exist.
Q_ESTATE_NO_PUBLIC_CASE = "ESTATE_NO_PUBLIC_CASE_FOUND"
Q_SOS_ID_REQUIRED = "SOS_ENTITY_IDENTITY_REQUIRED"
Q_ENTITY_PRINCIPAL_FOUND = "ENTITY_PRINCIPAL_FOUND_CONTACT_NEEDED"
Q_TRUSTEE_REQUIRED = "TRUSTEE_REQUIRED"
Q_TRUSTEE_FOUND = "TRUSTEE_FOUND_CONTACT_NEEDED"
Q_TIER_C_REVIEW = "TIER_C_REVIEW"
Q_FREE_EXHAUSTED = "FREE_ROUTES_EXHAUSTED"
Q_NO_SAFE_MATCH = "NO_SAFE_MATCH"
Q_NOT_A_SELLER = "NOT_A_SELLER_CONTACT"
# v3. Two arrivals the entity route can now reach, because entity resolution
# happens once per ENTITY and fans out to every parcel it owns.
Q_ENTITY_ID_FOUND = "ENTITY_IDENTITY_FOUND_CONTACT_NEEDED"
# v3. The route the router used to hide inside "free routes exhausted": a source
# exists, it is legitimate, and it cannot be reached by a script.
Q_MANUAL_SOURCE = "MANUAL_SOURCE_REQUIRED"

OPERATOR_QUEUES = (
    Q_CONTACT_HIGH,
    Q_ESTATE_ID_REQUIRED,
    Q_ESTATE_ID_FOUND,
    Q_SOS_ID_REQUIRED,
    Q_ENTITY_PRINCIPAL_FOUND,
    Q_TRUSTEE_REQUIRED,
    Q_TRUSTEE_FOUND,
    Q_TIER_C_REVIEW,
    Q_FREE_EXHAUSTED,
    Q_NO_SAFE_MATCH,
    Q_NOT_A_SELLER,
)

# Contact status as the product should say it, one per queue. This is the
# vocabulary the DeedSonar contact panel renders.
QUEUE_CONTACT_STATUS = {
    Q_CONTACT_HIGH: "HIGH-CONFIDENCE CONTACT FOUND",
    Q_ESTATE_ID_REQUIRED: "NEEDS ESTATE IDENTITY",
    Q_ESTATE_NO_PUBLIC_CASE: "NO PUBLIC ESTATE CASE FOUND",
    Q_ESTATE_ID_FOUND: "IDENTITY RESOLVED — NO PHONE",
    Q_SOS_ID_REQUIRED: "NEEDS SOS / ENTITY IDENTITY",
    Q_ENTITY_PRINCIPAL_FOUND: "IDENTITY RESOLVED — NO PHONE",
    # v3 arrivals. Without these two, the route refresh wrote a blank status for
    # every row it moved here while every other queue carried a label.
    Q_ENTITY_ID_FOUND: "ENTITY IDENTIFIED — NO PHONE",
    Q_MANUAL_SOURCE: "MANUAL SOURCE REQUIRED",
    Q_TRUSTEE_REQUIRED: "NEEDS TRUSTEE IDENTITY",
    Q_TRUSTEE_FOUND: "TRUSTEE IDENTIFIED",
    Q_TIER_C_REVIEW: "TIER_C REVIEW",
    Q_FREE_EXHAUSTED: "FREE ROUTES EXHAUSTED",
    Q_NO_SAFE_MATCH: "NO SAFE MATCH",
    Q_NOT_A_SELLER: "NOT A SELLER CONTACT",
}


def _estate_route(county: str, has_living_identity: bool,
                  estate_public_index_searched: bool = False) -> NextSource:
    office = ("Mecklenburg County Clerk of Superior Court - Estates Division "
              "(Mecklenburg.Estates@nccourts.org for filings before 2023-10-09)"
              if county == "Mecklenburg"
              else f"{county} County Clerk of Superior Court - Estates Division")
    if not has_living_identity and estate_public_index_searched:
        # Somebody has already put this decedent through the public eCourts
        # index and it came back with no matching estate case. "Go and fetch
        # the estate file" is then the wrong instruction: the operator would
        # travel to collect a file the statewide index says is not there.
        #
        # What this does NOT assert: that no estate exists. eCourts coverage is
        # not complete for older filings, and the index cannot see a file that
        # was never opened. Both remain possible and the route says so rather
        # than choosing one.
        return NextSource(
            next_source="COUNTY_ESTATE_INDEX_OFFLINE / RECORDED_DEED",
            why="The public eCourts index was searched for this decedent under the "
                "tested identity variants and returned no matching estate case. "
                "That rules out the fastest route, not the estate: eCourts does "
                "not carry every older filing, and an estate may never have been "
                "opened at all. Reading the county's own estate index and the "
                "recorded deed chain is what separates those two.",
            required_input=f"decedent name + APN, taken to {office} for the county's "
                           f"own estate index, and to the Register of Deeds for the "
                           f"chain of title",
            cost_usd="0.00 (copy fees may apply)",
            manual_or_automatic="MANUAL",
            expected_information="whether an estate file exists in the county index "
                                 "but not in eCourts; and the last recorded "
                                 "instrument with its named grantees",
            blocking_reason="",
            queue=Q_ESTATE_NO_PUBLIC_CASE,
        )
    if has_living_identity:
        return NextSource(
            next_source="NCSBE_PUBLIC_VOTER_FILE",
            why="A living representative or heir is already named. The remaining "
                "step is a contact for THAT person, not another estate lookup.",
            required_input="the resolved living person's name + county",
            cost_usd="0.00",
            manual_or_automatic="AUTOMATIC",
            expected_information="phone where the county's voter file carries one; "
                                 "residence address otherwise",
            blocking_reason="",
            queue=Q_ESTATE_ID_FOUND,
        )
    return NextSource(
        next_source="ESTATE_IMPORT",
        why="The recorded owner is deceased or the roll names only heirs. No phone "
            "vendor can supply a personal representative's NAME; only the estate "
            "file can. This is the blocker, and it is free.",
        required_input=f"decedent name + APN, taken to {office}",
        cost_usd="0.00 (copy fees may apply)",
        manual_or_automatic="MANUAL",
        expected_information="estate case number; applicant; personal representative / "
                             "executor / administrator; named heirs; mailing addresses",
        blocking_reason="No operator estate file has been supplied yet. "
                        "www.nccourts.gov and the eCourts Portal refuse scripted "
                        "clients, and the Portal's public search additionally sits "
                        "behind a human-verification challenge that re-arms every "
                        "ten minutes -- so the lookup is a person's job in a "
                        "browser, not an automated one.",
        queue=Q_ESTATE_ID_REQUIRED,
    )


def _entity_route(owner_class: str, county: str, has_living_identity: bool) -> NextSource:
    if has_living_identity:
        return NextSource(
            next_source="NCSBE_PUBLIC_VOTER_FILE",
            why="An officer, manager or principal is already identified. Route that "
                "PERSON through the free cascade. The entity's own line is not a "
                "personal contact and is never relabelled as one.",
            required_input="the verified principal's name + county",
            cost_usd="0.00",
            manual_or_automatic="AUTOMATIC",
            expected_information="personal phone where the voter file carries one",
            blocking_reason="",
            queue=Q_ENTITY_PRINCIPAL_FOUND,
        )
    if owner_class == "NONPROFIT":
        return NextSource(
            next_source="SOS_IMPORT (Non-Profits in each county)",
            why="Nonprofits are the one entity slice with a genuinely free, "
                "genuinely per-county official file. The IRS EO BMF is already "
                "exhausted for this cohort; the SOS county report is not.",
            required_input=f"one click: '{county} (Excel)' on the SOS Non-Profits report",
            cost_usd="0.00",
            manual_or_automatic="MANUAL",
            expected_information="registered nonprofit corporations in the county with "
                                 "published status and address",
            blocking_reason="Download is an interactive POST carrying an anti-forgery "
                            "token; one operator click per county, never scripted.",
            queue=Q_SOS_ID_REQUIRED,
        )
    return NextSource(
        next_source="SOS_IMPORT",
        why="An LLC or corporation cannot be voter-matched. Its controlling human "
            "has to come from the business registry first.",
        required_input="entity legal name -> NC SOS Business Registration Search",
        cost_usd="0.00 interactive / PAID for bulk",
        manual_or_automatic="MANUAL",
        expected_information="SOSID; status; formation date; registered agent; "
                             "principal office; company officials",
        blocking_reason="NC SOS forbids automated or scripted searching, and the only "
                        "bulk feed is a paid Data Subscription, which this lane does "
                        "not buy.",
        queue=Q_SOS_ID_REQUIRED,
    )


def _trust_route(state: str, has_living_identity: bool) -> NextSource:
    if has_living_identity:
        return NextSource(
            next_source="NCSBE_PUBLIC_VOTER_FILE",
            why="The roll names a trustee and that trustee is identified. The trustee "
                "is the party who can convey; route them through the free cascade.",
            required_input="trustee name + county",
            cost_usd="0.00",
            manual_or_automatic="AUTOMATIC",
            expected_information="trustee personal phone where the county carries one",
            blocking_reason="",
            queue=Q_TRUSTEE_FOUND,
        )
    if state == "TRUST_EXCLUDED_FROM_VOTER_PERSON_MATCH":
        return NextSource(
            next_source="NCSBE_PUBLIC_VOTER_FILE",
            why="A trustee IS named on the roll but no address-corroborated match was "
                "accepted. Corroboration, not a new source, is what is missing.",
            required_input="trustee name + property/mailing address",
            cost_usd="0.00",
            manual_or_automatic="AUTOMATIC",
            expected_information="an accepted identity once a second field corroborates",
            blocking_reason="No address-corroborated match at current thresholds. "
                            "Thresholds are not lowered to manufacture a match.",
            queue=Q_TRUSTEE_REQUIRED,
        )
    return NextSource(
        next_source="ESTATE_IMPORT / RECORDED_DEED",
        why="The roll names no trustee at all. A trust NAME never yields a person; "
            "the trustee has to be read off the instrument.",
        required_input="trust name + APN -> recorded deed or trust instrument",
        cost_usd="0.00",
        manual_or_automatic="MANUAL",
        expected_information="the trustee of record",
        blocking_reason="No trustee is named in the tax roll segment for this parcel.",
        queue=Q_TRUSTEE_REQUIRED,
    )


def _route_for(
    *,
    owner_class: str,
    owner_condition: str,
    contact_resolution_state: str,
    county: str = "",
    has_living_identity: bool = False,
    has_high_phone: bool = False,
    tier_c_held: bool = False,
    tier_c_certified: bool = False,
    attempted_sources: Optional[frozenset] = None,
    entity_resolution_state: str = "",
    entity_principal_known: bool = False,
    estate_public_index_searched: bool = False,
) -> NextSource:
    """The single decision. Owner condition first, failure message second.

    v3 adds three inputs and no new vocabulary of its own:

      attempted_sources        what has already been run against this candidate.
                               A router that keeps recommending a source that
                               has already returned nothing is not routing, it
                               is looping, and the operator pays for it.
      entity_resolution_state  entity research happens once per ENTITY and fans
                               out. A parcel whose owner has already been
                               resolved should not be sent to the SOS a second
                               time on behalf of the same LLC.
      entity_principal_known   whether that resolution actually named a human.

    T30A adds one more, for the same reason attempted_sources exists:

      estate_public_index_searched
                               whether a person has already put this decedent
                               through the public eCourts index. A no-match
                               there is a RESULT, and it sends the candidate to
                               a different desk than one nobody has looked up.
    """
    attempted = attempted_sources or frozenset()

    if has_high_phone:
        return NextSource(
            next_source=NONE_FREE,
            why="A HIGH-confidence contact already exists for this candidate.",
            required_input="",
            cost_usd="0.00",
            manual_or_automatic="AUTOMATIC",
            expected_information="",
            blocking_reason="",
            queue=Q_CONTACT_HIGH,
        )

    if owner_class == "GOVERNMENT" or owner_condition == "GOVERNMENT":
        return NextSource(
            next_source=NOT_A_SELLER,
            why="A government body is not a seller contact. Pursuing it would be "
                "effort spent on a party that cannot transact this way.",
            required_input="",
            cost_usd="0.00",
            manual_or_automatic="MANUAL",
            expected_information="",
            blocking_reason="Out of scope by owner class, not by data availability.",
            queue=Q_NOT_A_SELLER,
        )

    if tier_c_held:
        return NextSource(
            next_source="PRECISION_COURT" + (" (certified, not armed)" if tier_c_certified else ""),
            why=("This row is inside the certified TIER_C subcondition: precision 1.0000 "
                 "against two independent arbiters. It is held because arming the rule "
                 "would couple the matcher to arbiter sources it does not load."
                 if tier_c_certified else
                 "A candidate match exists but its subcondition has not met the "
                 "precision contract. Held is the correct answer, not a bug."),
            required_input="operator adjudication, or an armed versioned promotion rule",
            cost_usd="0.00",
            manual_or_automatic="MANUAL",
            expected_information="promotion to HIGH, or a recorded refusal",
            blocking_reason="TIER_C stays held until precision >= 0.98 on n >= 40 AND "
                            "the arbiter evidence is loadable at match time.",
            queue=Q_TIER_C_REVIEW,
        )

    if owner_condition in {"DECEASED", "ESTATE_OPEN", "HEIRS_UNRESOLVED"} or \
       owner_class in {"DECEASED", "ESTATE", "HEIRS"}:
        return _estate_route(county or "the", has_living_identity,
                             estate_public_index_searched)

    if owner_class == "TRUST" or owner_condition == "TRUST_HELD":
        return _trust_route(contact_resolution_state, has_living_identity)

    # v3: the entity has already been researched. Say what THAT produced rather
    # than sending the operator back to a search someone has already run.
    if entity_resolution_state and entity_resolution_state not in {
            "ENTITY_IDENTITY_REQUIRED", "ENTITY_REVIEW"}:
        if entity_principal_known:
            return NextSource(
                next_source="OPERATOR_CONTACT_ENTITY_PRINCIPAL",
                why="An official registry already names a principal for this entity, "
                    "resolved once and shared by every parcel it owns.",
                required_input="the entity record already on file",
                cost_usd="0.00",
                manual_or_automatic="MANUAL",
                expected_information="a natural person to approach on behalf of the entity",
                blocking_reason="",
                queue=Q_ENTITY_PRINCIPAL_FOUND,
            )
        if entity_resolution_state == "ENTITY_OFFICIAL_ADDRESS_FOUND":
            return NextSource(
                next_source="NC_SOS_ENTITY_DETAIL_MANUAL",
                why="The entity's official mailing address is known from the county "
                    "roll; the registry record that names a human is not. That step "
                    "is one lookup for the entity, not one per parcel.",
                required_input="entity legal name",
                cost_usd="0.00",
                manual_or_automatic="MANUAL",
                expected_information="registered agent, officers, managers, members",
                blocking_reason="NC SOS entity search prohibits automated querying.",
                queue=Q_ENTITY_ID_FOUND,
            )

    if owner_class in {"LLC", "CORPORATION", "OTHER_ENTITY", "NONPROFIT"} or \
       owner_condition in {"ENTITY_ACTIVE", "ENTITY_STATUS_UNKNOWN"}:
        return _entity_route(owner_class, county or "the county", has_living_identity)

    # Living people from here down.
    if contact_resolution_state == "IDENTITY_MATCHED_VOTER_PHONE_ABSENT":
        return NextSource(
            next_source=NONE_FREE,
            why="The person is identified. The county's voter file simply carries no "
                "phone for them. This is a source-coverage fact, not a matching "
                "failure, and no free NC route fills it.",
            required_input="",
            cost_usd="0.00",
            manual_or_automatic="AUTOMATIC",
            expected_information="",
            blocking_reason=(
                "Wake County's voter file has 0.0% phone fill; a matched Wake identity "
                "can never yield a phone from this source."
                if county == "Wake" else
                "The matched voter record has no phone field populated."),
            queue=Q_FREE_EXHAUSTED,
        )

    if contact_resolution_state in {"AMBIGUOUS_REJECTED", "COMMON_NAME_REJECTED"}:
        return NextSource(
            next_source="NCSBE_PUBLIC_VOTER_FILE",
            why="A match was found and refused for want of a second corroborating "
                "field. The refusal is correct; the fix is more evidence, never a "
                "lower threshold.",
            required_input="a corroborating mailing or residence address",
            cost_usd="0.00",
            manual_or_automatic="AUTOMATIC",
            expected_information="an accepted identity once corroborated",
            blocking_reason="Rejected as ambiguous or common-name at current thresholds.",
            queue=Q_NO_SAFE_MATCH,
        )

    if contact_resolution_state == "NO_ADDRESS_CORROBORATED_NCSBE_MATCH":
        return NextSource(
            next_source="NCSBE_PUBLIC_VOTER_FILE",
            why="No address-corroborated voter match exists for this owner. Address "
                "normalization improvements are what move this population -- T20D "
                "moved 21 candidates that way without touching a threshold.",
            required_input="improved property/mailing address normalization",
            cost_usd="0.00",
            manual_or_automatic="AUTOMATIC",
            expected_information="an accepted identity, then a phone where fill exists",
            blocking_reason="No corroborated match at current normalization.",
            queue=Q_NO_SAFE_MATCH,
        )

    return NextSource(
        next_source="TRIAGE",
        why="The owner string has not been classified into a route class, so no "
            "source can be chosen honestly.",
        required_input="owner name triage",
        cost_usd="0.00",
        manual_or_automatic="MANUAL",
        expected_information="an owner class, then a route",
        blocking_reason="Owner class UNKNOWN.",
        queue=Q_NO_SAFE_MATCH,
    )


# Sources that produce nothing new on a second automated pass against the same
# candidate. A manual source is NOT here: a human reading a probate file after a
# script failed is a different act, not a repeat of the same one.
_EXHAUSTED_ON_REPEAT = frozenset({
    "NCSBE_PUBLIC_VOTER_FILE",
    "IRS_EO_BMF_NC",
    "IRS_FORM_990_PUBLIC",
})


def route_with_memory(
    route: NextSource,
    attempted_sources: Optional[frozenset] = None,
) -> NextSource:
    """Never recommend an automated source that has already returned nothing.

    The v1 router chose from owner condition alone, so a candidate that had been
    through the voter file three times was told to try the voter file. That is
    not a route, it is a loop, and the operator is the one who pays for it.

    A source is only demoted when it is AUTOMATIC and in the exhausted set. A
    manual route is left alone: "read the probate file" does not become wrong
    because a script could not do it.
    """
    attempted = attempted_sources or frozenset()
    if route.next_source not in attempted:
        return route
    if route.manual_or_automatic != "AUTOMATIC" or route.next_source not in _EXHAUSTED_ON_REPEAT:
        return route
    return NextSource(
        next_source="MANUAL_PUBLIC_RECORD",
        why=f"{route.next_source} has already been run against this candidate and "
            f"returned nothing. Re-running it is not a route. {route.why}",
        required_input=route.required_input or "operator research against an official record",
        cost_usd="0.00",
        manual_or_automatic="MANUAL",
        expected_information=route.expected_information,
        blocking_reason=f"Automated source {route.next_source} is exhausted for this "
                        f"candidate.",
        queue=Q_MANUAL_SOURCE,
    )


NEXT_SOURCE_COLUMNS = [
    "candidate_id", "county", "apn", "owner_name", "owner_class", "owner_condition",
    "qualification_state", "primary_recipe", "score", "global_rank",
    "contact_resolution_state", "has_living_identity", "phone_present",
    "operator_queue", "contact_status",
    "next_source", "why", "required_input", "cost_usd", "manual_or_automatic",
    "expected_information", "blocking_reason",
]


def next_best_source(**kwargs) -> NextSource:
    """The public entry point: choose a route, then apply what has been tried.

    Split from _route_for so the memory rule cannot be bypassed by a return path
    that forgot about it -- there is exactly one place the decision leaves.
    """
    attempted = kwargs.get("attempted_sources")
    return route_with_memory(_route_for(**kwargs), attempted)

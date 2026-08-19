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

ROUTER_VERSION = "next_best_source.v1"


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
Q_SOS_ID_REQUIRED = "SOS_ENTITY_IDENTITY_REQUIRED"
Q_ENTITY_PRINCIPAL_FOUND = "ENTITY_PRINCIPAL_FOUND_CONTACT_NEEDED"
Q_TRUSTEE_REQUIRED = "TRUSTEE_REQUIRED"
Q_TRUSTEE_FOUND = "TRUSTEE_FOUND_CONTACT_NEEDED"
Q_TIER_C_REVIEW = "TIER_C_REVIEW"
Q_FREE_EXHAUSTED = "FREE_ROUTES_EXHAUSTED"
Q_NO_SAFE_MATCH = "NO_SAFE_MATCH"
Q_NOT_A_SELLER = "NOT_A_SELLER_CONTACT"

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
    Q_ESTATE_ID_FOUND: "IDENTITY RESOLVED — NO PHONE",
    Q_SOS_ID_REQUIRED: "NEEDS SOS / ENTITY IDENTITY",
    Q_ENTITY_PRINCIPAL_FOUND: "IDENTITY RESOLVED — NO PHONE",
    Q_TRUSTEE_REQUIRED: "NEEDS TRUSTEE IDENTITY",
    Q_TRUSTEE_FOUND: "TRUSTEE IDENTIFIED",
    Q_TIER_C_REVIEW: "TIER_C REVIEW",
    Q_FREE_EXHAUSTED: "FREE ROUTES EXHAUSTED",
    Q_NO_SAFE_MATCH: "NO SAFE MATCH",
    Q_NOT_A_SELLER: "NOT A SELLER CONTACT",
}


def _estate_route(county: str, has_living_identity: bool) -> NextSource:
    office = ("Mecklenburg County Clerk of Superior Court - Estates Division "
              "(Mecklenburg.Estates@nccourts.org for filings before 2023-10-09)"
              if county == "Mecklenburg"
              else f"{county} County Clerk of Superior Court - Estates Division")
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
        blocking_reason="No operator estate file has been supplied yet; "
                        "www.nccourts.gov and the eCourts Portal both refuse "
                        "programmatic clients, so this cannot be automated.",
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


def next_best_source(
    *,
    owner_class: str,
    owner_condition: str,
    contact_resolution_state: str,
    county: str = "",
    has_living_identity: bool = False,
    has_high_phone: bool = False,
    tier_c_held: bool = False,
    tier_c_certified: bool = False,
) -> NextSource:
    """The single decision. Owner condition first, failure message second."""

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
        return _estate_route(county or "the", has_living_identity)

    if owner_class == "TRUST" or owner_condition == "TRUST_HELD":
        return _trust_route(contact_resolution_state, has_living_identity)

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


NEXT_SOURCE_COLUMNS = [
    "candidate_id", "county", "apn", "owner_name", "owner_class", "owner_condition",
    "qualification_state", "primary_recipe", "score", "global_rank",
    "contact_resolution_state", "has_living_identity", "phone_present",
    "operator_queue", "contact_status",
    "next_source", "why", "required_input", "cost_usd", "manual_or_automatic",
    "expected_information", "blocking_reason",
]

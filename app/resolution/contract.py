"""The one canonical resolution record.

`app.contact` answers "did we find a phone?". This module defines what the
founder actually asked for: a record that can carry a property, why it is an
opportunity, who owns it, whether that owner is living, who the legitimate
living party is, how each contact point was obtained, and what an operator may
safely do next -- with every fact traceable to a source.

Four rules are enforced here rather than left to convention:

1. NO NAKED FACTS. A phone, an email, or an identity assertion that carries no
   Evidence cannot be constructed. `ContactPoint` and `Party` both refuse an
   empty evidence list.
2. ROLE IS NEVER COLLAPSED. A trustee is not an owner; a registered agent is
   not a principal; an entity switchboard is not a person's mobile. The role
   vocabulary is closed and the phone role must agree with the party role.
3. OPPORTUNITY AND CONTACTABILITY ARE SEPARATE DIMENSIONS. The record carries
   the opportunity score untouched and computes contact resolution beside it.
   Nothing in this module can change a score.
4. PHONE PRESENT != CALLABLE. `callability_status` is NOT_AUTHORIZED and there
   is no code path in this package that sets it to anything else.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

RESOLUTION_CONTRACT_VERSION = "resolution_record.v2"

# --------------------------------------------------------------------------
# Closed vocabularies
# --------------------------------------------------------------------------

# How a living party relates to the parcel. "OWNER" is deliberately absent as a
# catch-all: a party is a RECORDED_OWNER (named on the roll) or something more
# specific. Collapsing these is the defect this lane exists to fix.
RELATIONSHIP_ROLES: Tuple[str, ...] = (
    "RECORDED_OWNER",
    "COOWNER",
    "LIFE_TENANT",
    "HEIR",
    "PERSONAL_REPRESENTATIVE",
    "EXECUTOR",
    "ADMINISTRATOR",
    "TRUSTEE",
    "ENTITY_OFFICER",
    "ENTITY_MANAGER",
    "ENTITY_PRINCIPAL",
    "ENTITY_MAIN_CONTACT",
    "REGISTERED_AGENT",
    "DECEDENT",
    "OTHER_VERIFIED_RELATION",
)

# ---------------------------------------------------------------------------
# FAMILY is not AUTHORITY (T30)
# ---------------------------------------------------------------------------
# A role answers "what is this person to the parcel". It does not answer "how
# do we know", and those are different questions with different consequences.
# An obituary naming a daughter is FAMILY evidence; it does not make her the
# personal representative. A tax roll printing "SMITH JOHN W EXECUTOR" states a
# role, but a tax roll is not the court -- it is repeating what someone told the
# assessor. Only the estate file adjudicates.
#
# Before this class existed the graph could only say EXECUTOR, so a roll-stated
# executor and a court-appointed one were the same fact. They are not, and the
# operator who calls one of them needs to know which he has.
RELATIONSHIP_CLASSES: Tuple[str, ...] = (
    # Named on an official property instrument or roll as holding an interest.
    "PROPERTY_INTEREST",
    # Kinship or household. Evidence of a relationship between PEOPLE. Carries
    # no authority over the parcel and never will on its own.
    "FAMILY",
    # An official record STATES the role, but the record is not the adjudicating
    # one. A tax roll's "EXECUTOR" line lives here, honestly, forever -- until an
    # estate file corroborates it and it is re-asserted as LEGAL_AUTHORITY.
    "ROLE_STATED_ON_RECORD",
    # The adjudicating record itself names this person as holding legal power
    # over the estate or the parcel. This is the only class that may be shown to
    # an operator as authority to transact.
    "LEGAL_AUTHORITY",
    # The subject of the estate. Never contactable, never an authority.
    "DECEDENT_SUBJECT",
)

# Which source families are ALLOWED to establish LEGAL_AUTHORITY. This is the
# structural guard: not a naming convention, a refusal. A parcel roll cannot
# appoint an executor no matter what its owner-name column says, and the
# voter file cannot adjudicate an heir.
LEGAL_AUTHORITY_SOURCE_FAMILIES = frozenset({
    "ESTATE_IMPORT",
    "PROBATE_INDEX",
    "OPERATOR_MANUAL_PUBLIC_RECORD",
})

# The roles that assert legal power. Each one needs an adjudicating source.
AUTHORITY_ROLES = frozenset({
    "PERSONAL_REPRESENTATIVE",
    "EXECUTOR",
    "ADMINISTRATOR",
    "HEIR",
})

# The class a role defaults to when nothing better is asserted. A role that can
# be adjudicated defaults to ROLE_STATED_ON_RECORD, never to LEGAL_AUTHORITY:
# authority is earned from the evidence, not granted by the name of the role.
DEFAULT_CLASS_BY_ROLE: Dict[str, str] = {
    "RECORDED_OWNER": "PROPERTY_INTEREST",
    "COOWNER": "PROPERTY_INTEREST",
    "LIFE_TENANT": "PROPERTY_INTEREST",
    "TRUSTEE": "PROPERTY_INTEREST",
    "HEIR": "ROLE_STATED_ON_RECORD",
    "PERSONAL_REPRESENTATIVE": "ROLE_STATED_ON_RECORD",
    "EXECUTOR": "ROLE_STATED_ON_RECORD",
    "ADMINISTRATOR": "ROLE_STATED_ON_RECORD",
    "ENTITY_OFFICER": "ROLE_STATED_ON_RECORD",
    "ENTITY_MANAGER": "ROLE_STATED_ON_RECORD",
    "ENTITY_PRINCIPAL": "ROLE_STATED_ON_RECORD",
    "ENTITY_MAIN_CONTACT": "ROLE_STATED_ON_RECORD",
    "REGISTERED_AGENT": "ROLE_STATED_ON_RECORD",
    "DECEDENT": "DECEDENT_SUBJECT",
    "OTHER_VERIFIED_RELATION": "FAMILY",
}

# How a FAMILY relationship is specified when the evidence names the kinship.
# Separate from the role vocabulary on purpose: "daughter" is not a thing you
# are to a parcel, it is a thing you are to a person.
FAMILY_RELATIONS: Tuple[str, ...] = (
    "SPOUSE",
    "CHILD",
    "PARENT",
    "SIBLING",
    "GRANDCHILD",
    "IN_LAW",
    "HOUSEHOLD_CO_RESIDENT",
    "SURNAME_CORROBORATED_RELATIVE",
    "NAMED_SURVIVOR",
    "UNSPECIFIED_RELATIVE",
)


# A decedent is never a contactable party. A registered agent is a service
# address, not the seller. Both are modelled so the graph stays honest, and
# both are excluded from the operator's "who do I talk to" set.
NON_CONTACTABLE_ROLES = frozenset({"DECEDENT"})
NOT_THE_SELLER_ROLES = frozenset({"REGISTERED_AGENT"})

PARTY_KINDS: Tuple[str, ...] = ("PERSON", "ORG")

OWNER_CONDITIONS: Tuple[str, ...] = (
    "LIVING",
    "DECEASED",
    "ESTATE_OPEN",
    "HEIRS_UNRESOLVED",
    "ENTITY_ACTIVE",
    "ENTITY_STATUS_UNKNOWN",
    "TRUST_HELD",
    "GOVERNMENT",
    "UNKNOWN",
)

CONTACT_POINT_KINDS: Tuple[str, ...] = (
    "PHONE",
    "EMAIL",
    "MAILING_ADDRESS",
    "BUSINESS_ADDRESS",
    "PUBLIC_ENTITY_CONTACT",
)

# Whose line is it. This is the field that stops an entity reception number
# being reported as the owner's personal phone.
CONTACT_ROLES: Tuple[str, ...] = (
    "OWNER_PERSONAL",
    "COOWNER_PERSONAL",
    "HEIR_PERSONAL",
    "TRUSTEE_PERSONAL",
    "REPRESENTATIVE_PERSONAL",
    "ENTITY_MAIN",
    "BUSINESS_OFFICE",
    "REGISTERED_AGENT_OFFICE",
    "OTHER",
)

PERSONAL_CONTACT_ROLES = frozenset({
    "OWNER_PERSONAL", "COOWNER_PERSONAL", "HEIR_PERSONAL",
    "TRUSTEE_PERSONAL", "REPRESENTATIVE_PERSONAL",
})

# Which relationship roles may legitimately carry which contact role. An
# ENTITY_OFFICER's switchboard line is BUSINESS_OFFICE; it only becomes
# ENTITY_MAIN when the filing itself is the contact. A TRUSTEE never yields
# OWNER_PERSONAL.
ROLE_CONTACT_MATRIX: Dict[str, frozenset] = {
    "RECORDED_OWNER": frozenset({"OWNER_PERSONAL", "OTHER"}),
    "COOWNER": frozenset({"COOWNER_PERSONAL", "OTHER"}),
    "LIFE_TENANT": frozenset({"OWNER_PERSONAL", "COOWNER_PERSONAL", "OTHER"}),
    "HEIR": frozenset({"HEIR_PERSONAL", "COOWNER_PERSONAL", "OTHER"}),
    "PERSONAL_REPRESENTATIVE": frozenset({"REPRESENTATIVE_PERSONAL", "OTHER"}),
    "EXECUTOR": frozenset({"REPRESENTATIVE_PERSONAL", "OTHER"}),
    "ADMINISTRATOR": frozenset({"REPRESENTATIVE_PERSONAL", "OTHER"}),
    "TRUSTEE": frozenset({"TRUSTEE_PERSONAL", "OTHER"}),
    "ENTITY_OFFICER": frozenset({"BUSINESS_OFFICE", "ENTITY_MAIN", "OTHER"}),
    "ENTITY_MANAGER": frozenset({"BUSINESS_OFFICE", "ENTITY_MAIN", "OTHER"}),
    "ENTITY_PRINCIPAL": frozenset({"BUSINESS_OFFICE", "ENTITY_MAIN", "OTHER"}),
    "ENTITY_MAIN_CONTACT": frozenset({"ENTITY_MAIN", "BUSINESS_OFFICE", "OTHER"}),
    "REGISTERED_AGENT": frozenset({"REGISTERED_AGENT_OFFICE", "OTHER"}),
    "DECEDENT": frozenset({"OTHER"}),
    "OTHER_VERIFIED_RELATION": frozenset({"OTHER"}),
}

CONFIDENCE_CATEGORIES: Tuple[str, ...] = ("HIGH", "MEDIUM", "REVIEW", "REJECTED")

CONTACT_POINT_STATUSES: Tuple[str, ...] = (
    "ACTIVE_EVIDENCE",     # source asserts it now
    "HELD_FOR_REVIEW",     # found, precision not certified
    "SUPERSEDED",          # a better-evidenced point replaced it
    "RETIRED_AT_SOURCE",   # the source withdrew it
)

CALLABILITY_NOT_AUTHORIZED = "NOT_AUTHORIZED"

# Source families. A family is the class of authority, not the file.
SOURCE_FAMILIES: Tuple[str, ...] = (
    "NCSBE_PUBLIC_VOTER_FILE",
    "COUNTY_TAX_ROLL",
    "ESTATE_IMPORT",
    "PROBATE_INDEX",
    "SOS_IMPORT",
    "IRS_EO_ICO_NCSBE",
    "IRS_FORM_990_PUBLIC",
    "PUBLIC_BUSINESS_WEB",
    "LOCAL_OWNED_IDENTITY",
    "NC_ONEMAP_PARCEL_FABRIC",
    "CENSUS_ZCTA_GAZETTEER",
    "OPERATOR_MANUAL_PUBLIC_RECORD",
    # The county/state parcel rolls T22 acquired. One family because the class
    # of authority is the same -- a government parcel roll -- while source_name
    # carries which one (WAKE_PARCELS, DURHAM_PARCELS, MECK_CAMA_OWNERSHIP,
    # NC_ONEMAP_PARCELS). Distinct from NC_ONEMAP_PARCEL_FABRIC, which is an
    # ARBITER used to adjudicate geography and may never supply a contact.
    "COUNTY_PARCEL_ROLL",
    # The IRS Exempt Organizations Business Master File. Not a 990 filing.
    "IRS_EXEMPT_ORG_BMF",
)

# Arbiters adjudicate a match; they never supply a contact. Keeping this
# explicit stops a parcel-fabric hit ever being counted as a phone source.
ARBITER_FAMILIES = frozenset({"NC_ONEMAP_PARCEL_FABRIC", "CENSUS_ZCTA_GAZETTEER"})


class ContractViolation(ValueError):
    """A fact was asserted that the contract does not permit."""


def _norm(value: Optional[str]) -> str:
    return " ".join(str(value or "").strip().split())


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Evidence:
    """One traceable observation. Every important fact points at one of these.

    `source_locator` is recorded only where the terms of the source permit
    republishing a locator; where they do not it stays empty and
    `source_record_id` plus `source_hash` carry the traceability.
    """

    source_family: str
    source_name: str
    field: str
    raw_value: str
    normalized_value: str
    match_method: str
    confidence: str
    observed_at: str
    retrieved_at: str
    source_record_id: str = ""
    source_locator: str = ""
    source_hash: str = ""
    freshness_days: Optional[int] = None
    notes: str = ""

    def __post_init__(self) -> None:
        if self.source_family not in SOURCE_FAMILIES:
            raise ContractViolation(f"unknown source_family {self.source_family!r}")
        if self.confidence not in CONFIDENCE_CATEGORIES:
            raise ContractViolation(f"unknown confidence {self.confidence!r}")
        if not _norm(self.field):
            raise ContractViolation("evidence must name the field it supports")
        if not _norm(self.match_method):
            raise ContractViolation("evidence must state how it was matched")

    @property
    def evidence_id(self) -> str:
        """Content address. Identical observations collapse to one id, which is
        what makes the whole projection idempotent."""
        payload = "|".join([
            self.source_family, self.source_name, self.source_record_id,
            self.field, _norm(self.normalized_value).upper(),
            self.match_method, self.source_hash, self.observed_at,
        ])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict:
        out = asdict(self)
        out["evidence_id"] = self.evidence_id
        return out


# --------------------------------------------------------------------------
# Contact points
# --------------------------------------------------------------------------

@dataclass
class ContactPoint:
    kind: str
    value: str
    role: str
    confidence: str
    observed_at: str
    evidence: List[Evidence] = field(default_factory=list)
    status: str = "ACTIVE_EVIDENCE"
    confidence_score: Optional[int] = None
    callability_status: str = CALLABILITY_NOT_AUTHORIZED

    def __post_init__(self) -> None:
        if self.kind not in CONTACT_POINT_KINDS:
            raise ContractViolation(f"unknown contact point kind {self.kind!r}")
        if self.role not in CONTACT_ROLES:
            raise ContractViolation(f"unknown contact role {self.role!r}")
        if self.confidence not in CONFIDENCE_CATEGORIES:
            raise ContractViolation(f"unknown confidence {self.confidence!r}")
        if self.status not in CONTACT_POINT_STATUSES:
            raise ContractViolation(f"unknown status {self.status!r}")
        if not _norm(self.value):
            raise ContractViolation("a contact point must carry a value")
        if not self.evidence:
            raise ContractViolation(
                f"no naked contact points: {self.kind} {self.value!r} has no evidence"
            )
        if self.callability_status != CALLABILITY_NOT_AUTHORIZED:
            raise ContractViolation(
                "callability is not authorized in this lane; phone present != callable"
            )
        for ev in self.evidence:
            if ev.source_family in ARBITER_FAMILIES:
                raise ContractViolation(
                    f"{ev.source_family} is an arbiter and may not source a contact point"
                )

    @property
    def contact_point_id(self) -> str:
        payload = "|".join([self.kind, _norm(self.value).upper(), self.role])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict:
        return {
            "contact_point_id": self.contact_point_id,
            "kind": self.kind,
            "value": self.value,
            "role": self.role,
            "confidence": self.confidence,
            "confidence_score": self.confidence_score,
            "observed_at": self.observed_at,
            "status": self.status,
            "callability_status": self.callability_status,
            "evidence": [e.to_dict() for e in self.evidence],
        }


# --------------------------------------------------------------------------
# Parties and relationships
# --------------------------------------------------------------------------

@dataclass
class Party:
    """A living person or an organization. Never a decedent's contact record."""

    kind: str
    canonical_name: str
    evidence: List[Evidence] = field(default_factory=list)
    contact_points: List[ContactPoint] = field(default_factory=list)
    is_living: Optional[bool] = None
    entity_flags: Dict[str, str] = field(default_factory=dict)
    external_ids: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in PARTY_KINDS:
            raise ContractViolation(f"unknown party kind {self.kind!r}")
        if not _norm(self.canonical_name):
            raise ContractViolation("a party must have a canonical name")
        if not self.evidence:
            raise ContractViolation(
                f"no naked identity assertions: party {self.canonical_name!r} has no evidence"
            )

    @property
    def party_key(self) -> str:
        payload = "|".join([self.kind, _norm(self.canonical_name).upper()])
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict:
        return {
            "party_key": self.party_key,
            "kind": self.kind,
            "canonical_name": self.canonical_name,
            "is_living": self.is_living,
            "entity_flags": dict(self.entity_flags),
            "external_ids": dict(self.external_ids),
            "evidence": [e.to_dict() for e in self.evidence],
            "contact_points": [c.to_dict() for c in self.contact_points],
        }


@dataclass
class Relationship:
    """How one party relates to one candidate parcel, and how we know."""

    party: Party
    role: str
    evidence: List[Evidence] = field(default_factory=list)
    confidence: str = "HIGH"
    notes: str = ""
    #: Empty means "take the honest default for this role". It is never inferred
    #: upward: a caller must ASK for LEGAL_AUTHORITY, and asking is refused
    #: unless an adjudicating source is among the evidence.
    relationship_class: str = ""
    #: Only meaningful when relationship_class == FAMILY. What the evidence
    #: actually said the kinship was -- never a guess from a shared surname
    #: alone (that has its own value, SURNAME_CORROBORATED_RELATIVE).
    family_relation: str = ""

    def __post_init__(self) -> None:
        if self.role not in RELATIONSHIP_ROLES:
            raise ContractViolation(f"unknown relationship role {self.role!r}")
        if self.confidence not in CONFIDENCE_CATEGORIES:
            raise ContractViolation(f"unknown confidence {self.confidence!r}")
        if not self.evidence:
            raise ContractViolation(
                f"relationship {self.role} for {self.party.canonical_name!r} has no evidence"
            )
        if not self.relationship_class:
            object.__setattr__(self, "relationship_class",
                               DEFAULT_CLASS_BY_ROLE.get(self.role, "ROLE_STATED_ON_RECORD"))
        if self.relationship_class not in RELATIONSHIP_CLASSES:
            raise ContractViolation(
                f"unknown relationship class {self.relationship_class!r}")
        # THE GUARD. A role that asserts legal power must be able to name the
        # record that conferred it. Without this the class is decoration: any
        # caller could label a tax-roll line LEGAL_AUTHORITY and the graph would
        # agree. Refusing here means the only way to get authority into the
        # graph is to have read the adjudicating record.
        if self.relationship_class == "LEGAL_AUTHORITY":
            families = {e.source_family for e in self.evidence}
            if not (families & LEGAL_AUTHORITY_SOURCE_FAMILIES):
                raise ContractViolation(
                    f"{self.role} for {self.party.canonical_name!r} claims LEGAL_AUTHORITY "
                    f"but its evidence is {sorted(families)}; only "
                    f"{sorted(LEGAL_AUTHORITY_SOURCE_FAMILIES)} adjudicate authority"
                )
        if self.family_relation and self.family_relation not in FAMILY_RELATIONS:
            raise ContractViolation(f"unknown family relation {self.family_relation!r}")
        if self.family_relation and self.relationship_class != "FAMILY":
            raise ContractViolation(
                f"family_relation {self.family_relation!r} is only meaningful on a "
                f"FAMILY relationship, not {self.relationship_class}")
        allowed = ROLE_CONTACT_MATRIX[self.role]
        for cp in self.party.contact_points:
            if cp.role not in allowed:
                raise ContractViolation(
                    f"contact role {cp.role} is not permitted for relationship {self.role}; "
                    f"allowed: {sorted(allowed)}"
                )

    @property
    def is_contactable_party(self) -> bool:
        return self.role not in NON_CONTACTABLE_ROLES

    @property
    def is_seller_side(self) -> bool:
        """A registered agent or a formation service is not the seller."""
        return self.is_contactable_party and self.role not in NOT_THE_SELLER_ROLES

    @property
    def is_legal_authority(self) -> bool:
        """May this person be shown as holding power over the estate/parcel?

        The question the operator is really asking before he picks up the phone.
        A daughter named in an obituary answers NO here and that is not a defect
        -- it is the truth, and it is what stops the call being made on a
        premise the record does not support.
        """
        return self.relationship_class == "LEGAL_AUTHORITY"

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "relationship_class": self.relationship_class,
            "family_relation": self.family_relation,
            "confidence": self.confidence,
            "notes": self.notes,
            "is_contactable_party": self.is_contactable_party,
            "is_seller_side": self.is_seller_side,
            "is_legal_authority": self.is_legal_authority,
            "party": self.party.to_dict(),
            "evidence": [e.to_dict() for e in self.evidence],
        }


# --------------------------------------------------------------------------
# The resolution record
# --------------------------------------------------------------------------

@dataclass
class ResolutionRecord:
    """One CURRENT candidate, end to end.

    The opportunity fields (`score`, `global_rank`, `qualification_state`,
    `primary_recipe`) are carried verbatim. Nothing in this class writes them.
    """

    candidate_id: str
    estate_revision: str
    apn: str = ""
    county: str = ""
    fips: str = ""

    property_address_raw: str = ""
    property_address_normalized: str = ""
    mailing_address_raw: str = ""
    mailing_address_normalized: str = ""

    owner_name_raw: str = ""
    owner_name_normalized: str = ""
    owner_class: str = "UNKNOWN"
    owner_condition: str = "UNKNOWN"

    # opportunity dimension -- read-only here
    score: Optional[float] = None
    global_rank: Optional[int] = None
    qualification_state: str = ""
    primary_recipe: str = ""
    matched_recipes: List[str] = field(default_factory=list)
    recipe_version: str = ""

    relationships: List[Relationship] = field(default_factory=list)
    contact_resolution_state: str = "UNRESOLVED"
    next_source: Optional[dict] = None
    operator_queue: str = ""
    blockers: List[str] = field(default_factory=list)

    contract_version: str = RESOLUTION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.owner_condition not in OWNER_CONDITIONS:
            raise ContractViolation(f"unknown owner_condition {self.owner_condition!r}")

    # -- derived, all read-only ------------------------------------------
    @property
    def living_parties(self) -> List[Relationship]:
        return [r for r in self.relationships if r.is_contactable_party]

    @property
    def seller_side_parties(self) -> List[Relationship]:
        return [r for r in self.relationships if r.is_seller_side]

    @property
    def has_living_identity(self) -> bool:
        return bool(self.living_parties)

    @property
    def contact_points(self) -> List[Tuple[Relationship, ContactPoint]]:
        return [(r, cp) for r in self.relationships for cp in r.party.contact_points]

    @property
    def phones(self) -> List[Tuple[Relationship, ContactPoint]]:
        return [(r, cp) for r, cp in self.contact_points if cp.kind == "PHONE"]

    @property
    def high_phones(self) -> List[Tuple[Relationship, ContactPoint]]:
        return [(r, cp) for r, cp in self.phones
                if cp.confidence == "HIGH" and cp.status == "ACTIVE_EVIDENCE"]

    @property
    def callability_status(self) -> str:
        return CALLABILITY_NOT_AUTHORIZED

    @property
    def contact_resolution_score(self) -> int:
        """A 0-100 measure of how far this candidate is from a safe contact.

        Deliberately separate from the opportunity score, and never fed back
        into it. A parcel does not become a better deal because we found a
        phone number.
        """
        if self.high_phones:
            return 100
        if any(cp.kind == "PHONE" for _, cp in self.contact_points):
            return 70
        if self.has_living_identity and self.contact_points:
            return 55
        if self.has_living_identity:
            return 40
        if self.owner_condition in {"DECEASED", "ESTATE_OPEN", "HEIRS_UNRESOLVED"}:
            return 10
        if self.owner_condition == "GOVERNMENT":
            return 0
        return 20

    def to_dict(self) -> dict:
        return {
            "contract_version": self.contract_version,
            "candidate_id": self.candidate_id,
            "estate_revision": self.estate_revision,
            "apn": self.apn,
            "county": self.county,
            "fips": self.fips,
            "property_address_raw": self.property_address_raw,
            "property_address_normalized": self.property_address_normalized,
            "mailing_address_raw": self.mailing_address_raw,
            "mailing_address_normalized": self.mailing_address_normalized,
            "owner_name_raw": self.owner_name_raw,
            "owner_name_normalized": self.owner_name_normalized,
            "owner_class": self.owner_class,
            "owner_condition": self.owner_condition,
            "opportunity": {
                "score": self.score,
                "global_rank": self.global_rank,
                "qualification_state": self.qualification_state,
                "primary_recipe": self.primary_recipe,
                "matched_recipes": list(self.matched_recipes),
                "recipe_version": self.recipe_version,
            },
            "contact": {
                "contact_resolution_state": self.contact_resolution_state,
                "contact_resolution_score": self.contact_resolution_score,
                "has_living_identity": self.has_living_identity,
                "callability_status": self.callability_status,
                "operator_queue": self.operator_queue,
                "blockers": list(self.blockers),
                "next_source": self.next_source,
            },
            "relationships": [r.to_dict() for r in self.relationships],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

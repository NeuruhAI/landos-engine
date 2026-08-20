"""Who is still living in the dead owner's house.

THE GAP THIS CLOSES. `ncsbe_match` asks "is the OWNER in the voter file", and
for this cohort the answer is permanently no: the owner is dead, and the voter
file is a register of the living. 236 of the 306 Qualified opportunities are
deceased-owner records and every one of them has zero phones. Running the
matcher again cannot change that, because it is not failing -- it is answering
correctly. The question it was never asked is the one an investigator asks
first: WHO ELSE LIVES THERE.

THE MECHANISM. A parcel has a situs address and a mailing address. Both are
already held. The NCSBE public file is address-keyed. So for a decedent's
address, the file names the living people registered at it -- and when one of
them carries the decedent's surname, that is corroborated evidence of a family
relationship to the person whose name is on the deed.

WHY THIS IS THE OBITUARY, LAWFULLY. An obituary's value is its survivor list.
Legacy.com's robots.txt disallows the obituary paths to every non-partner agent
(re-probed 2026-08-20), so that list cannot be taken from there. A household
roster taken from the state's own public register is the same class of fact --
"these people were close enough to the decedent to share an address" -- from a
source that publishes it deliberately.

WHAT THIS IS NOT, AND THE MODULE ENFORCES IT:
  * Not heirship. Living at a decedent's address does not make anyone an heir.
    Every relationship this module emits is FAMILY class with role
    OTHER_VERIFIED_RELATION. It CANNOT emit HEIR, EXECUTOR, ADMINISTRATOR or
    PERSONAL_REPRESENTATIVE -- there is no code path and no parameter for it.
  * Not an identity merge. Two registrations are two people unless they share
    an NCID, which is the state's own unique key. A near-name (MILLET /
    MILLETT) is NOT collapsed: the file says they are two registrations and
    inventing a merge to tidy the display would be inventing a person.
  * Not a phone source of its own. A phone found here is an NCSBE phone, with
    NCSBE evidence, subject to the same confidentiality and status rules the
    existing matcher applies.

AMBIGUITY IS AN OUTPUT, NOT A PROBLEM. A house with three surname-matched
adults is three relatives, and all three are returned. What is refused is the
opposite error -- picking one and calling him "the" heir.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .address_normalize import ParsedAddress
from .owner_identity import PersonName

HOUSEHOLD_VERSION = "household_family.v1"

#: The registration statuses that describe a person the state still considers
#: registered. Same set the accepted matcher uses -- one definition of "living
#: and on the roll", not two that can drift apart.
ACTIVE_STATUSES = frozenset({"ACTIVE", "INACTIVE", "TEMPORARY"})
CONFIDENTIAL_YES = frozenset({"Y", "YES", "1", "TRUE"})

#: Which address the co-residency was observed at. The situs is the stronger
#: fact -- the person is living IN the subject property -- and the operator is
#: told which one he has rather than the two being averaged into "an address".
OBSERVED_AT_SITUS = "SITUS"
OBSERVED_AT_MAILING = "MAILING"

#: The match method that goes on the evidence row. Named for what was actually
#: compared, so that a later reader can tell it from an adjudicated heirship
#: without reading this docstring.
METHOD_SURNAME = "HOUSEHOLD_CO_RESIDENT_SURNAME_CORROBORATED"
METHOD_CO_RESIDENT = "HOUSEHOLD_CO_RESIDENT"
#: The owner line did not say which token was the surname, and the voter matches
#: only the reading the parser ranked second. A real observation, a weak claim.
METHOD_SURNAME_AMBIGUOUS = "HOUSEHOLD_CO_RESIDENT_SURNAME_ORDER_AMBIGUOUS"

#: A living co-resident who does NOT share the surname is real and is recorded,
#: but it is a weaker fact -- a tenant, a carer, a partner under another name.
#: It is never promoted to the primary party.
CONF_SURNAME = "HIGH"
CONF_CO_RESIDENT = "REVIEW"


@dataclass(frozen=True, slots=True)
class HouseholdVoter:
    """One registration in the public file, reduced to what this module reads."""
    ncid: str
    last: str
    first: str
    middle: str = ""
    suffix: str = ""
    status: str = ""
    reason: str = ""
    confidential: str = ""
    phone_raw: str = ""
    birth_year: str = ""
    county: str = ""
    res: Optional[ParsedAddress] = None
    source_file: str = ""
    source_hash: str = ""
    observed_at: str = ""

    @property
    def display_name(self) -> str:
        parts = [self.first, self.middle, self.last, self.suffix]
        return " ".join(p for p in (x.strip() for x in parts) if p)

    @property
    def is_live_registration(self) -> bool:
        return _u(self.status) in ACTIVE_STATUSES

    @property
    def is_confidential(self) -> bool:
        return _u(self.confidential) in CONFIDENTIAL_YES

    @property
    def is_deceased_flagged(self) -> bool:
        return "DECEASED" in _u(self.reason)


@dataclass(frozen=True, slots=True)
class HouseholdMember:
    """A living person found at a decedent's address, and why we believe it."""
    voter: HouseholdVoter
    observed_at_address: str      # OBSERVED_AT_SITUS | OBSERVED_AT_MAILING
    surname_corroborated: bool
    match_method: str
    confidence: str
    #: Always FAMILY. Present as a field so the value travels with the fact
    #: rather than being re-derived by every consumer.
    relationship_class: str = "FAMILY"
    #: HOUSEHOLD_CO_RESIDENT when only the address agrees;
    #: SURNAME_CORROBORATED_RELATIVE when the family name agrees too.
    family_relation: str = "HOUSEHOLD_CO_RESIDENT"
    #: The role on the parcel. Fixed. See the module docstring.
    relationship_role: str = "OTHER_VERIFIED_RELATION"

    @property
    def has_phone(self) -> bool:
        return bool(self.voter.phone_raw.strip())


@dataclass
class HouseholdResult:
    """What one candidate's addresses yielded, including the refusals."""
    members: List[HouseholdMember] = field(default_factory=list)
    decedent_registrations: List[HouseholdVoter] = field(default_factory=list)
    skipped_confidential: int = 0
    skipped_not_live: int = 0
    addresses_probed: List[str] = field(default_factory=list)
    #: True when more than one DISTINCT surname-corroborated person was found.
    #: Not an error -- a household -- but the caller must not pick one and call
    #: it "the" relative.
    multiple_relatives: bool = False

    @property
    def surname_members(self) -> List[HouseholdMember]:
        return [m for m in self.members if m.surname_corroborated]

    @property
    def distinct_person_count(self) -> int:
        return len({m.voter.ncid or m.voter.display_name for m in self.surname_members})


def _u(value: Optional[str]) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _by_segment(people: Sequence[PersonName]) -> Dict[str, List[PersonName]]:
    """Group readings back onto the raw text they were read from."""
    out: Dict[str, List[PersonName]] = {}
    for i, person in enumerate(people):
        out.setdefault(person.raw_segment or f"#{i}", []).append(person)
    return out


def decedent_surnames(people: Sequence[PersonName]) -> Set[str]:
    """The surnames the roll most likely attributes to the parcel's dead owners.

    A county that writes `SURNAME, FIRST MIDDLE HEIRS` has told us the surname.
    A county that writes `SURNAME FIRST MIDDLE HEIRS` has not, and the owner
    parser is honest about it: it returns BOTH readings, `last_first` (SURNAME)
    and `western` (MIDDLE). Treating that ambiguity as two equally good surnames
    would let a neighbour who happens to carry the middle name as a SURNAME
    corroborate as family, which is precisely the false link this module exists
    to avoid. Real owner strings are not used as examples here: the parcel roll
    names real people, and a docstring is not the place for them.

    So the PRIMARY set is one surname per raw segment -- the parser's own first
    reading, which is `last_first`, the order the NC rolls overwhelmingly use
    for a `... HEIRS` line. The alternate reading is not discarded; it is
    returned by `alternate_surnames` and it only ever earns REVIEW.
    """
    return {_u(group[0].last) for group in _by_segment(people).values()
            if group and _u(group[0].last)}


def alternate_surnames(people: Sequence[PersonName]) -> Set[str]:
    """Surnames from the readings the parser did NOT put first.

    Kept separate so a match against one is visibly weaker, never silently
    equal to a match against the primary reading.
    """
    primary = decedent_surnames(people)
    alt: Set[str] = set()
    for group in _by_segment(people).values():
        for person in group[1:]:
            if _u(person.last):
                alt.add(_u(person.last))
    return alt - primary


def _dedupe_voters(voters: Iterable[HouseholdVoter]) -> List[HouseholdVoter]:
    """One row per registration. NCID is the state's key; absent it, fall back
    to the full name plus birth year, which is the weakest key that still
    refuses to merge two different people who share a first and last name."""
    seen: Set[Tuple[str, ...]] = set()
    out: List[HouseholdVoter] = []
    for v in voters:
        key = (v.ncid,) if v.ncid else ("", _u(v.last), _u(v.first), _u(v.middle),
                                        _u(v.suffix), v.birth_year)
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


def household_for_candidate(
    *,
    owner_people: Sequence[PersonName],
    situs_voters: Sequence[HouseholdVoter] = (),
    mailing_voters: Sequence[HouseholdVoter] = (),
    situs_label: str = "",
    mailing_label: str = "",
) -> HouseholdResult:
    """Read a decedent's two addresses into a family roster.

    Inputs are already-selected voter rows -- the caller does the address
    lookup, this module does the judgement. Pure and deterministic: the same
    rows in the same order give byte-identical output, so the result can be
    replayed and diffed.
    """
    result = HouseholdResult()
    surnames = decedent_surnames(owner_people)
    alternates = alternate_surnames(owner_people)
    if situs_label:
        result.addresses_probed.append(f"{OBSERVED_AT_SITUS}:{situs_label}")
    if mailing_label:
        result.addresses_probed.append(f"{OBSERVED_AT_MAILING}:{mailing_label}")

    # The situs is examined first so that when the same person is registered at
    # both, the stronger observation is the one that survives the dedupe.
    for where, voters in ((OBSERVED_AT_SITUS, situs_voters),
                          (OBSERVED_AT_MAILING, mailing_voters)):
        for voter in _dedupe_voters(voters):
            if voter.is_deceased_flagged:
                # The file itself says this registration is the decedent's, or
                # another dead resident's. Kept as corroboration of the death,
                # never offered as a living party.
                result.decedent_registrations.append(voter)
                continue
            if voter.is_confidential:
                # G.S. 132-1.4 / protected registrants. Not read, not counted,
                # not "found but withheld" -- the row does not leave here.
                result.skipped_confidential += 1
                continue
            if not voter.is_live_registration:
                result.skipped_not_live += 1
                continue
            vlast = _u(voter.last)
            corroborated = vlast in surnames
            # A match only against the parser's second reading of an ambiguous
            # owner line is recorded, and it is recorded as the weaker thing it
            # is: the surname itself is not established.
            on_alternate = (not corroborated) and vlast in alternates
            if corroborated:
                method, conf, relation = METHOD_SURNAME, CONF_SURNAME, "SURNAME_CORROBORATED_RELATIVE"
            elif on_alternate:
                method, conf, relation = METHOD_SURNAME_AMBIGUOUS, CONF_CO_RESIDENT, "HOUSEHOLD_CO_RESIDENT"
            else:
                method, conf, relation = METHOD_CO_RESIDENT, CONF_CO_RESIDENT, "HOUSEHOLD_CO_RESIDENT"
            result.members.append(HouseholdMember(
                voter=voter,
                observed_at_address=where,
                surname_corroborated=corroborated,
                match_method=method,
                confidence=conf,
                family_relation=relation,
            ))

    # A person registered at both addresses is one person. Keep the situs
    # observation (listed first) and drop the weaker duplicate.
    seen: Set[str] = set()
    kept: List[HouseholdMember] = []
    for m in result.members:
        key = m.voter.ncid or f"{_u(m.voter.last)}|{_u(m.voter.first)}|{m.voter.birth_year}"
        if key in seen:
            continue
        seen.add(key)
        kept.append(m)
    result.members = kept
    result.multiple_relatives = result.distinct_person_count > 1
    return result


def relationship_note(member: HouseholdMember, decedent_display: str) -> str:
    """The sentence the operator reads. It states the limit in the same breath
    as the finding, because the finding is only safe if the limit travels."""
    if member.surname_corroborated:
        return (
            f"living co-resident at the {member.observed_at_address.lower()} address "
            f"sharing the surname of decedent {decedent_display}. Household "
            f"co-residency plus a shared family name is FAMILY evidence; it is not "
            f"an adjudicated heirship and confers no authority over the estate. "
            f"The estate file remains the only record that can name a personal "
            f"representative."
        )
    return (
        f"living co-resident at the {member.observed_at_address.lower()} address of "
        f"decedent {decedent_display}, under a different surname. Co-residency alone "
        f"does not evidence a family relationship and confers no authority; held for "
        f"review."
    )

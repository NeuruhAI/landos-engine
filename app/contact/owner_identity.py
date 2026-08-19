"""Parse assessor owner strings into people vs entities vs decedents.

Never treat an entity as a voter-person. Never treat a decedent as a living
voter. Living co-owners / named heirs on the roll are first-class people with
relationship provenance.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[.]")

ENTITY_SUFFIX_RE = re.compile(
    r"\b(LLC|L\.?L\.?C\.?|INC|INCORPORATED|CORP|CORPORATION|LTD|LIMITED|"
    r"LLP|PLLC|PLLP|LP|PC|P\.C\.|CO|COMPANY|HOLDINGS|PROPERTIES|PROPERTY|"
    r"ENTERPRISES?|INVESTMENTS?|INVESTMENT GROUP|DEVELOPMENT|DEVELOPERS?|"
    r"REALTY|PARTNERS(?:HIP)?|PARTNER|ASSOC(?:IATION|IATES)?|ASSN|"
    r"HOMEOWNERS|HOA|CONDOMINIUM|AUTHORITY|COMMISSION|DISTRICT|"
    r"DEPARTMENT|DEPT|BANK|CREDIT UNION|UNIVERSITY|COLLEGE|HOSPITAL|"
    r"HOUSING|PROPCO|GROUP LLC)\b",
    re.I,
)
RELIGIOUS_RE = re.compile(
    r"\b(CHURCH|MINISTR(?:Y|IES)|BAPTIST|METHODIST|CATHOLIC|PRESBYTERIAN|"
    r"LUTHERAN|PENTECOSTAL|HOLINESS|GOSPEL|MOSQUE|SYNAGOGUE|TABERNACLE|"
    r"ASSEMBLY OF GOD|HOUSE OF PRAYER|TEMPLE OF|CHAPEL)\b",
    re.I,
)
GOV_RE = re.compile(
    r"\b(CITY OF|COUNTY OF|TOWN OF|STATE OF|UNITED STATES|USA|NC DOT|"
    r"DEPARTMENT OF|BOARD OF EDUCATION|SCHOOL DISTRICT)\b",
    re.I,
)
TRUST_RE = re.compile(r"\b(TRUST|TRUSTEE)\b", re.I)
REAL_ESTATE_RE = re.compile(r"\bREAL\s+ESTATE\b", re.I)
# Bare "LE" is forbidden — it is a substring of ALLEN/ALLEYNE.
LIFE_ESTATE_RE = re.compile(r"\(?\s*LIFE\s+ESTATE\s*\)?|\(L/?E\)|\bL/E\b", re.I)
# HEIR singular is NOT a decedent marker (SOLE HEIR is a living person).
DECEASED_RE = re.compile(
    r"\b(?:HEIRS(?:\s+OF)?|HRS|DECEASED|DEC'?D|SOLE\s+HEIRS)\b|\bESTATE(?:\s+OF)?\b",
    re.I,
)
SOLE_HEIR_RE = re.compile(r"\bSOLE\s+HEIR\b", re.I)
ESTATE_OF_RE = re.compile(r"^\s*ESTATE(?:\s+OF)?\s+", re.I)
ET_AL_RE = re.compile(r"\bET\s+(?:AL|ALS|UX|VIR|UXOR)\b\.?", re.I)
STATUS_STRIP_RE = re.compile(
    r"\b(?:HEIRS\s+OF|HEIRS|HRS|SOLE\s+HEIRS|SOLE\s+HEIR|DECEASED|DEC'?D|"
    r"ESTATE(?:\s+OF)?|LIFE\s+ESTATE)\b|\(L/?E\)|\bL/E\b",
    re.I,
)
CORPORATE_TOKEN_RE = re.compile(
    r"\b(LLC|INC|CORP|LTD|LLP|PLLC|LP|PC|COMPANY|HOLDINGS|PROPERTIES|"
    r"ENTERPRISES?|REALTY|PARTNERS|ASSOCIATION|HOA|BANK|CREDIT UNION)\b",
    re.I,
)
SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "VI", "2ND", "3RD", "4TH"}
JOINERS = {"AND", "WIFE", "HUSBAND", "SPOUSE", "ET", "UX", "ALS", "THE"}
JOINER_LASTS = JOINERS | {"AL"}
# Tokens that are not a human given name / surname in isolation.
NON_PERSON_TOKENS = {
    "LAND", "ROAD", "STREET", "AVENUE", "DRIVE", "LLC", "INC", "CORP",
    "TRUST", "BANK", "CHURCH", "CITY", "COUNTY", "TOWN", "STATE",
}

COMMON_LASTS = {
    "SMITH", "JOHNSON", "WILLIAMS", "BROWN", "JONES", "GARCIA", "MILLER",
    "DAVIS", "RODRIGUEZ", "MARTINEZ", "HERNANDEZ", "LOPEZ", "GONZALEZ",
    "WILSON", "ANDERSON", "THOMAS", "TAYLOR", "MOORE", "JACKSON", "MARTIN",
    "LEE", "PEREZ", "THOMPSON", "WHITE", "HARRIS", "SANCHEZ", "CLARK",
    "RAMIREZ", "LEWIS", "ROBINSON", "WALKER", "YOUNG", "ALLEN", "KING",
    "WRIGHT", "SCOTT", "TORRES", "NGUYEN", "HILL", "FLORES", "GREEN",
    "ADAMS", "NELSON", "BAKER", "HALL", "RIVERA", "CAMPBELL", "MITCHELL",
    "CARTER", "ROBERTS", "EDWARDS", "COLLINS", "TURNER", "PARKER", "EVANS",
    "COLLINS", "STEWART", "MORRIS", "MURPHY", "COOK", "ROGERS", "MORGAN",
    "PETERSON", "COOPER", "REED", "BAILEY", "BELL", "GOMEZ", "HOWARD",
    "WARD", "COX", "DIAZ", "RICHARDSON", "WOOD", "WATSON", "BROOKS",
    "BENNETT", "GRAY", "JAMES", "REYES", "CRUZ", "HUGHES", "PRICE",
    "MYERS", "LONG", "FOSTER", "SANDERS", "ROSS", "POWELL", "SULLIVAN",
    "RUSSELL", "ORTIZ", "JENKINS", "GUTIERREZ", "PERRY", "BUTLER",
    "BARNES", "FISHER", "HENDERSON", "COLEMAN", "SIMMONS", "PATTERSON",
    "JORDAN", "REYNOLDS", "HAMILTON", "GRAHAM", "KIM", "WALLACE", "WOODS",
    "COLE", "WEST", "JORDAN", "OWENS", "REYNOLDS", "HARRISON", "GIBSON",
}
COMMON_FIRSTS = {
    "JAMES", "JOHN", "ROBERT", "MICHAEL", "WILLIAM", "DAVID", "RICHARD",
    "JOSEPH", "THOMAS", "CHRISTOPHER", "CHARLES", "DANIEL", "MATTHEW",
    "ANTHONY", "MARK", "DONALD", "STEVEN", "PAUL", "ANDREW", "JOSHUA",
    "KENNETH", "KEVIN", "BRIAN", "GEORGE", "TIMOTHY", "RONALD", "EDWARD",
    "JASON", "JEFFREY", "RYAN", "JACOB", "GARY", "NICHOLAS", "ERIC",
    "JONATHAN", "STEPHEN", "LARRY", "JUSTIN", "SCOTT", "BRANDON",
    "BENJAMIN", "SAMUEL", "GREGORY", "ALEXANDER", "PATRICK", "FRANK",
    "MARY", "PATRICIA", "JENNIFER", "LINDA", "ELIZABETH", "BARBARA",
    "SUSAN", "JESSICA", "SARAH", "KAREN", "LISA", "NANCY", "BETTY",
    "MARGARET", "SANDRA", "ASHLEY", "KIMBERLY", "EMILY", "DONNA",
    "MICHELLE", "DOROTHY", "CAROL", "AMANDA", "MELISSA", "DEBORAH",
    "STEPHANIE", "REBECCA", "SHARON", "LAURA", "CYNTHIA", "KATHLEEN",
    "AMY", "ANGELA", "SHIRLEY", "ANNA", "BRENDA", "PAMELA", "EMMA",
    "NICOLE", "HELEN", "SAMANTHA", "KATHERINE", "CHRISTINE", "DEBRA",
    "RACHEL", "CAROLYN", "JANET", "CATHERINE", "MARIA", "HEATHER",
    "DIANE", "JULIE", "JOYCE", "VICTORIA", "KELLY", "CHRISTINA",
}


@dataclass(frozen=True)
class PersonName:
    first: str
    last: str
    middle: str = ""
    suffix: str = ""
    deceased: bool = False
    life_estate: bool = False
    role: str = "OWNER"  # OWNER, COOWNER, LIFE_TENANT, NAMED_HEIR_ON_ROLL, NAMED_TRUSTEE_ON_ROLL, DECEDENT
    parse_method: str = ""
    raw_segment: str = ""

    @property
    def full_normalized(self) -> str:
        parts = [self.first, self.middle, self.last, self.suffix]
        return " ".join(p for p in parts if p)

    @property
    def is_common_name(self) -> bool:
        return self.last in COMMON_LASTS and self.first in COMMON_FIRSTS

    @property
    def living_matchable(self) -> bool:
        return (not self.deceased) and self.role != "DECEDENT" and bool(self.first) and bool(self.last)


@dataclass
class OwnerParse:
    raw: str
    kind: str  # INDIVIDUAL, DECEASED, LIFE_ESTATE, TRUST, ENTITY, MIXED, EMPTY
    people: List[PersonName] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def living_people(self) -> List[PersonName]:
        return [p for p in self.people if p.living_matchable]

    @property
    def decedents(self) -> List[PersonName]:
        return [p for p in self.people if p.deceased or p.role == "DECEDENT"]


def norm_upper(text: str) -> str:
    text = _PUNCT.sub("", (text or "").upper())
    text = text.replace(",", " , ")
    text = text.replace("&", " & ")
    return _WS.sub(" ", text).strip()


def _norm(text: str) -> str:
    return norm_upper(text)


def is_entity_name(name: str) -> bool:
    n = name or ""
    if REAL_ESTATE_RE.search(n) and ENTITY_SUFFIX_RE.search(n):
        return True
    if GOV_RE.search(n) or RELIGIOUS_RE.search(n):
        return True
    if ENTITY_SUFFIX_RE.search(n):
        if DECEASED_RE.search(n) and not REAL_ESTATE_RE.search(n):
            # "SMITH JOHN ESTATE" is not an entity.
            if not re.search(r"\b(LLC|INC|CORP|LP|LLP|PLLC|CO|COMPANY)\b", n, re.I):
                return False
        return True
    return False


def is_trust_name(name: str) -> bool:
    n = name or ""
    if re.search(r"\b(TRUST|TRUSTEE)\b", n, re.I):
        if is_entity_name(n) and re.search(r"\b(LLC|INC|CORP)\b", n, re.I):
            return True
        return True
    toks = _norm(n).split()
    return bool(toks) and toks[-1] == "TR"


def _tokens(segment: str) -> List[str]:
    cleaned = ET_AL_RE.sub(" ", segment)
    cleaned = STATUS_STRIP_RE.sub(" ", cleaned)
    cleaned = LIFE_ESTATE_RE.sub(" ", cleaned)
    cleaned = _norm(cleaned).replace(",", " ")
    return [t for t in cleaned.split() if t and t not in {"OF", "THE", ",", "&"}]


def _split_suffix(tokens: List[str]) -> tuple[List[str], str]:
    suffix = ""
    out: List[str] = []
    for i, tok in enumerate(tokens):
        if tok in SUFFIXES and not suffix and i > 0:
            suffix = tok
        else:
            out.append(tok)
    if out and out[-1] in SUFFIXES and not suffix:
        suffix = out.pop()
    return out, suffix


def _person(first, last, middle="", suffix="", deceased=False, life_estate=False,
            role="OWNER", parse_method="", raw_segment="") -> Optional[PersonName]:
    first = (first or "").strip()
    last = (last or "").strip()
    if not first or not last:
        return None
    if first in JOINERS or last in JOINER_LASTS:
        return None
    if last in NON_PERSON_TOKENS:
        return None
    role_out = role
    if deceased and role == "OWNER":
        role_out = "DECEDENT"
    return PersonName(
        first=first,
        last=last,
        middle=(middle or "").strip(),
        suffix=suffix or "",
        deceased=deceased,
        life_estate=life_estate,
        role=role_out,
        parse_method=parse_method,
        raw_segment=raw_segment,
    )


def _segment_flags(segment: str) -> tuple[bool, bool, bool]:
    life = bool(LIFE_ESTATE_RE.search(segment))
    sole_heir = bool(SOLE_HEIR_RE.search(segment))
    deceased = bool(DECEASED_RE.search(segment)) and not REAL_ESTATE_RE.search(segment)
    if life or sole_heir:
        deceased = False
    return deceased, life, sole_heir


def _parse_comma_segment(segment: str) -> List[PersonName]:
    deceased, life, sole_heir = _segment_flags(segment)
    if "," not in segment:
        return []
    last_part, rest = segment.split(",", 1)
    last_toks, last_suffix_unused = _split_suffix(_tokens(last_part))
    rest_toks, suffix = _split_suffix(_tokens(rest))
    if not last_toks or not rest_toks:
        return []
    last = last_toks[0]
    first = rest_toks[0]
    middle = " ".join(rest_toks[1:])
    if life:
        role = "LIFE_TENANT"
    elif sole_heir:
        role = "NAMED_HEIR_ON_ROLL"
    elif deceased:
        role = "DECEDENT"
    else:
        role = "OWNER"
    p = _person(first, last, middle, suffix or last_suffix_unused, deceased, life, role, "comma", segment)
    return [p] if p else []


def _parse_estate_of(segment: str) -> List[PersonName]:
    if not ESTATE_OF_RE.search(segment):
        return []
    rest = ESTATE_OF_RE.sub("", segment)
    toks, suffix = _split_suffix(_tokens(rest))
    if len(toks) < 2:
        return []
    # "ESTATE OF REGINALD M SANDERS" / "ESTATE OF JOHN LEWIS SMALL" → western order.
    first, last = toks[0], toks[-1]
    middle = " ".join(toks[1:-1])
    p = _person(first, last, middle, suffix, True, False, "DECEDENT", "estate_of", segment)
    return [p] if p else []


def _parse_given_status_last(segment: str) -> List[PersonName]:
    """WILLIE C HEIRS GUY / KATIE W (LIFE ESTATE) CLAWSON / EVA SOLE HEIR CHAMBERS."""
    deceased, life, sole_heir = _segment_flags(segment)
    if not deceased and not life and not sole_heir:
        return []
    # Split around the status token, requiring a trailing surname.
    m = re.search(
        r"^(?P<given>.+?)\s+(?:\(\s*)?(?:HEIRS\s+OF|HEIRS|HRS|SOLE\s+HEIRS|SOLE\s+HEIR|"
        r"ESTATE(?:\s+OF)?|LIFE\s+ESTATE|L/E)(?:\s*\))?\s+(?P<last>[A-Z][A-Z'\-]{1,})$",
        _norm(segment).replace(" ,", ""),
        re.I,
    )
    if not m:
        return []
    given_toks, suffix = _split_suffix(_tokens(m.group("given")))
    last = m.group("last").upper()
    if last in SUFFIXES or last in JOINER_LASTS:
        return []
    if not given_toks:
        return []
    first = given_toks[0]
    middle = " ".join(given_toks[1:])
    if life:
        role = "LIFE_TENANT"
    elif sole_heir:
        role = "NAMED_HEIR_ON_ROLL"
    else:
        role = "DECEDENT"
    p = _person(first, last, middle, suffix, deceased and not life, life, role, "given_status_last", segment)
    return [p] if p else []


def _parse_meck_ampersand(segment: str) -> List[PersonName]:
    """JAMES G III & BOLTON / DAVID & STRAITE / MACK LEE & WIFE MORRISON."""
    if "&" not in segment or "," in segment:
        return []
    left, right = [p.strip() for p in segment.split("&", 1)]
    right_toks = _tokens(re.sub(r"\b(WIFE|HUSBAND|SPOUSE)\b", " ", right, flags=re.I))
    if len(right_toks) != 1:
        return []
    last = right_toks[0]
    deceased, life, sole_heir = _segment_flags(segment)
    left_toks, suffix = _split_suffix(_tokens(left))
    if not left_toks:
        return []
    first = left_toks[0]
    middle = " ".join(left_toks[1:])
    if life:
        role = "LIFE_TENANT"
    elif sole_heir:
        role = "NAMED_HEIR_ON_ROLL"
    elif deceased:
        role = "DECEDENT"
    else:
        role = "OWNER"
    p = _person(first, last, middle, suffix, deceased, life, role, "meck_ampersand", segment)
    return [p] if p else []


def _parse_last_first_and_western(segment: str) -> List[PersonName]:
    deceased, life, sole_heir = _segment_flags(segment)
    toks, suffix = _split_suffix(_tokens(segment))
    if len(toks) < 2:
        return []
    if life:
        role = "LIFE_TENANT"
    elif sole_heir:
        role = "NAMED_HEIR_ON_ROLL"
    elif deceased:
        role = "DECEDENT"
    else:
        role = "OWNER"
    out: List[PersonName] = []
    # Assessor last-first: CHAMBERS EDNA MAE / GREEN KIMBERLY / HARRIS GRADY C
    p1 = _person(toks[1], toks[0], " ".join(toks[2:]), suffix, deceased, life, role, "last_first", segment)
    if p1:
        out.append(p1)
    # Western first-last: RANDALL A WOLF / JOHNNIE LITTLE / PHILIP ALLEN
    p2 = _person(toks[0], toks[-1], " ".join(toks[1:-1]), suffix, deceased, life, role, "western", segment)
    if p2 and (p1 is None or p2.full_normalized != p1.full_normalized):
        out.append(p2)
    return out


def parse_segment(segment: str) -> List[PersonName]:
    seg = _norm(segment)
    if not seg:
        return []
    for parser in (_parse_comma_segment, _parse_estate_of, _parse_given_status_last, _parse_meck_ampersand):
        found = parser(seg)
        if found:
            return found
    return _parse_last_first_and_western(seg)


def _split_ampersand_people(name: str) -> List[str]:
    n = _norm(name)
    if "&" not in n:
        return [n]
    # Comma-list of people: "POWELL, MARJORIE G HEIRS & GOODSON, BETTY"
    if "," in n:
        parts = [p.strip() for p in n.split("&") if p.strip()]
        return parts
    # Mecklenburg "GIVEN & LAST" is a single person, not two.
    right = n.split("&", 1)[1].strip()
    right_toks = _tokens(re.sub(r"\b(WIFE|HUSBAND|SPOUSE)\b", " ", right, flags=re.I))
    if len(right_toks) == 1 and "," not in n:
        return [n]
    return [p.strip() for p in n.split("&") if p.strip()]



# ---------------------------------------------------------------------------
# Trustees named on the tax roll.
#
# The assessor roll writes a trust's controlling person into the owner string
# itself: "TEW, B DAVID TRUSTEE & TEW, PATRICIA TRUSTEE". That is the same
# authority, and the same shape, as the heir the roll names on an estate parcel
# — which this module already matches. A trustee is the person with authority
# to convey a trust-held parcel, so the roll naming one is evidence of identity,
# not a guess about it.
#
# Only a segment the roll explicitly marks TRUSTEE yields a person. The trust's
# own name never does, and neither does a segment that merely describes the
# trust ("TRUSTEE OF THE SMITH FAMILY TRUST").
# ---------------------------------------------------------------------------

TRUSTEE_TOKEN_RE = re.compile(r"\b(?:CO[\s\-]?)?TRUSTEES?\b", re.I)
TRUSTEE_OF_RE = re.compile(r"^\s*(?:AS\s+)?(?:CO[\s\-]?)?TRUSTEES?\s+(?:OF|FOR|UNDER)\b", re.I)
_LEADING_DIGIT_RE = re.compile(r"^\s*\d")


def _trustee_people_from_segment(segment: str) -> List[PersonName]:
    seg = (segment or "").strip()
    if not seg or not TRUSTEE_TOKEN_RE.search(seg):
        return []
    # "TRUSTEE OF THE ... TRUST" names the trust, not a person.
    if TRUSTEE_OF_RE.search(seg):
        return []
    # "724 KING RICHARD ROAD TRUST" — an address-named trust, not a person.
    if _LEADING_DIGIT_RE.search(seg):
        return []
    stripped = TRUSTEE_TOKEN_RE.sub(" ", seg)
    stripped = re.sub(r"\s+", " ", stripped).strip(" ,-")
    if not stripped:
        return []
    # Anything still carrying TRUST is the entity's own name.
    if re.search(r"\bTRUST\w*\b", stripped, re.I):
        return []
    if is_entity_name(stripped):
        return []
    out: List[PersonName] = []
    for person in parse_segment(stripped):
        if person.deceased or person.life_estate:
            continue
        # A trustee must be named, not initialled: both parts real words.
        if len(person.first) < 2 or len(person.last) < 2:
            continue
        out.append(PersonName(
            first=person.first, last=person.last, middle=person.middle,
            suffix=person.suffix, deceased=False, life_estate=False,
            role="NAMED_TRUSTEE_ON_ROLL", parse_method=person.parse_method,
            raw_segment=person.raw_segment or seg,
        ))
    return out


def trustee_people_on_roll(owner_name: Optional[str]) -> List[PersonName]:
    """Natural persons the roll explicitly names as trustee of this parcel."""
    n = _norm(owner_name or "")
    if not n or not TRUSTEE_TOKEN_RE.search(n):
        return []
    people: List[PersonName] = []
    seen = set()
    for seg in _split_ampersand_people(n):
        for person in _trustee_people_from_segment(seg):
            key = person.full_normalized
            if key in seen:
                continue
            seen.add(key)
            people.append(person)
    return people


def parse_owner_name(owner_name: Optional[str]) -> OwnerParse:
    raw = (owner_name or "").strip()
    if not raw:
        return OwnerParse(raw="", kind="EMPTY")
    n = _norm(raw)
    notes: List[str] = []
    if is_entity_name(n):
        decedent_estate = bool(DECEASED_RE.search(n)) and not REAL_ESTATE_RE.search(n)
        if not (decedent_estate and not CORPORATE_TOKEN_RE.search(n)):
            return OwnerParse(raw=raw, kind="ENTITY", notes=["entity_excluded_from_voter_person_match"])
    if is_trust_name(n) and not DECEASED_RE.search(n):
        trustees = trustee_people_on_roll(n)
        if trustees:
            return OwnerParse(
                raw=raw, kind="TRUST", people=trustees,
                notes=["named_trustee_on_roll"],
            )
        return OwnerParse(raw=raw, kind="TRUST", notes=["trust_excluded_from_voter_person_match"])

    segments = _split_ampersand_people(n)
    people: List[PersonName] = []
    seen = set()
    for i, seg in enumerate(segments):
        parsed = parse_segment(seg)
        for p in parsed:
            # Second+ comma segments without deceased tokens are co-owners/heirs on the roll.
            if i > 0 and "," in n and not p.deceased and not p.life_estate:
                p = PersonName(
                    first=p.first, last=p.last, middle=p.middle, suffix=p.suffix,
                    deceased=False, life_estate=False, role="NAMED_HEIR_ON_ROLL",
                    parse_method=p.parse_method, raw_segment=p.raw_segment,
                )
            key = (p.full_normalized, p.parse_method, p.role, p.deceased)
            if key in seen:
                continue
            seen.add(key)
            people.append(p)

    if not people:
        kind = "ENTITY" if is_entity_name(n) else "UNKNOWN"
        return OwnerParse(raw=raw, kind=kind, notes=["unparsed"])

    living = [p for p in people if p.living_matchable]
    decedents = [p for p in people if p.deceased or p.role == "DECEDENT"]
    life = [p for p in people if p.life_estate]
    if life and not decedents:
        kind = "LIFE_ESTATE"
    elif decedents and living:
        kind = "MIXED"
    elif decedents:
        kind = "DECEASED"
    else:
        kind = "INDIVIDUAL"
    if any(p.role == "NAMED_HEIR_ON_ROLL" for p in living):
        notes.append("named_heir_or_coowner_on_roll")
    return OwnerParse(raw=raw, kind=kind, people=people, notes=notes)

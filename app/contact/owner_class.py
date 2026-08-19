"""Route-class for every CURRENT owner string. Controls $0 source routing."""
from __future__ import annotations

import re
from typing import Optional

from .owner_identity import (
    GOV_RE,
    RELIGIOUS_RE,
    OwnerParse,
    is_entity_name,
    is_trust_name,
    parse_owner_name,
)

ROUTE_CLASSES = (
    "LIVING_PERSON",
    "MULTI_PERSON",
    "DECEASED",
    "ESTATE",
    "HEIRS",
    "TRUST",
    "LLC",
    "CORPORATION",
    "NONPROFIT",
    "GOVERNMENT",
    "OTHER_ENTITY",
    "UNKNOWN",
)

LLC_RE = re.compile(r"\bLLC\b", re.I)
CORP_RE = re.compile(r"\b(INC|INCORPORATED|CORP|CORPORATION)\b", re.I)
ESTATE_WORD_RE = re.compile(r"\bESTATE\b", re.I)


def owner_route_class(owner_name: Optional[str], parsed: Optional[OwnerParse] = None) -> str:
    parsed = parsed or parse_owner_name(owner_name)
    n = (owner_name or "").strip()
    if parsed.kind == "EMPTY" or not n:
        return "UNKNOWN"
    if GOV_RE.search(n):
        return "GOVERNMENT"
    if RELIGIOUS_RE.search(n):
        return "NONPROFIT"
    if parsed.kind == "TRUST" or (is_trust_name(n) and parsed.kind not in {"DECEASED", "MIXED", "INDIVIDUAL"}):
        return "TRUST"
    if parsed.kind == "ENTITY" or (is_entity_name(n) and parsed.kind not in {"DECEASED", "MIXED", "INDIVIDUAL"}):
        if LLC_RE.search(n):
            return "LLC"
        if CORP_RE.search(n):
            return "CORPORATION"
        return "OTHER_ENTITY"
    living_segs = {p.raw_segment or p.full_normalized for p in parsed.living_people}
    decedent_segs = {p.raw_segment or p.full_normalized for p in parsed.decedents}
    if decedent_segs and living_segs:
        return "HEIRS"
    if decedent_segs:
        if ESTATE_WORD_RE.search(n):
            return "ESTATE"
        return "DECEASED"
    if len(living_segs) >= 2:
        return "MULTI_PERSON"
    if living_segs:
        return "LIVING_PERSON"
    return "UNKNOWN"


def phone_role_for(person_role: str, route_class: str, source: str) -> str:
    if source in {"IRS_FORM_990_PUBLIC", "IRS_EO_ICO_NCSBE"}:
        return "ENTITY_MAIN" if source == "IRS_FORM_990_PUBLIC" else "BUSINESS_OFFICE"
    if person_role in {"DECEDENT"}:
        return "OTHER"
    if person_role == "NAMED_TRUSTEE_ON_ROLL":
        # The trustee is the party who can convey a trust-held parcel. Naming the
        # role truthfully matters: this is not the trust's own line, and it is
        # not an "owner" phone.
        return "TRUSTEE_PERSONAL"
    if person_role == "NAMED_HEIR_ON_ROLL":
        return "HEIR_PERSONAL" if route_class in {"HEIRS", "ESTATE", "DECEASED"} else "COOWNER_PERSONAL"
    if person_role in {"LIFE_TENANT", "OWNER", "COOWNER"}:
        return "COOWNER_PERSONAL" if route_class == "MULTI_PERSON" else "OWNER_PERSONAL"
    if person_role == "ENTITY":
        return "ENTITY_MAIN"
    return "OTHER"

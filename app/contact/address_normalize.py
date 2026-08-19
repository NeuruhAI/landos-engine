"""Deterministic US/NC address normalization for identity matching."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_PUNCT = re.compile(r"[.,#;:()\"']")
_WS = re.compile(r"\s+")
_ZIP = re.compile(r"\b(\d{5})(?:-?\d{4})?\b")
_HOUSE = re.compile(r"^(\d+[A-Z]?)\b")

DIRECTIONALS = {
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
}

# Includes Mecklenburg GIS shorthand: AV, BV, CR, PY.
STREET_TYPES = {
    "AVENUE": "AVE", "AVE": "AVE", "AV": "AVE",
    "STREET": "ST", "ST": "ST", "STR": "ST",
    "ROAD": "RD", "RD": "RD",
    "DRIVE": "DR", "DR": "DR", "DRV": "DR",
    "LANE": "LN", "LN": "LN",
    "COURT": "CT", "CT": "CT",
    "CIRCLE": "CIR", "CIR": "CIR", "CR": "CIR",
    "BOULEVARD": "BLVD", "BLVD": "BLVD", "BV": "BLVD", "BL": "BLVD", "BLV": "BLVD",
    "PARKWAY": "PKWY", "PKWY": "PKWY", "PY": "PKWY", "PKY": "PKWY", "PKWAY": "PKWY",
    "PLACE": "PL", "PL": "PL",
    "TERRACE": "TER", "TER": "TER", "TE": "TER",
    "HIGHWAY": "HWY", "HWY": "HWY",
    "TRAIL": "TRL", "TRL": "TRL", "TR": "TRL",
    "WAY": "WAY",
    "LOOP": "LOOP",
    "SQUARE": "SQ", "SQ": "SQ",
    "CROSSING": "XING", "XING": "XING",
    "PIKE": "PIKE",
    "PATH": "PATH",
    "RIDGE": "RDG", "RDG": "RDG",
    "POINT": "PT", "PT": "PT",
    "RUN": "RUN",
    "WALK": "WALK",
    "TRACE": "TRCE", "TRCE": "TRCE",
    "BEND": "BND", "BND": "BND",
    "CREEK": "CRK", "CRK": "CRK",
    "HOLLOW": "HOLW", "HOLW": "HOLW",
    "PARK": "PARK",
    "ROW": "ROW",
    "EXPRESSWAY": "EXPY", "EXPY": "EXPY",
    "FREEWAY": "FWY", "FWY": "FWY",
    "TURNPIKE": "TPKE", "TPKE": "TPKE",
    "ALLEY": "ALY", "ALY": "ALY",
}

UNIT_WORDS = {
    "APT", "APARTMENT", "UNIT", "STE", "SUITE", "BLDG", "BUILDING",
    "FL", "FLOOR", "RM", "ROOM", "LOT", "TRLR", "TRAILER", "SPC", "SPACE",
    "DEPT", "NUM", "NO",
}

DROP_TOKENS = {
    "NC", "USA", "US", "UNINC", "UNINCORPORATED",
}

STATE_NAMES = {
    "NORTH": None,  # handled as directional unless followed by CAROLINA
}


@dataclass(frozen=True, slots=True)
class ParsedAddress:
    raw: str
    house: str
    street_name: str
    street_type: str
    postdir: str
    unit: str
    city: str
    state: str
    zip5: str

    @property
    def street_slug(self) -> str:
        parts = [p for p in (self.street_name, self.street_type, self.postdir) if p]
        return " ".join(parts)

    @property
    def core_key(self) -> str:
        return "|".join([self.house, self.street_slug, self.zip5])

    @property
    def house_street_zip(self) -> tuple[str, str, str]:
        return (self.house, self.street_slug, self.zip5)

    @property
    def is_empty(self) -> bool:
        return not self.house and not self.street_name


def _clean(raw: str) -> str:
    text = _PUNCT.sub(" ", (raw or "").upper())
    text = text.replace("/", " ")
    return _WS.sub(" ", text).strip()


def extract_zip5(*parts: Optional[str]) -> str:
    for part in parts:
        if not part:
            continue
        matches = _ZIP.findall(str(part).upper().replace(" ", ""))
        if not matches:
            matches = _ZIP.findall(_clean(str(part)))
        if matches:
            return matches[-1]
    return ""


def parse_address(
    street: Optional[str],
    city: Optional[str] = None,
    state: Optional[str] = None,
    zipcode: Optional[str] = None,
) -> ParsedAddress:
    raw = " ".join(p for p in [street or "", city or "", state or "", zipcode or ""] if str(p).strip())
    zip5 = extract_zip5(zipcode, street, city, raw)
    city_n = _clean(city or "")
    state_n = _clean(state or "")
    if state_n in {"NORTH CAROLINA"}:
        state_n = "NC"
    if len(state_n) > 2 and state_n.endswith(" NC"):
        state_n = "NC"

    line = _clean(street or "")
    if zip5:
        line = re.sub(rf"\b{zip5}(?:-?\d{{4}})?\b", " ", line).strip()
        line = _WS.sub(" ", line)

    # Strip trailing city/state if they appear on the street line.
    if city_n:
        line = re.sub(rf"\b{re.escape(city_n)}\b", " ", line)
        line = _WS.sub(" ", line).strip()
    line = re.sub(r"\bNORTH CAROLINA\b", " ", line)
    line = re.sub(r"\bNC\b", " ", line)
    line = _WS.sub(" ", line).strip()

    tokens = [t for t in line.split(" ") if t and t not in DROP_TOKENS]
    house = ""
    if tokens and _HOUSE.match(tokens[0]):
        house = tokens[0]
        tokens = tokens[1:]

    unit = ""
    unit_idx = None
    for i, tok in enumerate(tokens):
        if tok in UNIT_WORDS:
            unit_idx = i
            break
    if unit_idx is not None:
        unit = " ".join(tokens[unit_idx:])
        tokens = tokens[:unit_idx]

    postdir = ""
    street_type = ""
    type_idx = None
    for i, tok in enumerate(tokens):
        if tok in STREET_TYPES:
            type_idx = i
    if type_idx is not None:
        street_type = STREET_TYPES[tokens[type_idx]]
        after = tokens[type_idx + 1 :]
        before = tokens[:type_idx]
        if after:
            lead = after[0]
            if lead in DIRECTIONALS or lead in DIRECTIONALS.values():
                postdir = DIRECTIONALS.get(lead, lead)
                after = after[1:]
        # Trailing leftover city (MATTHEWS TOWNSHIP PY MATTHEWS)
        if after and (after[-1] == city_n or after[-1] in before):
            after = after[:-1]
        tokens = before
        if tokens and (tokens[0] in DIRECTIONALS or tokens[0] in DIRECTIONALS.values()):
            tokens[0] = DIRECTIONALS.get(tokens[0], tokens[0])
    elif tokens:
        last = tokens[-1]
        if last in DIRECTIONALS or last in DIRECTIONALS.values():
            postdir = DIRECTIONALS.get(last, last)
            tokens = tokens[:-1]
        if tokens and (tokens[0] in DIRECTIONALS or tokens[0] in DIRECTIONALS.values()):
            tokens[0] = DIRECTIONALS.get(tokens[0], tokens[0])
    street_name = " ".join(tokens)
    if city_n in {"UNINC", "UNINCORPORATED"}:
        city_n = ""
    return ParsedAddress(
        raw=raw,
        house=house,
        street_name=street_name,
        street_type=street_type,
        postdir=postdir,
        unit=unit,
        city=city_n,
        state=state_n[:2] if state_n else "",
        zip5=zip5,
    )


def exact_core_match(a: ParsedAddress, b: ParsedAddress) -> bool:
    if a.is_empty or b.is_empty:
        return False
    if not a.house or not b.house or a.house != b.house:
        return False
    if not a.street_name or not b.street_name:
        return False
    if a.street_slug != b.street_slug:
        return False
    if a.zip5 and b.zip5 and a.zip5 != b.zip5:
        return False
    if not a.zip5 or not b.zip5:
        # ZIP missing: require city corroboration when both have city.
        if a.city and b.city and a.city != b.city:
            return False
    return True


def house_street_zip_match(a: ParsedAddress, b: ParsedAddress) -> bool:
    if not a.house or not b.house or a.house != b.house:
        return False
    if not a.street_name or not b.street_name:
        return False
    if a.street_name != b.street_name:
        return False
    if a.street_type and b.street_type and a.street_type != b.street_type:
        return False
    if not a.zip5 or not b.zip5 or a.zip5 != b.zip5:
        return False
    return True


def independent_components(a: ParsedAddress, b: ParsedAddress) -> int:
    """Count agreeing independent address components (house, street, zip, city)."""
    n = 0
    if a.house and b.house and a.house == b.house:
        n += 1
    if a.street_name and b.street_name and a.street_name == b.street_name:
        n += 1
    if a.zip5 and b.zip5 and a.zip5 == b.zip5:
        n += 1
    if a.city and b.city and a.city == b.city:
        n += 1
    return n

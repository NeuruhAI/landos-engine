"""US phone normalization aligned to the T12 libphonenumber-js contract.

libphonenumber validity is numbering-plan validity only. It is not proof the
number is currently assigned, right-party, or callable. Those flags stay
independent (dnc_status / callability_status).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberType

PHONE_CLASSES = (
    "VALID_US",
    "INVALID",
    "SYNTHETIC_555",
    "MALFORMED",
    "MISSING",
)

TYPE_NAMES = {
    PhoneNumberType.FIXED_LINE: "FIXED_LINE",
    PhoneNumberType.MOBILE: "MOBILE",
    PhoneNumberType.FIXED_LINE_OR_MOBILE: "FIXED_LINE_OR_MOBILE",
    PhoneNumberType.TOLL_FREE: "TOLL_FREE",
    PhoneNumberType.PREMIUM_RATE: "PREMIUM_RATE",
    PhoneNumberType.SHARED_COST: "SHARED_COST",
    PhoneNumberType.VOIP: "VOIP",
    PhoneNumberType.PERSONAL_NUMBER: "PERSONAL_NUMBER",
    PhoneNumberType.PAGER: "PAGER",
    PhoneNumberType.UAN: "UAN",
    PhoneNumberType.VOICEMAIL: "VOICEMAIL",
    PhoneNumberType.UNKNOWN: "UNKNOWN",
}


@dataclass(frozen=True, slots=True)
class NormalizedPhone:
    e164: Optional[str]
    national: Optional[str]
    classification: str
    number_type: str
    numbering_plan_valid: bool
    currently_assigned: str
    right_party: str
    callable: str


def digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def is_synthetic_555(value: str) -> bool:
    n = digits_only(value)
    if len(n) == 11 and n.startswith("1"):
        n = n[1:]
    if len(n) != 10:
        return False
    return n[:3] == "555" or n[3:6] == "555"


def is_repeated_digits(value: str) -> bool:
    n = digits_only(value)
    if len(n) == 11 and n.startswith("1"):
        n = n[1:]
    if len(n) != 10:
        return False
    return len(set(n)) == 1


def normalize_us_phone(raw: Optional[str]) -> NormalizedPhone:
    input_value = "" if raw is None else str(raw).strip()
    if not input_value:
        return NormalizedPhone(None, None, "MISSING", "UNKNOWN", False, "UNKNOWN", "UNKNOWN", "UNKNOWN")
    if is_synthetic_555(input_value):
        return NormalizedPhone(None, None, "SYNTHETIC_555", "UNKNOWN", False, "UNKNOWN", "UNKNOWN", "UNKNOWN")
    if is_repeated_digits(input_value):
        return NormalizedPhone(None, None, "INVALID", "UNKNOWN", False, "UNKNOWN", "UNKNOWN", "UNKNOWN")
    try:
        parsed = phonenumbers.parse(input_value, "US")
    except NumberParseException:
        return NormalizedPhone(None, None, "MALFORMED", "UNKNOWN", False, "UNKNOWN", "UNKNOWN", "UNKNOWN")
    national = str(parsed.national_number) if parsed.national_number else ""
    if is_synthetic_555(national):
        return NormalizedPhone(None, None, "SYNTHETIC_555", "UNKNOWN", False, "UNKNOWN", "UNKNOWN", "UNKNOWN")
    if is_repeated_digits(national):
        return NormalizedPhone(None, None, "INVALID", "UNKNOWN", False, "UNKNOWN", "UNKNOWN", "UNKNOWN")
    region = phonenumbers.region_code_for_number(parsed)
    valid = phonenumbers.is_valid_number(parsed) and region == "US"
    if not valid:
        return NormalizedPhone(None, None, "INVALID", "UNKNOWN", False, "UNKNOWN", "UNKNOWN", "UNKNOWN")
    ntype = TYPE_NAMES.get(phonenumbers.number_type(parsed), "UNKNOWN")
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return NormalizedPhone(
        e164=e164,
        national=national,
        classification="VALID_US",
        number_type=ntype,
        numbering_plan_valid=True,
        currently_assigned="UNKNOWN",
        right_party="UNKNOWN",
        callable="UNKNOWN",
    )

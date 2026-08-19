"""Read estate columns for what they actually contain, not for what they are named.

`property_zip` on the NC estate is not the property's ZIP. Measured across all
20,734 CURRENT NC rows: it equals `mailing_zip` on 14,061 of 14,061 populated
rows (100%), and on the 2,442 rows whose property address line carries its own
ZIP, the field never agrees with that line — it agrees with the mailing ZIP
every time. Passing it to the matcher as the property ZIP makes an absentee
owner's out-of-state mailing ZIP masquerade as the parcel's ZIP, which then
blocks the exact-address match against the voter file.

The property's ZIP is taken from the property address line, which carries a real
one on 8,528 rows. When the line has none, the property ZIP is UNKNOWN, and
saying so lets city corroboration decide instead of a wrong ZIP forcing a miss.
"""
from __future__ import annotations

from .address_normalize import extract_zip5, parse_address, strip_leading_house


def property_zip_for(row: dict) -> str:
    """The parcel's own ZIP, or "" when the estate does not actually carry one."""
    # Strip the house number first: a five-digit house is not a ZIP.
    line_zip = extract_zip5(
        strip_leading_house(row.get("property_address_raw")),
        strip_leading_house(row.get("property_address_normalized")),
    )
    if line_zip:
        return line_zip
    declared = str(row.get("property_zip") or "").strip()
    mailing = str(row.get("mailing_zip") or "").strip()
    if not declared:
        return ""
    # A declared property ZIP that merely copies the mailing ZIP is trustworthy
    # in exactly one case: the owner is at the property, so the two addresses are
    # the same place and the one ZIP describes both.
    if declared == mailing and not owner_occupies_property(row):
        return ""
    return extract_zip5(declared)


def _street_key(street: str, city: str = "") -> tuple:
    parsed = parse_address(street, city)
    # Spaces are dropped so FARM RIDGE and FARMRIDGE are one street, and the
    # street TYPE is ignored so BARNSDALE LN and BARNSDALE RD still compare.
    return (parsed.house, parsed.street_name.replace(" ", ""))


def owner_occupies_property(row: dict) -> bool:
    """Does the owner's mailing address name the subject property itself?"""
    prop = _street_key(row.get("property_address_raw") or
                       row.get("property_address_normalized") or "",
                       row.get("property_city") or "")
    mail = _street_key(row.get("mailing_address") or "", row.get("mailing_city") or "")
    if not prop[0] or not prop[1] or not mail[0] or not mail[1]:
        return False
    return prop == mail


def property_zip_is_mailing_copy(row: dict) -> bool:
    declared = str(row.get("property_zip") or "").strip()
    mailing = str(row.get("mailing_zip") or "").strip()
    return bool(declared) and declared == mailing

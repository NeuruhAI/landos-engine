"""$0 entity identity: SOS import adapter (no scripted search) + IRS EO/990.

NC SOS interactive search is not called from this module. Official SOS master
files are paid subscriptions and are not used. Operators may drop a lawfully
downloaded listing/filing extract matching SOS_IMPORT_COLUMNS.

Nonprofit/church phones come from official IRS Form 990 XML for EINs first
matched on the free EO BMF extract (name + address corroboration). Common Crawl
is used only against a website domain that the 990 itself verified.
"""
from __future__ import annotations

import csv
import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional
from urllib.parse import urlparse

from .address_normalize import extract_zip5, parse_address, exact_core_match, house_street_zip_match
from .owner_identity import is_entity_name, norm_upper

SOS_IMPORT_COLUMNS = (
    "entity_legal_name",
    "sos_entity_id",
    "registered_agent",
    "principal_office",
    "mailing_office",
    "official_name",
    "official_title",
    "verified_domain",
    "source_document",
    "source_date",
    "property_candidate_id",
    "apn",
    "county",
    "fips",
)

PHONE_RE = re.compile(r"(?:\+?1[-.\s]*)?\(?\d{3}\)?[-.\s]*\d{3}[-.\s]*\d{4}")


def org_key(name: str) -> str:
    text = norm_upper(name)
    text = re.sub(
        r"\b(THE|LLC|INC|INCORPORATED|CORP|CORPORATION|LTD|LIMITED|LP|LLP|"
        r"PLLC|CO|COMPANY|NA|N A)\b",
        " ",
        text,
    )
    text = re.sub(r"[^A-Z0-9 ]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def names_corroborate(a: str, b: str) -> bool:
    ka, kb = org_key(a), org_key(b)
    if not ka or not kb:
        return False
    if ka == kb or ka in kb or kb in ka:
        return True
    ta, tb = set(ka.split()), set(kb.split())
    if not ta or not tb:
        return False
    inter = len(ta & tb)
    return inter >= 2 and inter / min(len(ta), len(tb)) >= 0.8


@dataclass
class IrsEoRecord:
    ein: str
    name: str
    ico: str
    street: str
    city: str
    state: str
    zip5: str


@dataclass
class PublicBusinessPhone:
    candidate_id: str
    apn: str
    county: str
    fips: str
    owner_name: str
    phone: str
    phone_source: str
    source_record_identity: str
    source_hash: str
    observed_at: str
    match_tier: str
    match_confidence: str
    verified_domain: str
    ein: str
    corroboration: str


def load_eo_bmf(path: str | Path) -> List[IrsEoRecord]:
    p = Path(path)
    if not p.exists():
        return []
    out: List[IrsEoRecord] = []
    with p.open(newline="", encoding="latin-1") as fh:
        for row in csv.DictReader(fh):
            zip5 = extract_zip5(row.get("ZIP"), row.get("zip"))
            out.append(
                IrsEoRecord(
                    ein=str(row.get("EIN") or "").strip(),
                    name=str(row.get("NAME") or "").strip(),
                    ico=str(row.get("ICO") or "").strip(),
                    street=str(row.get("STREET") or "").strip(),
                    city=str(row.get("CITY") or "").strip(),
                    state=str(row.get("STATE") or "").strip(),
                    zip5=zip5,
                )
            )
    return out


def match_entity_to_eo(
    owner_name: str,
    mailing_street: str,
    mailing_city: str,
    mailing_state: str,
    mailing_zip: str,
    property_street: str,
    eo_index: dict[str, List[IrsEoRecord]],
) -> Optional[IrsEoRecord]:
    key = org_key(owner_name)
    if not key:
        return None
    cands = list(eo_index.get(key, []))
    # Prefix buckets: first two tokens.
    toks = key.split()
    if len(toks) >= 2:
        cands.extend(eo_index.get(" ".join(toks[:2]), []))
    # Dedup by EIN
    by_ein = {c.ein: c for c in cands if c.ein}
    owner_mail = parse_address(mailing_street, mailing_city, mailing_state, mailing_zip)
    owner_prop = parse_address(property_street, mailing_city, mailing_state, mailing_zip)
    hits = []
    for rec in by_ein.values():
        if not names_corroborate(owner_name, rec.name):
            continue
        rec_addr = parse_address(rec.street, rec.city, rec.state, rec.zip5)
        zip_ok = bool(owner_mail.zip5 and rec.zip5 and owner_mail.zip5 == rec.zip5)
        city_ok = bool(owner_mail.city and rec.city and owner_mail.city == rec.city.upper())
        street_ok = exact_core_match(owner_mail, rec_addr) or house_street_zip_match(owner_mail, rec_addr)
        prop_ok = exact_core_match(owner_prop, rec_addr) or (
            owner_prop.zip5 and rec.zip5 and owner_prop.zip5 == rec.zip5 and names_corroborate(owner_name, rec.name)
        )
        if street_ok or zip_ok or (city_ok and names_corroborate(owner_name, rec.name) and len(org_key(owner_name).split()) >= 3):
            hits.append(rec)
        elif prop_ok and zip_ok:
            hits.append(rec)
    if len(hits) == 1:
        return hits[0]
    return None


def build_eo_index(records: Iterable[IrsEoRecord]) -> dict[str, List[IrsEoRecord]]:
    idx: dict[str, List[IrsEoRecord]] = {}
    for rec in records:
        key = org_key(rec.name)
        if not key:
            continue
        idx.setdefault(key, []).append(rec)
        toks = key.split()
        if len(toks) >= 2:
            idx.setdefault(" ".join(toks[:2]), []).append(rec)
    return idx


def extract_990_contact(xml_bytes: bytes) -> dict:
    """Pull business name, phone, website, address from a public 990 XML."""
    root = ET.fromstring(xml_bytes)
    def texts(*names: str) -> List[str]:
        found = []
        for node in root.iter():
            tag = node.tag.split("}")[-1]
            if tag in names and (node.text or "").strip():
                found.append(node.text.strip())
        return found
    phones = texts("PhoneNum", "PhoneNumberTxt")
    websites = texts("WebsiteAddressTxt", "WebsiteAddress")
    names = texts("BusinessNameLine1Txt", "BusinessNameLine1", "FilerName")
    us_phone = ""
    for p in phones:
        if PHONE_RE.search(p):
            us_phone = p
            break
    domain = ""
    if websites:
        raw = websites[0]
        if not raw.startswith("http"):
            raw = "http://" + raw
        host = urlparse(raw).hostname or ""
        domain = host.lower().lstrip("www.")
    return {
        "name": names[0] if names else "",
        "phone": us_phone,
        "domain": domain,
        "street": (texts("AddressLine1Txt") or [""])[0],
        "city": (texts("CityNm") or [""])[0],
        "state": (texts("StateAbbreviationCd") or [""])[0],
        "zip": (texts("ZIPCd") or [""])[0],
    }


def load_sos_import_csv(path: str | Path) -> List[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def domain_from_sos_row(row: dict) -> str:
    return (row.get("verified_domain") or "").strip().lower().lstrip("www.")


ATTORNEY_LIKE_RE = re.compile(
    r"\b(ESQ|ESQUIRE|ATTORNEY|LAWYER|COUNSEL|LAW FIRM|REGISTERED AGENT)\b",
    re.I,
)
ICO_PREFIX_RE = re.compile(r"^[%\s]*(?:C/?O|CARE OF)?\s*", re.I)


def parse_ico_people(ico: str) -> List:
    from .owner_identity import parse_owner_name
    raw = (ico or "").strip()
    if not raw or ATTORNEY_LIKE_RE.search(raw):
        return []
    cleaned = ICO_PREFIX_RE.sub("", raw).strip()
    if not cleaned or ATTORNEY_LIKE_RE.search(cleaned):
        return []
    parsed = parse_owner_name(cleaned)
    return list(parsed.living_people)


def source_id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:20]

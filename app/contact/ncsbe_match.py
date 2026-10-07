"""Deterministic NCSBE identity matching. Name-only matches are never accepted."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence

from .address_normalize import (
    ParsedAddress,
    exact_core_match,
    house_street_zip_match,
    independent_components,
    parse_address,
)
from .owner_identity import PersonName
from .phone_normalize import NormalizedPhone, normalize_us_phone

ACTIVE_STATUSES = {"ACTIVE", "INACTIVE", "TEMPORARY"}
CONFIDENTIAL_YES = {"Y", "YES", "1", "TRUE"}


@dataclass(slots=True)
class VoterRecord:
    county_id: str
    county_desc: str
    voter_reg_num: str
    last_name: str
    first_name: str
    middle_name: str
    suffix: str
    status: str
    res: ParsedAddress
    mail: ParsedAddress
    phone_raw: str
    confidential: str
    source_file: str
    source_hash: str
    observed_at: str

    @property
    def identity_hash(self) -> str:
        payload = f"{self.county_id}|{self.voter_reg_num}|{self.source_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    @property
    def person_key(self) -> tuple[str, str, str, str]:
        return (self.last_name, self.first_name, self.middle_name[:1], self.suffix)


@dataclass
class MatchDecision:
    accepted: bool
    tier: str
    confidence: str
    reason: str
    voter: Optional[VoterRecord] = None
    phone: Optional[NormalizedPhone] = None
    reject_class: str = ""


def _u(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def names_compatible(person: PersonName, voter: VoterRecord, require_strong: bool) -> tuple[bool, bool]:
    """Return (compatible, strong).

    strong = exact first + exact last (not initial-only first).
    """
    v_last, v_first = _u(voter.last_name), _u(voter.first_name)
    v_mid, v_suf = _u(voter.middle_name), _u(voter.suffix)
    p_last, p_first = _u(person.last), _u(person.first)
    p_mid, p_suf = _u(person.middle), _u(person.suffix)
    if not v_last or not v_first or v_last != p_last:
        return False, False
    first_exact = v_first == p_first
    first_initial = (
        (len(p_first) == 1 and v_first.startswith(p_first))
        or (len(v_first) == 1 and p_first.startswith(v_first))
    )
    if not first_exact and not first_initial:
        return False, False
    if p_mid and v_mid:
        p_m = p_mid.split()[0]
        v_m = v_mid.split()[0]
        mid_ok = p_m == v_m or p_m[:1] == v_m[:1]
        if not mid_ok:
            return False, False
    if p_suf and v_suf and p_suf != v_suf:
        return False, False
    if p_suf and not v_suf and p_suf in {"JR", "SR", "II", "III", "IV"}:
        # Suffix on owner, none on voter: father/son risk. Not strong.
        strong = False
    else:
        strong = first_exact and len(p_first) > 1 and len(v_first) > 1
    if require_strong and not strong:
        return False, False
    return True, strong


def _addr_tier(
    person_addrs: Sequence[tuple[str, ParsedAddress]],
    voter: VoterRecord,
    county: str,
) -> tuple[str, str]:
    """Return (tier, which_address) or ("", "")."""
    v_addrs = [("res", voter.res), ("mail", voter.mail)]
    county_u = _u(county)
    voter_county = _u(voter.county_desc)
    for label, addr in person_addrs:
        if addr.is_empty:
            continue
        for vlabel, vaddr in v_addrs:
            if exact_core_match(addr, vaddr):
                return "TIER_A", f"{label}:{vlabel}"
    for label, addr in person_addrs:
        if addr.is_empty:
            continue
        for vlabel, vaddr in v_addrs:
            if house_street_zip_match(addr, vaddr):
                county_ok = (not county_u) or county_u == voter_county
                if county_ok:
                    return "TIER_B", f"{label}:{vlabel}"
    best = 0
    best_which = ""
    for label, addr in person_addrs:
        for vlabel, vaddr in v_addrs:
            n = independent_components(addr, vaddr)
            if n > best:
                best = n
                best_which = f"{label}:{vlabel}"
    if best >= 3:
        return "TIER_C", best_which
    return "", ""


def collapse_same_person(voters: Sequence[VoterRecord]) -> List[VoterRecord]:
    by_id = {}
    for v in voters:
        by_id.setdefault(v.identity_hash, v)
    # Also collapse identical person_key + same res house/zip.
    out: List[VoterRecord] = []
    seen = set()
    for v in by_id.values():
        k = (v.person_key, v.res.house, v.res.zip5, v.phone_raw)
        if k in seen:
            continue
        seen.add(k)
        out.append(v)
    return out


def match_person_to_voters(
    person: PersonName,
    property_addr: ParsedAddress,
    mailing_addr: ParsedAddress,
    county: str,
    voters: Sequence[VoterRecord],
) -> MatchDecision:
    if not person.living_matchable:
        return MatchDecision(False, "", "", "deceased_or_not_matchable", reject_class="DECEASED_OWNER")
    addrs = [("property", property_addr), ("mailing", mailing_addr)]
    compatible: List[tuple[VoterRecord, bool, str, str]] = []
    for voter in voters:
        if _u(voter.confidential) in CONFIDENTIAL_YES:
            continue
        if _u(voter.status) not in ACTIVE_STATUSES:
            continue
        ok, strong = names_compatible(person, voter, require_strong=False)
        if not ok:
            continue
        tier, which = _addr_tier(addrs, voter, county)
        if not tier:
            continue
        compatible.append((voter, strong, tier, which))

    if not compatible:
        return MatchDecision(False, "", "", "no_address_corroborated_name_match", reject_class="NO_MATCH")

    # Name-only is already excluded because _addr_tier required address components.
    # Ambiguity: distinct people (different person_key) at conflicting addresses.
    collapsed = collapse_same_person([c[0] for c in compatible])
    people_keys = {v.person_key for v in collapsed}
    houses = {(v.res.house, v.res.zip5) for v in collapsed if v.res.house}

    if len(people_keys) > 1:
        return MatchDecision(False, "", "", "multiple_plausible_voters", reject_class="AMBIGUOUS")
    if len(houses) > 1:
        return MatchDecision(False, "", "", "address_conflict", reject_class="ADDRESS_CONFLICT")

    # Pick the strongest tier among remaining.
    order = {"TIER_A": 0, "TIER_B": 1, "TIER_C": 2}
    compatible.sort(key=lambda x: (order[x[2]], 0 if x[1] else 1))
    voter, strong, tier, which = compatible[0]

    if person.is_common_name and not strong:
        return MatchDecision(False, tier, "", "common_name_initial_only", reject_class="COMMON_NAME")
    if person.is_common_name and tier == "TIER_C":
        return MatchDecision(False, tier, "", "common_name_tier_c_suppressed", reject_class="COMMON_NAME")

    if tier == "TIER_C" and not (person.middle or person.suffix or voter.middle_name):
        return MatchDecision(False, "TIER_C", "", "tier_c_lacks_middle_or_suffix", reject_class="LOW_CONFIDENCE")
    if tier == "TIER_C":
        # Conservative: do not append TIER_C unless unique + middle/suffix + 3 components.
        # Still require strong first+last.
        if not strong:
            return MatchDecision(False, "TIER_C", "", "tier_c_name_not_strong", reject_class="LOW_CONFIDENCE")

    if not strong and tier != "TIER_A":
        return MatchDecision(False, tier, "", "name_not_strong_below_tier_a", reject_class="WEAK_NAME")

    phone = normalize_us_phone(voter.phone_raw)
    confidence = "HIGH" if tier in {"TIER_A", "TIER_B"} else "MEDIUM"
    if tier == "TIER_C":
        # User: if confidence is not high enough, do not append that tier.
        # MEDIUM is not high enough for append.
        return MatchDecision(
            False, "TIER_C", "MEDIUM", f"tier_c_not_appended:{which}", voter=voter,
            phone=phone, reject_class="TIER_C_HELD",
        )
    return MatchDecision(
        accepted=True,
        tier=tier,
        confidence=confidence,
        reason=f"{tier}:{which}:strong={strong}",
        voter=voter,
        phone=phone,
    )


def voter_from_ncsbe_row(row: dict, source_file: str, source_hash: str, observed_at: str) -> VoterRecord:
    res = parse_address(
        row.get("res_street_address"),
        row.get("res_city_desc"),
        row.get("state_cd"),
        row.get("zip_code"),
    )
    mail = parse_address(
        row.get("mail_addr1"),
        row.get("mail_city"),
        row.get("mail_state"),
        row.get("mail_zipcode"),
    )
    return VoterRecord(
        county_id=str(row.get("county_id") or "").strip(),
        county_desc=str(row.get("county_desc") or "").strip(),
        voter_reg_num=str(row.get("voter_reg_num") or "").strip(),
        last_name=_u(row.get("last_name")),
        first_name=_u(row.get("first_name")),
        middle_name=_u(row.get("middle_name")),
        suffix=_u(row.get("name_suffix_lbl")),
        status=_u(row.get("voter_status_desc") or row.get("status_cd")),
        res=res,
        mail=mail,
        phone_raw=str(row.get("full_phone_number") or "").strip(),
        confidential=str(row.get("confidential_ind") or "").strip(),
        source_file=source_file,
        source_hash=source_hash,
        observed_at=observed_at,
    )

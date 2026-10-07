"""Quality court + unit tests for $0 NC NCSBE phone enrichment."""
from __future__ import annotations

import csv
import io

from app.contact.address_normalize import parse_address, exact_core_match, house_street_zip_match
from app.contact.estate_import import parse_estate_import_rows, FORBIDDEN_RELATIONSHIPS
from app.contact.ncsbe_match import VoterRecord, match_person_to_voters, voter_from_ncsbe_row
from app.contact.owner_identity import parse_owner_name, is_entity_name
from app.contact.phone_normalize import normalize_us_phone, is_synthetic_555
from app.contact.t12_append import (
    PhoneAppendIdentity,
    evaluate_phone_append,
    validate_append_row,
    COMBINED_COLUMNS,
    source_row_id_for,
)


def _voter(**kwargs) -> VoterRecord:
    res = parse_address(
        kwargs.pop("res_street", "4042 REDWOOD AVE"),
        kwargs.pop("res_city", "CHARLOTTE"),
        kwargs.pop("res_state", "NC"),
        kwargs.pop("res_zip", "28205"),
    )
    mail = parse_address(
        kwargs.pop("mail_street", "4042 REDWOOD AVE"),
        kwargs.pop("mail_city", "CHARLOTTE"),
        kwargs.pop("mail_state", "NC"),
        kwargs.pop("mail_zip", "28205"),
    )
    return VoterRecord(
        county_id=kwargs.pop("county_id", "60"),
        county_desc=kwargs.pop("county_desc", "MECKLENBURG"),
        voter_reg_num=kwargs.pop("voter_reg_num", "1001"),
        last_name=kwargs.pop("last_name", "WOLF"),
        first_name=kwargs.pop("first_name", "RANDALL"),
        middle_name=kwargs.pop("middle_name", "A"),
        suffix=kwargs.pop("suffix", ""),
        status=kwargs.pop("status", "ACTIVE"),
        res=res,
        mail=mail,
        phone_raw=kwargs.pop("phone_raw", "7045551212"),
        confidential=kwargs.pop("confidential", "N"),
        source_file=kwargs.pop("source_file", "ncvoter60.txt"),
        source_hash=kwargs.pop("source_hash", "abc"),
        observed_at=kwargs.pop("observed_at", "2026-08-16T00:12:55Z"),
    )


def test_libphonenumber_normalization_and_555():
    ok = normalize_us_phone("(704) 331-1000")
    assert ok.classification == "VALID_US"
    assert ok.e164 == "+17043311000"
    assert ok.numbering_plan_valid is True
    assert ok.currently_assigned == "UNKNOWN"
    assert ok.right_party == "UNKNOWN"
    assert ok.callable == "UNKNOWN"
    assert is_synthetic_555("704-555-1212")
    assert normalize_us_phone("704-555-1212").classification == "SYNTHETIC_555"
    assert normalize_us_phone("555-123-4567").classification == "SYNTHETIC_555"
    assert normalize_us_phone("").classification == "MISSING"
    assert normalize_us_phone("12").classification in {"MALFORMED", "INVALID"}
    assert normalize_us_phone("0000000000").classification == "INVALID"


def test_t12_import_schema_validation():
    ident = PhoneAppendIdentity(
        candidate_id="cand-1",
        source_row_id="NC:37119:09104330",
        apn="09104330",
        fips="37119",
        county="Mecklenburg",
        owner_name="RANDALL A WOLF",
    )
    good = {
        "candidate_id": "cand-1",
        "apn": "9104330",  # leading-zero strip
        "phone": "7043311000",
        "county": "Mecklenburg",
        "fips": "37119",
        "phone_source": "NCSBE_PUBLIC_VOTER_FILE",
    }
    accepted = evaluate_phone_append(ident, good)
    assert accepted["ok"] is True
    assert accepted["e164"] == "+17043311000"
    assert accepted["match_confidence"] == "STRONG_IDENTITY"
    assert evaluate_phone_append(ident, {**good, "candidate_id": "other"})["reason"] == "CANDIDATE_MISMATCH"
    assert evaluate_phone_append(ident, {**good, "apn": "999"})["reason"] == "APN_MISMATCH"
    assert evaluate_phone_append(ident, {**good, "fips": "37183"})["reason"] == "COUNTY_FIPS_MISMATCH"
    assert evaluate_phone_append(ident, {**good, "phone": "7045550100"})["reason"] == "SYNTHETIC_555"
    assert evaluate_phone_append(ident, {**good, "phone": ""})["reason"] == "MISSING_PHONE"
    assert evaluate_phone_append(ident, good, ambiguous=True)["reason"] == "AMBIGUOUS_IDENTITY"
    row = validate_append_row(good, ident)
    assert row["ok"] is True
    for col in ("candidate_id", "apn", "phone", "county", "fips"):
        assert col in COMBINED_COLUMNS
    assert source_row_id_for("37119", "09104330") == "NC:37119:09104330"


def test_suffix_and_middle_name_parsing():
    wake = parse_owner_name("FARRIS, JOHN A JR HEIRS")
    dec = [p for p in wake.people if p.last == "FARRIS" and p.first == "JOHN"]
    assert dec
    assert dec[0].suffix == "JR"
    assert dec[0].middle.startswith("A")
    assert dec[0].deceased is True
    life = parse_owner_name("KATIE W (LIFE ESTATE) CLAWSON")
    assert life.kind == "LIFE_ESTATE"
    p = life.living_people[0]
    assert p.last == "CLAWSON" and p.first == "KATIE" and p.life_estate is True
    meck = parse_owner_name("RANDALL A WOLF")
    names = {(x.first, x.last) for x in meck.living_people}
    assert ("RANDALL", "WOLF") in names or ("A", "RANDALL") in names
    # western hypothesis RANDALL WOLF must exist
    assert any(x.first == "RANDALL" and x.last == "WOLF" for x in meck.people)


def test_comma_mailing_and_meck_heir_order():
    p = parse_owner_name("ASPDEN, RUTH N HEIRS")
    assert p.kind == "DECEASED"
    assert p.decedents[0].last == "ASPDEN" and p.decedents[0].first == "RUTH"
    guy = parse_owner_name("WILLIE C HEIRS GUY")
    assert any(x.last == "GUY" and x.first == "WILLIE" and x.deceased for x in guy.people)
    estate = parse_owner_name("ESTATE OF REGINALD M SANDERS")
    assert any(x.last == "SANDERS" and x.first == "REGINALD" and x.deceased for x in estate.people)


def test_entity_exclusion_from_voter_person_matching():
    assert is_entity_name("PDS REAL ESTATE LLC")
    assert parse_owner_name("PDS REAL ESTATE LLC").kind == "ENTITY"
    assert parse_owner_name("FRANCHISE REALTY INTERSTATE CORPORATION").kind == "ENTITY"
    assert parse_owner_name("PEOPLE UNITED HOUSE OF PRAYER FOR ALL").kind == "ENTITY"
    assert parse_owner_name("ALP REVOCABLE LIVING TRUST").kind == "TRUST"
    assert parse_owner_name("STATE EMPLOYEES CREDIT UNION").kind == "ENTITY"
    # Decedent estate is NOT an LLC entity
    assert parse_owner_name("SANDRA H NEWTON ESTATE").kind == "DECEASED"
    allen = parse_owner_name("PHILIP ALLEN")
    assert any(p.first == "PHILIP" and p.last == "ALLEN" for p in allen.living_people)
    alleyne = parse_owner_name("ELVIA ALLEYNE")
    assert any(p.first == "ELVIA" and p.last == "ALLEYNE" for p in alleyne.living_people)


def test_deceased_owner_exclusion_and_heir_on_roll():
    mixed = parse_owner_name("POWELL, MARJORIE G HEIRS & GOODSON, BETTY")
    assert mixed.kind == "MIXED"
    decedents = mixed.decedents
    living = mixed.living_people
    assert any(p.last == "POWELL" and p.first == "MARJORIE" for p in decedents)
    assert any(p.last == "GOODSON" and p.first == "BETTY" and p.role == "NAMED_HEIR_ON_ROLL" for p in living)
    # Decedent is not living-matchable
    assert all(not p.living_matchable for p in decedents)

    ident = _voter(last_name="POWELL", first_name="MARJORIE", middle_name="G",
                   phone_raw="9193311000")
    prop = parse_address("100 MAIN ST", "RALEIGH", "NC", "27610")
    mail = parse_address("15 KNOLLWOOD DR", "WEST ORANGE", "NJ", "07052")
    marjorie = [p for p in decedents if p.last == "POWELL"][0]
    decision = match_person_to_voters(marjorie, prop, mail, "Wake", [ident])
    assert decision.accepted is False
    assert decision.reject_class == "DECEASED_OWNER"


def test_exact_name_exact_address_accept():
    person = [p for p in parse_owner_name("RANDALL A WOLF").people if p.first == "RANDALL" and p.last == "WOLF"][0]
    voter = _voter(last_name="WOLF", first_name="RANDALL", middle_name="A",
                   phone_raw="7043311000", res_street="17511 OLD STATESVILLE RD",
                   res_city="HUNTERSVILLE", res_zip="28078",
                   mail_street="461 RALPH HOOD RD", mail_city="FORT MILL", mail_zip="29715")
    prop = parse_address("17511 OLD STATESVILLE RD", "HUNTERSVILLE", "NC", "28078")
    mail = parse_address("461 RALPH HOOD RD", "FORT MILL", "SC", "29715")
    d = match_person_to_voters(person, prop, mail, "Mecklenburg", [voter])
    assert d.accepted is True
    assert d.tier == "TIER_A"
    assert d.phone.e164 == "+17043311000"


def test_mailing_address_match_and_property_address_match():
    person = [p for p in parse_owner_name("RANDALL A WOLF").people if p.first == "RANDALL"][0]
    # Mailing only (property different / missing house)
    voter = _voter(last_name="WOLF", first_name="RANDALL", middle_name="A",
                   phone_raw="7043311001",
                   res_street="999 OTHER ST", res_zip="28205",
                   mail_street="461 RALPH HOOD RD", mail_zip="29715")
    prop = parse_address("17511 OLD STATESVILLE RD HUNTERSVILLE NC", "HUNTERSVILLE", "NC", "28078")
    mail = parse_address("461 RALPH HOOD RD", "", "SC", "29715")
    d = match_person_to_voters(person, prop, mail, "Mecklenburg", [voter])
    assert d.accepted is True
    assert d.tier == "TIER_A"
    assert "mailing" in d.reason

    voter2 = _voter(last_name="WOLF", first_name="RANDALL", middle_name="A",
                    phone_raw="7043311002",
                    res_street="17511 OLD STATESVILLE RD", res_zip="28078",
                    mail_street="PO BOX 1", mail_zip="28078")
    d2 = match_person_to_voters(person, prop, mail, "Mecklenburg", [voter2])
    assert d2.accepted is True
    assert "property" in d2.reason


def test_same_name_different_address_rejected():
    person = [p for p in parse_owner_name("RANDALL A WOLF").people if p.first == "RANDALL"][0]
    voter = _voter(last_name="WOLF", first_name="RANDALL", middle_name="A",
                   phone_raw="7043311000",
                   res_street="100 MAIN ST", res_zip="28202",
                   mail_street="100 MAIN ST", mail_zip="28202")
    prop = parse_address("17511 OLD STATESVILLE RD", "HUNTERSVILLE", "NC", "28078")
    mail = parse_address("461 RALPH HOOD RD", "FORT MILL", "SC", "29715")
    d = match_person_to_voters(person, prop, mail, "Mecklenburg", [voter])
    assert d.accepted is False


def test_common_name_ambiguity_rejected():
    person = [p for p in parse_owner_name("JAMES SMITH").people if p.first == "JAMES" and p.last == "SMITH"]
    if not person:
        person = [p for p in parse_owner_name("SMITH, JAMES").people if p.last == "SMITH"]
    person = person[0]
    v1 = _voter(last_name="SMITH", first_name="JAMES", voter_reg_num="1",
                phone_raw="7043311000", res_street="10 A ST", res_zip="28205",
                mail_street="10 A ST", mail_zip="28205")
    v2 = _voter(last_name="SMITH", first_name="JAMES", voter_reg_num="2",
                phone_raw="7043311001", res_street="20 B ST", res_zip="28205",
                mail_street="20 B ST", mail_zip="28205")
    prop = parse_address("10 A ST", "CHARLOTTE", "NC", "28205")
    mail = parse_address("10 A ST", "CHARLOTTE", "NC", "28205")
    # Two James Smiths both matching? v2 address differs so only v1 should match
    d = match_person_to_voters(person, prop, mail, "Mecklenburg", [v1, v2])
    assert d.accepted is True  # unique at the exact address
    # Both at same mailing zip with only name + zip (tier C held / ambiguous)
    v3 = _voter(last_name="SMITH", first_name="JAMES", voter_reg_num="3",
                phone_raw="7043311002", res_street="10 A ST", res_zip="28205",
                mail_street="10 A ST", mail_zip="28205")
    v3b = _voter(last_name="SMITH", first_name="JAMES", middle_name="Q", voter_reg_num="4",
                 phone_raw="7043311099", res_street="10 A ST", res_zip="28205",
                 mail_street="10 A ST", mail_zip="28205")
    d2 = match_person_to_voters(person, prop, mail, "Mecklenburg", [v3, v3b])
    assert d2.accepted is False
    assert d2.reject_class in {"AMBIGUOUS", "COMMON_NAME"}


def test_confidential_and_entity_never_match():
    person = [p for p in parse_owner_name("RANDALL A WOLF").people if p.first == "RANDALL"][0]
    voter = _voter(confidential="Y", phone_raw="7043311000",
                   res_street="17511 OLD STATESVILLE RD", res_zip="28078")
    prop = parse_address("17511 OLD STATESVILLE RD", "HUNTERSVILLE", "NC", "28078")
    d = match_person_to_voters(person, prop, prop, "Mecklenburg", [voter])
    assert d.accepted is False


def test_estate_import_preserves_relationship_and_blocks_attorney():
    rows = [
        {
            "property_candidate_id": "c1",
            "apn": "1",
            "county": "Wake",
            "fips": "37183",
            "decedent_name": "POWELL, MARJORIE G",
            "estate_case_number": "25E0001",
            "applicant_name": "GOODSON, BETTY",
            "co_applicant_name": "",
            "personal_representative_name": "",
            "heir_name": "",
            "relationship": "DAUGHTER",
            "mailing_address": "15 KNOLLWOOD DR",
            "mailing_city": "WEST ORANGE",
            "mailing_state": "NJ",
            "mailing_zip": "07052",
            "attorney_name": "SMITH, ESQ",
            "source_document": "AOC-E-201",
            "source_date": "2024-01-01",
        },
        {
            "property_candidate_id": "c2",
            "apn": "2",
            "county": "Wake",
            "fips": "37183",
            "decedent_name": "X",
            "estate_case_number": "25E0002",
            "applicant_name": "",
            "co_applicant_name": "",
            "personal_representative_name": "",
            "heir_name": "JONES, ANN",
            "relationship": "ATTORNEY",
            "mailing_address": "1 MAIN",
            "mailing_city": "RALEIGH",
            "mailing_state": "NC",
            "mailing_zip": "27601",
            "attorney_name": "JONES, ANN",
            "source_document": "docket",
            "source_date": "2024-01-01",
        },
    ]
    people = parse_estate_import_rows(rows)
    assert len(people) == 1
    assert people[0].relationship == "DAUGHTER"
    assert people[0].person.first == "BETTY"
    assert people[0].person.last == "GOODSON"
    assert people[0].estate_case == "25E0001"
    assert people[0].heir_person_id.startswith("HEIR_")
    assert "ATTORNEY" in FORBIDDEN_RELATIONSHIPS


def test_duplicate_and_household_and_multi_property_phone_detection():
    from collections import defaultdict
    appends = [
        {"candidate_id": "a", "phone": "+17043311000", "house": "1"},
        {"candidate_id": "a", "phone": "+17043311000", "house": "1"},  # duplicate row
        {"candidate_id": "b", "phone": "+17043311000", "house": "1"},  # household / same phone other person
        {"candidate_id": "c", "phone": "+17043311000", "house": "9"},  # same phone other property
    ]
    by_phone = defaultdict(set)
    for row in appends:
        by_phone[row["phone"]].add(row["candidate_id"])
    assert by_phone["+17043311000"] == {"a", "b", "c"}
    dup_rows = len(appends) - len({(r["candidate_id"], r["phone"]) for r in appends})
    assert dup_rows == 1


def test_mecklenburg_av_bv_street_normalization():
    a = parse_address("4042 REDWOOD AV CHARLOTTE NC", "CHARLOTTE", "NC", "28205")
    b = parse_address("4042 REDWOOD AVE", "CHARLOTTE", "NC", "28205")
    assert a.street_type == "AVE"
    assert exact_core_match(a, b)
    c = parse_address("2518 BEATTIES FORD RD")
    d = parse_address("2518 BEATTIES FORD ROAD", zipcode="28216")
    # house+street without zip on one side still exact if zip missing allowed
    assert a.house == "4042"
    e = parse_address("809 MATTHEWS TOWNSHIP PY MATTHEWS NC")
    assert e.street_type == "PKWY"
    f = parse_address("1101 EAST BV CHARLOTTE NC")
    assert f.street_type == "BLVD"
    assert house_street_zip_match(
        parse_address("8023 LAINE RD", zipcode="28214"),
        parse_address("8023 LAINE ROAD", zipcode="28214"),
    )


def test_sole_heir_is_living_not_decedent():
    eva = parse_owner_name("EVA SOLE HEIR CHAMBERS")
    living = eva.living_people
    assert any(p.last == "CHAMBERS" and p.first == "EVA" and p.role == "NAMED_HEIR_ON_ROLL" for p in living)
    assert all(not p.deceased for p in living)


def test_name_only_match_rejected():
    person = [p for p in parse_owner_name("RANDALL A WOLF").people if p.first == "RANDALL" and p.last == "WOLF"][0]
    voter = _voter(
        last_name="WOLF", first_name="RANDALL", middle_name="A",
        phone_raw="7043311000",
        res_street="999 OTHER ST", res_zip="28202",
        mail_street="999 OTHER ST", mail_zip="28202",
    )
    prop = parse_address("17511 OLD STATESVILLE RD", "HUNTERSVILLE", "NC", "28078")
    mail = parse_address("461 RALPH HOOD RD", "FORT MILL", "SC", "29715")
    d = match_person_to_voters(person, prop, mail, "Mecklenburg", [voter])
    assert d.accepted is False
    assert d.reject_class in {"NO_MATCH", "ADDRESS_CONFLICT"}


def test_tier_c_held_not_appended():
    person = [p for p in parse_owner_name("FARRIS, JOHN A JR").people if p.first == "JOHN" and p.last == "FARRIS"][0]
    # Name + city + zip + no house agreement → independent components may reach 3 via city/zip/street miss
    voter = _voter(
        last_name="FARRIS", first_name="JOHN", middle_name="A", suffix="JR",
        phone_raw="9193311000",
        res_street="50 OTHER CREEK RD", res_city="RALEIGH", res_zip="27610",
        mail_street="50 OTHER CREEK RD", mail_city="RALEIGH", mail_zip="27610",
        county_desc="WAKE",
    )
    prop = parse_address("100 MAIN ST", "RALEIGH", "NC", "27610")
    mail = parse_address("100 MAIN ST", "RALEIGH", "NC", "27610")
    d = match_person_to_voters(person, prop, mail, "Wake", [voter])
    assert d.accepted is False
    assert d.tier in {"", "TIER_C"} or d.reject_class in {
        "NO_MATCH", "TIER_C_HELD", "LOW_CONFIDENCE", "ADDRESS_CONFLICT",
    }


def test_libphonenumber_is_not_callable_claim():
    ok = normalize_us_phone("7043311000")
    assert ok.numbering_plan_valid is True
    assert ok.callable == "UNKNOWN"
    assert ok.currently_assigned == "UNKNOWN"
    assert ok.right_party == "UNKNOWN"


def test_entity_public_name_corroboration():
    from app.contact.entity_public import names_corroborate, org_key, extract_990_contact
    assert names_corroborate("PEOPLE UNITED HOUSE OF PRAYER FOR ALL", "PEOPLE UNITED HOUSE OF PRAYER FOR ALL PEOPLE")
    assert org_key("PDS REAL ESTATE LLC") == org_key("PDS REAL ESTATE")
    xml = b"""<?xml version='1.0'?><Return><ReturnHeader>
      <Filer><BusinessName><BusinessNameLine1Txt>TEST CHURCH</BusinessNameLine1Txt></BusinessName>
      <USAddress><AddressLine1Txt>100 MAIN ST</AddressLine1Txt><CityNm>CHARLOTTE</CityNm>
      <StateAbbreviationCd>NC</StateAbbreviationCd><ZIPCd>28202</ZIPCd></USAddress>
      <PhoneNum>7043311000</PhoneNum></Filer>
      <WebsiteAddressTxt>www.testchurch.org</WebsiteAddressTxt>
    </ReturnHeader></Return>"""
    contact = extract_990_contact(xml)
    assert contact["phone"] == "7043311000"
    assert contact["domain"] == "testchurch.org"
    assert "CHURCH" in contact["name"].upper() or contact["name"] == "TEST CHURCH"


def test_repeated_digits_and_confidence_and_route_class():
    from app.contact.confidence import score_contact
    from app.contact.owner_class import owner_route_class, phone_role_for
    from app.contact.phone_graph import analyze_phone_graph
    assert normalize_us_phone("0000000000").classification == "INVALID"
    assert normalize_us_phone("1111111111").classification == "INVALID"
    assert owner_route_class("RANDALL A WOLF") == "LIVING_PERSON"
    assert owner_route_class("POWELL, MARJORIE G HEIRS & GOODSON, BETTY") == "HEIRS"
    assert owner_route_class("PDS REAL ESTATE LLC") == "LLC"
    assert owner_route_class("FRANCHISE REALTY INTERSTATE CORPORATION") == "CORPORATION"
    assert owner_route_class("PEOPLE UNITED HOUSE OF PRAYER FOR ALL") == "NONPROFIT"
    assert owner_route_class("ALP REVOCABLE LIVING TRUST") == "TRUST"
    assert owner_route_class("CITY OF CHARLOTTE") == "GOVERNMENT"
    assert phone_role_for("NAMED_HEIR_ON_ROLL", "HEIRS", "NCSBE_PUBLIC_VOTER_FILE") == "HEIR_PERSONAL"
    high = score_contact(
        tier="TIER_A", strong_name=True, common_name=False,
        source="NCSBE_PUBLIC_VOTER_FILE", observed_at="2026-08-16T00:00:00Z",
        source_count=1, fanout_candidates=1, fanout_distinct_owners=1,
        numbering_plan_valid=True, deceased_target=False,
    )
    assert high.category == "HIGH"
    held = score_contact(
        tier="TIER_C", strong_name=True, common_name=False,
        source="NCSBE_PUBLIC_VOTER_FILE", observed_at="2026-08-16T00:00:00Z",
        source_count=1, fanout_candidates=1, fanout_distinct_owners=1,
        numbering_plan_valid=True, deceased_target=False,
    )
    assert held.category == "REVIEW"
    g = analyze_phone_graph([
        {"candidate_id": "a", "phone": "+17043311000", "owner_name": "A"},
        {"candidate_id": "b", "phone": "+17043311000", "owner_name": "B"},
    ])
    assert g["MULTI_PROPERTY_PHONES"] == 1
    assert evaluate_phone_append(
        PhoneAppendIdentity("c", "NC:37119:1", "1", "37119", "Mecklenburg", "X"),
        {"candidate_id": "c", "apn": "1", "phone": "7043311000", "county": "Mecklenburg", "fips": "37119"},
        low_confidence=True,
    )["reason"] == "LOW_CONFIDENCE"
    assert evaluate_phone_append(
        PhoneAppendIdentity("c", "NC:37119:1", "1", "37119", "Mecklenburg", "X"),
        {"candidate_id": "c", "apn": "1", "phone": "7043311000", "county": "Mecklenburg", "fips": "37119"},
        deceased_target=True,
    )["reason"] == "DECEASED_TARGET"
    row = {
        "county_id": "60",
        "county_desc": "MECKLENBURG",
        "voter_reg_num": "999",
        "ncid": "SHOULD_NOT_LEAK",
        "last_name": "WOLF",
        "first_name": "RANDALL",
        "middle_name": "A",
        "name_suffix_lbl": "",
        "voter_status_desc": "ACTIVE",
        "res_street_address": "17511 OLD STATESVILLE RD",
        "res_city_desc": "HUNTERSVILLE",
        "state_cd": "NC",
        "zip_code": "28078",
        "mail_addr1": "461 RALPH HOOD RD",
        "mail_city": "FORT MILL",
        "mail_state": "SC",
        "mail_zipcode": "29715",
        "full_phone_number": "7043311000",
        "confidential_ind": "N",
    }
    v = voter_from_ncsbe_row(row, "ncvoter60.txt", "hash", "2026-08-16")
    assert "SHOULD_NOT_LEAK" not in v.identity_hash
    assert v.last_name == "WOLF"
    assert len(v.identity_hash) == 20

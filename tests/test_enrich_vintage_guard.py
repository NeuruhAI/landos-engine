"""
test_enrich_vintage_guard.py — ENRICH-1 P3: the vintage-capture guard + APN hygiene.

Doctrine as code: a government/vested roll name NEVER reaches owner_name — the
pattern is the directive's, verbatim. Plus the dirty-APN cleaner that keys the
statewide cohort's decorated parcel strings to something queryable (or honestly
refuses: None, never guessed).
"""
from scripts.enrich_owner_chain import VINTAGE_RE, apn_variants, canon_apn, clean_apn


# --- the guard: government patterns flag, humans/entities pass ---------------

def test_government_names_match():
    for vested in ("ANTRIM COUNTY TREASURER", "Tuscola County Treasurer",
                   "COUNTY OF MUSKEGON", "CITY OF KALAMAZOO", "STATE OF MICHIGAN",
                   "GENESEE COUNTY LAND BANK AUTHORITY", "FORFEITED - DO NOT BILL",
                   # census-observed government spellings (2026-07-05 fan-out):
                   "MICHIGAN DEPARTMENT OF TREASURY",     # Mecosta 3/3
                   "IONIA COUNTY TREASRUER",              # the county's own typo
                   "DEPARTMENT OF NATURAL RESOURCE",      # Kalkaska 3/3 (MI DNR)
                   "MI DEPT OF NATURAL RESOURCES",        # Charlevoix
                   "GRATIOT COUNTY",                      # trailing COUNTY
                   "STATE"):                              # Lake Co bare shorthand
        assert VINTAGE_RE.search(vested.strip()), vested


def test_human_and_entity_names_pass():
    for owner in ("SMITH JOHN A", "HUDSON CARL J & PAULINE O", "LAND OF LAND INC",
                  "TREASURE ISLAND HOLDINGS LLC",   # TREASURE != TREASURER/TREASURY
                  "LANDMARK BANK NA",               # LANDMARK BANK != LAND BANK
                  "STATEWIDE HOLDINGS LLC",         # STATE only matches bare/exact
                  "DE FELICE LUKE A ESTATE",        # estates are LEADS, never vintage
                  "REEB, DORIS REVOC LIV TR",
                  "COUNTY LINE FARMS LLC",          # COUNTY mid-name, not trailing
                  "GREENE RONALD"):
        assert not VINTAGE_RE.search(owner.strip()), owner


# --- APN hygiene: decorated strings key to the billable parcel ---------------

def test_clean_apn_plain_forms_pass_verbatim():
    for apn in ("082-003-000-001-00", "11 180 122 000", "L21-35-700-040-00",
                "008-0-F50-000-079-00"):
        assert clean_apn(apn) == apn


def test_clean_apn_part_of_queries_parent_parcel():
    assert clean_apn("Part of 12-410-004-00 (B)") == "12-410-004-00"
    assert clean_apn("Part of 062-017-100-001-00 (C)") == "062-017-100-001-00"


def test_clean_apn_multi_apn_takes_first():
    assert clean_apn("005-218-006-00 005-218-002-00") == "005-218-006-00"


def test_clean_apn_placeholder_is_none_never_guessed():
    assert clean_apn("Parcel ID is TBD Chippewa") is None
    assert clean_apn("") is None
    assert clean_apn(None) is None


def test_apn_variants_ordered_as_is_first():
    v = apn_variants("11 180 122 000")
    assert v[0] == "11 180 122 000"
    assert "11-180-122-000" in v and "11180122000" in v


def test_canon_apn_separator_immune():
    assert canon_apn("05-10-235-337-00") == canon_apn("05 10 235 337 00") == "051023533700"

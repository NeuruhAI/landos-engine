"""The import court's address key must equal DeedSonar's normalizeAddress byte for byte.

Golden vectors were produced by running the real function
(deedsonar src/lib/imports/county-normalize.ts normalizeAddress @ ae6bb614, node)
over this corpus. The previous str.replace mirror disagreed on 7 of 20
(BROADWAY -> "brdway", PLANET -> "plnt", full-width "ＰＯ" dropped, ...), so the
court's duplicate-address check keyed rows differently from the importer.
"""
from scripts.validate_deedsonar_import import norm_addr_key

GOLDEN = [
    ("123 Broadway", "123 broadway"),
    ("12 Planet Rd.", "12 planet rd"),
    ("400 N. Main Street #4", "400 n main st #4"),
    ("12 Main St 4", "12 main st 4"),
    ("7 Lanes End Lane", "7 lanes end ln"),
    ("55 Highway 64 E", "55 hwy 64 e"),
    ("9 Drive-In Dr.", "9 dr in dr"),
    ("1 St. James Ave.", "1 st james ave"),
    ("88 Roadrunner Road", "88 roadrunner rd"),
    ("3 Avenue Of The Oaks", "3 ave of the oaks"),
    ("ＰＯ Box 12", "po box 12"),
    ("  12   Oak   Street  ", "12 oak st"),
    ("12 OAK ST. APT 2", "12 oak st apt 2"),
    ("6 Streeter Ln.", "6 streeter ln"),
    ("14 Hwy. 1 N", "14 hwy 1 n"),
    ("", ""),
    ("2 Dr. King Blvd", "2 dr king blvd"),
    ("5 Céline Street", "5 c line st"),
    ("10 Stanley Dr", "10 stanley dr"),
    ("701 Laneview Ave", "701 laneview ave"),
]


def test_address_key_matches_the_importer_normalizer():
    assert [norm_addr_key(raw) for raw, _ in GOLDEN] == [want for _, want in GOLDEN]


def test_suffix_folds_are_whole_words_only():
    assert norm_addr_key("123 Broadway") == "123 broadway"
    assert norm_addr_key("88 Roadrunner Road") == "88 roadrunner rd"

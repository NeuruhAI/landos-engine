"""
test_tangled_title.py — parcels taxed to dead people (curative-title feedstock).

Covers the PURE classifier (every tangled pattern + the two census-proven false
positives it must REJECT: "REAL ESTATE" companies and "CO LLC" mistaken for c/o,
plus living trusts), the confidence tiers (high/med, tax-delinquent stack-boost,
no-probate lift), the per-county census (incl. the honest probate data-gate), and
the adapter's armed emission + signal shape. One DB-backed case proves the LIVE
census returns the real tangled universe and that the probate discriminator is
currently inert — it skips when the spine is unreachable and never writes.
"""
import pytest

from app.discovery import tangled_title as tt
from app.discovery.tangled_title import TangledTitleAdapter, classify_owner_name

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"


def _rec(county, apn, owner_name, tax_delinquent=False, has_probate_filing=False):
    return {"county_fips": county, "parcel_apn": apn, "owner_name": owner_name,
            "tax_delinquent": tax_delinquent, "has_probate_filing": has_probate_filing}


# ---- pure classifier: genuine tangled-title patterns -----------------------

def test_estate_of_classifies_high():
    c = classify_owner_name("DE FELICE LUKE A ESTATE")
    assert c == {"matched_pattern": "estate_of", "tier": "high"}


def test_life_estate_wins_over_estate_of():
    # "(LIFE ESTATE)" contains "estate" but the more specific remainderman signal
    # must win — ordering guarantees it.
    assert classify_owner_name("KLINK NOREEN M (LIFE ESTATE)")["matched_pattern"] == "life_estate"
    assert classify_owner_name("WEBB ANNETTE (L/E)")["matched_pattern"] == "life_estate"


def test_heirs_and_deceased_classify_high():
    assert classify_owner_name("HEIRS OF WILLIAM PARKS")["matched_pattern"] == "heirs_of"
    assert classify_owner_name("JONES MARY DECEASED")["matched_pattern"] == "deceased"
    assert classify_owner_name("SMITH J DEC'D")["matched_pattern"] == "deceased"


def test_et_al_and_et_ux_classify_med():
    assert classify_owner_name("GRUBAUGH H LEROY ET AL") == {"matched_pattern": "et_al", "tier": "med"}
    assert classify_owner_name("MOTT ELMER ET UX")["matched_pattern"] == "et_al"


def test_c_o_requires_the_slash():
    assert classify_owner_name("SMITH JOHN C/O JANE DOE")["matched_pattern"] == "c_o"


# ---- pure classifier: the census-proven FALSE POSITIVES it must reject ------

def test_real_estate_company_is_not_an_estate():
    # the naive `~ 'estate'` inflated the count 3x on these — anti-pattern kills it.
    assert classify_owner_name("PDS REAL ESTATE LLC") is None
    assert classify_owner_name("BOERMA FAMILY REAL ESTATE") is None
    assert classify_owner_name("SKYLINE REAL ESTATE SERVICE LLC") is None


def test_company_co_is_not_caretaker_co():
    # "OIL CO", "INVESTMENT CO LLC" must NOT read as the caretaker "c/o".
    assert classify_owner_name("RUUSIVIVIAN OIL CO") is None
    assert classify_owner_name("CONSUMERS INVESTMENT CO LLC") is None


def test_living_trust_excluded():
    # a revocable/irrevocable trust is estate PLANNING that avoids probate — the
    # opposite of a tangled title. It matches no pattern.
    assert classify_owner_name("ALP REVOCABLE LIVING TRUST") is None
    assert classify_owner_name("MILLER FAMILY IRREVOCABLE TRUST") is None


def test_ordinary_name_and_empty_are_none():
    assert classify_owner_name("SMITH JOHN A") is None
    assert classify_owner_name("") is None
    assert classify_owner_name(None) is None


# ---- confidence tiers ------------------------------------------------------

def test_high_tier_base_and_boosts():
    # high base 0.80; +0.10 tax-delinquent; +0.05 no-probate; capped 0.95
    assert tt._confidence("high", False, True) == 0.80          # probate open, clean taxes
    assert tt._confidence("high", True, True) == 0.90           # +delinquent
    assert tt._confidence("high", True, False) == 0.95          # +delinquent +no-probate (capped)
    assert tt._confidence("high", False, False) == 0.85         # +no-probate only


def test_med_tier_is_lower():
    assert tt._confidence("med", False, True) == 0.55
    assert tt._confidence("med", True, False) == 0.70


# ---- census ----------------------------------------------------------------

def test_census_counts_hits_tiers_and_subsets():
    recs = [
        _rec("26049", "P1", "DE FELICE LUKE A ESTATE", tax_delinquent=True, has_probate_filing=False),
        _rec("26049", "P2", "GRUBAUGH H LEROY ET AL", tax_delinquent=True, has_probate_filing=False),
        _rec("26049", "P3", "SMITH JOHN A"),               # ordinary — not a hit
        _rec("26049", "P4", "PDS REAL ESTATE LLC"),        # false positive — not a hit
    ]
    c = tt.census(recs)["26049"]
    assert c["scanned"] == 4 and c["hits"] == 2
    assert c["high"] == 1 and c["med"] == 1
    assert c["tax_delinquent"] == 2 and c["no_probate"] == 2 and c["hi_conviction"] == 2


def test_census_flags_probate_gate_when_none_matched():
    recs = [_rec("26049", "P1", "CHAPMAN PHYLLIS ESTATE", has_probate_filing=False)]
    c = tt.census(recs)["26049"]
    assert c["probate_matched"] == 0 and "INERT" in c["probate_gate"]


def test_census_no_gate_note_when_a_probate_matches():
    recs = [_rec("26049", "P1", "CHAPMAN PHYLLIS ESTATE", has_probate_filing=True)]
    c = tt.census(recs)["26049"]
    assert c["probate_matched"] == 1 and "probate_gate" not in c


# ---- adapter: armed emission + signal shape --------------------------------

def test_adapter_emits_one_signal_per_tangled_parcel():
    recs = [
        _rec("26049", "HIT", "MC CLAIN LYMAN E ESTATE", tax_delinquent=True, has_probate_filing=False),
        _rec("26049", "SKIP", "SMITH JOHN A"),             # ordinary
        _rec("26049", "TRUST", "ALP REVOCABLE LIVING TRUST"),  # excluded
    ]
    sigs = TangledTitleAdapter(TENANT, owner_records=recs).fetch()
    assert len(sigs) == 1
    s = sigs[0]
    assert s["parcel_apn"] == "HIT"
    assert s["source_ref"] == "26049:HIT:tangled_title"
    assert s["owner_name"] == "MC CLAIN LYMAN E ESTATE"
    assert s["confidence"] == 0.95                          # high + delinquent + no-probate
    assert s["raw_payload"] == {
        "type": "heirs_of_tangled_title", "parcel_apn": "HIT",
        "matched_pattern": "estate_of", "has_probate_filing": False,
        "tier": "high", "tax_delinquent": True,
    }


def test_adapter_tax_delinquent_stack_boost_is_visible():
    clean = _rec("26049", "A", "JONES ESTATE", tax_delinquent=False, has_probate_filing=False)
    delinq = _rec("26049", "B", "JONES ESTATE", tax_delinquent=True, has_probate_filing=False)
    a = TangledTitleAdapter(TENANT, owner_records=[clean, delinq])
    by_apn = {s["parcel_apn"]: s for s in a.fetch()}
    assert by_apn["B"]["confidence"] > by_apn["A"]["confidence"]   # delinquency lifts it


def test_adapter_pins_single_county():
    recs = [_rec("26049", "A", "SMITH ESTATE"), _rec("26081", "B", "JONES ESTATE")]
    a = TangledTitleAdapter(TENANT, owner_records=recs, county_fips="26049")
    sigs = a.fetch()
    assert len(sigs) == 1 and sigs[0]["parcel_apn"] == "A"
    assert set(a.last_census) == {"26049"}


# ---- DB-backed: the LIVE census returns the real universe + probate gate ----

def test_live_census_returns_universe_and_probate_is_gated():
    psycopg = pytest.importorskip("psycopg")
    from app.config import DATABASE_URL
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"database unreachable: {e}")
    try:
        with conn.cursor() as cur:
            rep = tt.census_live(cur, TENANT)
        total_hits = sum(c["hits"] for c in rep.values())
        # owner names are present on the spine today -> the adapter ARMS (unlike the
        # exemption sleeper). There ARE tangled parcels.
        assert total_hits >= 1, "expected tangled-title hits on the live spine"
        # probate ingest is ~1 APN today -> no tangled parcel matches a probate
        # filing -> the no-probate subset equals every hit (discriminator inert).
        assert sum(c["probate_matched"] for c in rep.values()) == 0
        assert sum(c["no_probate"] for c in rep.values()) == total_hits
    finally:
        conn.rollback()
        conn.close()

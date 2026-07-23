"""
test_obituary_signal.py — P-23: obituary adapter + estate-candidate correlation.

Local fixtures only (tests/fixtures/obituary_18141.csv + inline rows), no DB, no
network. The adversarial cases are the point: common-name ambiguity, entity
"decedents", a CSV that tries to smuggle a parcel_apn, PII leakage into receipts,
and — above all — that NO path through this lane produces anything but a REVIEW
candidate. Fuzzy identity never mints.
"""
import os
import random

import pytest

from app.discovery.obituary import OBIT_CONFIDENCE, ObituaryAdapter
from app.discovery import obituary_correlation as oc
from app.discovery.obituary_correlation import (
    CORRELATED_CONFIDENCE_CAP, FUZZY_CONFIDENCE_CAP, build_candidate_receipt,
    correlate_probate, estate_candidates, receipt_canonical)

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "obituary_18141.csv")


def _owner(lead_id, name, apn, fips="18141", situs_city=None, mail_city=None):
    return {"lead_id": lead_id, "owner_name": name, "parcel_apn": apn,
            "county_fips": fips, "situs_city": situs_city,
            "owner_mailing_city": mail_city}


def _fetch():
    return ObituaryAdapter(TENANT, csv_path=FIXTURE).fetch()


# ---- adapter contract ------------------------------------------------------

def test_adapter_parses_fixture_and_skips_unusable_rows():
    signals = _fetch()
    refs = [s["source_ref"] for s in signals]
    assert refs == ["OB-2026-0001", "OB-2026-0002", "OB-2026-0003", "OB-2026-0005"]
    # OB-2026-0004 (no decedent name) is skipped, never guessed


def test_adapter_confidence_is_the_name_only_ceiling():
    assert all(s["confidence"] == OBIT_CONFIDENCE for s in _fetch())
    assert OBIT_CONFIDENCE < 0.5   # below every mint-grade threshold in the codebase


def test_adapter_never_emits_a_parcel_apn():
    assert all(s["parcel_apn"] is None for s in _fetch())


def test_adapter_refuses_a_smuggled_parcel_apn_column(tmp_path):
    p = tmp_path / "obituary_sneaky.csv"
    p.write_text(
        "source_ref,decedent_name,parcel_apn,city,death_date\n"
        "OB-X-1,Jane Q Public,71-01-01-100-001.000-001,South Bend,2026-07-01\n")
    signals = ObituaryAdapter(TENANT, csv_path=str(p)).fetch()
    assert len(signals) == 1
    assert signals[0]["parcel_apn"] is None
    assert "parcel_apn" not in signals[0]["raw_payload"]


def test_adapter_signal_date_prefers_death_date():
    s = {x["source_ref"]: x for x in _fetch()}
    assert s["OB-2026-0001"]["signal_date"] == "2026-06-25"
    assert s["OB-2026-0002"]["raw_payload"]["surviving_family_list"] == ["Robert Johnson"]
    assert all(x["raw_payload"]["name_only"] is True for x in _fetch())


def test_adapter_missing_csv_fails_loud():
    with pytest.raises(FileNotFoundError):
        ObituaryAdapter(TENANT, csv_path="data/incoming/nope_00000.csv").fetch()


# ---- fuzzy identity -> estate candidates (REVIEW ONLY) ---------------------

OWNERS = [
    _owner("L-1", "VANDERMEER HAROLD J", "71-08-15-201-002.000-026", situs_city="South Bend"),
    _owner("L-2", "JOHNSON MARY", "71-02-03-102-011.000-025", situs_city="Mishawaka"),
    _owner("L-3", "JOHNSON MARY", "71-09-11-427-006.000-021", situs_city="South Bend"),
    _owner("L-4", "SMITH JAMES", "71-05-05-105-005.000-005"),
    _owner("L-5", "RIVERBEND HOLDINGS LLC", "71-06-06-106-006.000-006"),
    _owner("L-6", "KOWALCZYK THEODORE STANLEY", "71-07-07-107-007.000-007",
           mail_city="Granger"),
]


def test_exact_token_match_with_city_corroboration():
    cands = estate_candidates(_fetch(), OWNERS)
    hits = [c for c in cands if c["obit_source_ref"] == "OB-2026-0001"]
    assert len(hits) == 1
    c = hits[0]
    assert c["match_kind"] == "exact_tokens" and c["lead_id"] == "L-1"
    assert c["match_keys"] == ["name", "city"]
    assert c["confidence"] == pytest.approx(0.65)   # 0.55 + 0.10 city
    assert c["ambiguous"] is False


def test_common_name_ambiguity_is_penalized_and_marked():
    cands = [c for c in estate_candidates(_fetch(), OWNERS)
             if c["obit_source_ref"] == "OB-2026-0002"]
    assert len(cands) == 2 and all(c["ambiguous"] for c in cands)
    assert all(c["candidate_pool"] == 2 for c in cands)
    by_lead = {c["lead_id"]: c for c in cands}
    # obit city Mishawaka corroborates L-2 only; both carry the ambiguity penalty
    assert by_lead["L-2"]["confidence"] == pytest.approx(0.55)  # 0.55 + 0.10 - 0.10
    assert by_lead["L-3"]["confidence"] == pytest.approx(0.45)  # 0.55 - 0.10


def test_surname_given_tier():
    cands = [c for c in estate_candidates(_fetch(), OWNERS)
             if c["obit_source_ref"] == "OB-2026-0003"]
    assert len(cands) == 1
    assert cands[0]["match_kind"] == "surname_given" and cands[0]["lead_id"] == "L-6"


def test_surname_initial_tier():
    cands = estate_candidates(
        [{"source_ref": "OB-I", "owner_name": "Mary Johnson", "county_fips": "18141",
          "raw_payload": {"city": ""}}],
        [_owner("L-7", "JOHNSON MARGARET", "71-01-01-101-001.000-001")])
    assert len(cands) == 1 and cands[0]["match_kind"] == "surname_initial"
    assert cands[0]["confidence"] == pytest.approx(0.30)


def test_an_entity_cannot_die():
    # the LLC "decedent" (OB-2026-0005) must match nothing — entity owners are excluded
    assert not [c for c in estate_candidates(_fetch(), OWNERS)
                if c["obit_source_ref"] == "OB-2026-0005"]


def test_every_candidate_is_review_only_and_fuzzy_capped():
    cands = estate_candidates(_fetch(), OWNERS)
    assert cands, "fixture must produce candidates"
    assert all(c["disposition"] == "review" for c in cands)
    assert all(c["confidence"] <= FUZZY_CONFIDENCE_CAP for c in cands)


def test_no_mintable_disposition_exists_in_the_module():
    # structural mint-safety: the module defines exactly one disposition constant
    assert oc.DISPOSITION_REVIEW == "review"
    src = open(oc.__file__).read()
    assert "DISPOSITION_MINT" not in src and "'mint'" not in src


def test_candidates_deterministic_across_input_order():
    base = estate_candidates(_fetch(), OWNERS)
    for seed in (3, 11):
        obits, owners = _fetch(), OWNERS[:]
        random.Random(seed).shuffle(obits)
        random.Random(seed).shuffle(owners)
        assert estate_candidates(obits, owners) == base


# ---- probate correlation ---------------------------------------------------

def _probate(case, name, apn=None, legal=None):
    return {"source_ref": case, "owner_name": name, "signal_date": "2026-07-18",
            "parcel_apn": apn, "raw_payload": {"legal_description": legal or ""}}


def _harold():
    return [c for c in estate_candidates(_fetch(), OWNERS)
            if c["obit_source_ref"] == "OB-2026-0001"]


def test_probate_two_key_with_apn_agreement_lifts_to_the_cap():
    out = correlate_probate(_harold(), [
        _probate("71C01-2607-EU-000123", "Harold J Vandermeer",
                 apn="71-08-15-201-002.000-026")])
    c = out[0]
    assert c["correlation"]["two_key"] and c["correlation"]["apn_agrees"]
    assert c["confidence"] == pytest.approx(CORRELATED_CONFIDENCE_CAP)
    assert c["disposition"] == "review"   # correlation NEVER changes disposition


def test_probate_legal_description_without_apn_is_two_key_smaller_lift():
    out = correlate_probate(_harold(), [
        _probate("71C01-2607-EU-000124", "Harold J Vandermeer",
                 legal="LOT 12 RIVER PARK 2ND ADD")])
    c = out[0]
    assert c["correlation"]["two_key"] and not c["correlation"]["apn_agrees"]
    assert c["confidence"] == pytest.approx(0.80)   # 0.65 + 0.15


def test_name_only_probate_match_stays_inside_the_fuzzy_cap():
    out = correlate_probate(_harold(), [
        _probate("71C01-2607-EU-000125", "Harold Vandermeer")])
    c = out[0]
    assert c["correlation"]["two_key"] is False
    assert c["confidence"] <= FUZZY_CONFIDENCE_CAP


def test_unrelated_probate_leaves_candidate_untouched():
    base = _harold()
    out = correlate_probate(base, [_probate("71C01-2607-EU-000126", "Alice Nobody")])
    assert out[0]["correlation"] is None
    assert out[0]["confidence"] == base[0]["confidence"]
    assert base[0]["correlation"] is None   # input never mutated


# ---- provenance + confidence receipt ---------------------------------------

def test_receipt_fingerprints_pii_and_pins_mint_ineligibility():
    out = correlate_probate(_harold(), [
        _probate("71C01-2607-EU-000123", "Harold J Vandermeer",
                 apn="71-08-15-201-002.000-026")])
    r = build_candidate_receipt(out[0])
    blob = receipt_canonical(r)
    assert "Vandermeer" not in blob and "Harold" not in blob   # decedent = sha256 only
    assert "Ruth" not in blob and "Simmons" not in blob        # family never in receipts
    assert r["sections"]["SOURCE"]["data"]["decedent_sha256"]
    assert r["sections"]["DISPOSITION"]["data"]["mint_eligible"] is False
    assert r["sections"]["DISPOSITION"]["data"]["disposition"] == "review"
    assert r["sections"]["CORRELATION"]["data"]["probate_case"] == "71C01-2607-EU-000123"


def test_receipt_chain_is_stable_and_tamper_evident():
    c = _harold()[0]
    r1, r2 = build_candidate_receipt(c), build_candidate_receipt(dict(c))
    assert r1["chain_sha256"] == r2["chain_sha256"]
    tampered = dict(c, confidence=0.99)
    assert build_candidate_receipt(tampered)["chain_sha256"] != r1["chain_sha256"]


# ---- gate-in-data: the registry row ----------------------------------------

def test_registry_migration_pins_obituary_as_non_minting():
    mig = open(os.path.join(os.path.dirname(__file__), "..",
                            "migrations", "041_obituary_signal_registry.sql")).read()
    assert "'obituary', 'enrichment'" in mig and "false" in mig

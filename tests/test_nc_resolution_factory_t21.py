"""T21 Resolution Factory — contract, router, quality court, tier_c, projection.

Synthetic fixtures only. No real PII, no network, no database, no provider.
"""
from __future__ import annotations

import pytest

from app.contact.identity_ledger import IDENTITY_LEDGER_COLUMNS, identity_rows
from app.resolution.contract import (
    ARBITER_FAMILIES,
    CALLABILITY_NOT_AUTHORIZED,
    ContactPoint,
    ContractViolation,
    Evidence,
    Party,
    Relationship,
    ResolutionRecord,
)
from app.resolution import quality
from app.resolution.factory import (
    build_records,
    build_relationships,
    relationship_role_for,
    to_qualified_board,
)
from app.resolution.router import (
    OPERATOR_QUEUES,
    QUEUE_CONTACT_STATUS,
    next_best_source,
)
from app.resolution.sources import all_sources, entity_sources, estate_sources
from app.resolution import tier_c
from app.resolution.yield_engine import build_yield, recipe_contact_intelligence


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def ev(field="phone", family="NCSBE_PUBLIC_VOTER_FILE", confidence="HIGH"):
    return Evidence(
        source_family=family, source_name=family, field=field,
        raw_value="RAW", normalized_value="NORM", match_method="TIER_A",
        confidence=confidence, observed_at="2026-08-16T00:00:00Z",
        retrieved_at="2026-08-16T00:00:00Z", source_record_id="rec1",
        source_hash="deadbeef",
    )


def phone(role="OWNER_PERSONAL", value="+17045550123", confidence="HIGH"):
    return ContactPoint(kind="PHONE", value=value, role=role, confidence=confidence,
                        observed_at="2026-08-16T00:00:00Z", evidence=[ev()])


def party(name="JANE DOE", points=None, kind="PERSON"):
    return Party(kind=kind, canonical_name=name, evidence=[ev(field="identity")],
                 contact_points=points or [])


# ---------------------------------------------------------------------------
# 1. no naked facts
# ---------------------------------------------------------------------------

def test_contact_point_requires_evidence():
    with pytest.raises(ContractViolation, match="no naked contact points"):
        ContactPoint(kind="PHONE", value="+17045550123", role="OWNER_PERSONAL",
                     confidence="HIGH", observed_at="", evidence=[])


def test_party_requires_evidence():
    with pytest.raises(ContractViolation, match="no naked identity assertions"):
        Party(kind="PERSON", canonical_name="JANE DOE", evidence=[])


def test_relationship_requires_evidence():
    with pytest.raises(ContractViolation, match="has no evidence"):
        Relationship(party=party(), role="RECORDED_OWNER", evidence=[])


# ---------------------------------------------------------------------------
# 2. roles are never collapsed
# ---------------------------------------------------------------------------

def test_trustee_may_not_carry_an_owner_phone():
    p = party(points=[phone(role="OWNER_PERSONAL")])
    with pytest.raises(ContractViolation, match="not permitted for relationship TRUSTEE"):
        Relationship(party=p, role="TRUSTEE", evidence=[ev()])


def test_trustee_personal_is_accepted_for_a_trustee():
    p = party(points=[phone(role="TRUSTEE_PERSONAL")])
    rel = Relationship(party=p, role="TRUSTEE", evidence=[ev()])
    assert rel.is_seller_side


def test_registered_agent_is_never_the_seller():
    p = party(points=[phone(role="REGISTERED_AGENT_OFFICE")], kind="ORG")
    rel = Relationship(party=p, role="REGISTERED_AGENT", evidence=[ev()])
    assert rel.is_contactable_party is True
    assert rel.is_seller_side is False


def test_decedent_is_not_a_contactable_party():
    rel = Relationship(party=party(name="JOHN SMITH"), role="DECEDENT", evidence=[ev()])
    assert rel.is_contactable_party is False


def test_multi_person_owner_row_becomes_a_coowner_not_the_recorded_owner():
    """The defect the contract caught on the real bundle: person_role is the
    coarse 'OWNER' while owner_class/phone_role carry the finer truth."""
    row = {"person_role": "OWNER", "owner_class": "MULTI_PERSON",
           "phone_role": "COOWNER_PERSONAL"}
    assert relationship_role_for(row) == "COOWNER"
    assert relationship_role_for({"person_role": "OWNER", "owner_class": "LIVING_PERSON",
                                  "phone_role": "OWNER_PERSONAL"}) == "RECORDED_OWNER"
    assert relationship_role_for({"person_role": "NAMED_TRUSTEE_ON_ROLL"}) == "TRUSTEE"
    assert relationship_role_for({"person_role": "NAMED_HEIR_ON_ROLL"}) == "HEIR"


def test_build_relationships_keeps_two_coowners_apart():
    rows = [
        {"candidate_id": "c1", "matched_person": "A ONE", "person_role": "OWNER",
         "owner_class": "MULTI_PERSON", "phone_role": "COOWNER_PERSONAL",
         "phone": "+17045550001", "confidence_category": "HIGH",
         "phone_source": "NCSBE_PUBLIC_VOTER_FILE", "why_matched": "TIER_A",
         "observed_at": "2026-08-16T00:00:00Z"},
        {"candidate_id": "c1", "matched_person": "B TWO", "person_role": "OWNER",
         "owner_class": "MULTI_PERSON", "phone_role": "COOWNER_PERSONAL",
         "phone": "+17045550002", "confidence_category": "HIGH",
         "phone_source": "NCSBE_PUBLIC_VOTER_FILE", "why_matched": "TIER_A",
         "observed_at": "2026-08-16T00:00:00Z"},
    ]
    rels = build_relationships(rows)
    assert len(rels) == 2
    assert {r.role for r in rels} == {"COOWNER"}
    assert {r.party.contact_points[0].value for r in rels} == {"+17045550001", "+17045550002"}


# ---------------------------------------------------------------------------
# 3. callability and arbiters
# ---------------------------------------------------------------------------

def test_callability_cannot_be_authorized():
    with pytest.raises(ContractViolation, match="callability is not authorized"):
        ContactPoint(kind="PHONE", value="+17045550123", role="OWNER_PERSONAL",
                     confidence="HIGH", observed_at="", evidence=[ev()],
                     callability_status="AUTHORIZED")


def test_record_callability_is_always_not_authorized():
    rec = ResolutionRecord(candidate_id="c1", estate_revision="rev")
    assert rec.callability_status == CALLABILITY_NOT_AUTHORIZED


@pytest.mark.parametrize("family", sorted(ARBITER_FAMILIES))
def test_an_arbiter_may_never_source_a_contact_point(family):
    with pytest.raises(ContractViolation, match="arbiter"):
        ContactPoint(kind="PHONE", value="+17045550123", role="OWNER_PERSONAL",
                     confidence="HIGH", observed_at="",
                     evidence=[ev(family=family)])


# ---------------------------------------------------------------------------
# 4. opportunity and contactability stay separate
# ---------------------------------------------------------------------------

def test_finding_a_phone_does_not_change_the_opportunity_score():
    base = ResolutionRecord(candidate_id="c1", estate_revision="r", score=66.0,
                            global_rank=31, qualification_state="QUALIFIED_CANDIDATE")
    with_phone = ResolutionRecord(
        candidate_id="c1", estate_revision="r", score=66.0, global_rank=31,
        qualification_state="QUALIFIED_CANDIDATE",
        relationships=[Relationship(party=party(points=[phone()]),
                                    role="RECORDED_OWNER", evidence=[ev()])])
    assert with_phone.score == base.score
    assert with_phone.global_rank == base.global_rank
    assert with_phone.qualification_state == base.qualification_state
    # only the SEPARATE contact dimension moves
    assert with_phone.contact_resolution_score > base.contact_resolution_score


def test_contact_resolution_score_ranks_states_sensibly():
    none_ = ResolutionRecord(candidate_id="a", estate_revision="r",
                             owner_condition="DECEASED")
    named = ResolutionRecord(candidate_id="b", estate_revision="r",
                             relationships=[Relationship(party=party(), role="HEIR",
                                                         evidence=[ev()])])
    called = ResolutionRecord(candidate_id="c", estate_revision="r",
                              relationships=[Relationship(party=party(points=[phone()]),
                                                          role="RECORDED_OWNER",
                                                          evidence=[ev()])])
    assert none_.contact_resolution_score < named.contact_resolution_score < called.contact_resolution_score


# ---------------------------------------------------------------------------
# 5. next-best-source router
# ---------------------------------------------------------------------------

def test_deceased_owner_routes_to_the_estate_file_not_a_phone_source():
    ns = next_best_source(owner_class="DECEASED", owner_condition="DECEASED",
                          contact_resolution_state="DECEASED_OWNER_NEEDS_HEIR_IDENTITY",
                          county="Wake")
    assert ns.next_source == "ESTATE_IMPORT"
    assert ns.manual_or_automatic == "MANUAL"
    assert ns.queue == "ESTATE_IDENTITY_REQUIRED"
    assert "0.00" in ns.cost_usd


def test_once_the_estate_names_a_living_person_the_route_becomes_the_free_cascade():
    ns = next_best_source(owner_class="DECEASED", owner_condition="DECEASED",
                          contact_resolution_state="HEIR_ON_ROLL_NO_ACCEPTED_PHONE",
                          county="Durham", has_living_identity=True)
    assert ns.next_source == "NCSBE_PUBLIC_VOTER_FILE"
    assert ns.queue == "ESTATE_IDENTITY_FOUND_CONTACT_NEEDED"


def test_wake_identity_without_a_phone_is_a_source_coverage_fact():
    ns = next_best_source(owner_class="LIVING_PERSON", owner_condition="LIVING",
                          contact_resolution_state="IDENTITY_MATCHED_VOTER_PHONE_ABSENT",
                          county="Wake")
    assert ns.next_source == "NONE_FREE"
    assert "0.0% phone fill" in ns.blocking_reason
    assert ns.queue == "FREE_ROUTES_EXHAUSTED"


def test_nonprofit_gets_the_one_free_per_county_entity_file():
    ns = next_best_source(owner_class="NONPROFIT", owner_condition="ENTITY_ACTIVE",
                          contact_resolution_state="NONPROFIT_NO_990_PHONE",
                          county="Cabarrus")
    assert "Non-Profits" in ns.next_source
    assert "Cabarrus (Excel)" in ns.required_input


def test_llc_routes_to_sos_and_says_bulk_is_paid():
    ns = next_best_source(owner_class="LLC", owner_condition="ENTITY_ACTIVE",
                          contact_resolution_state="ENTITY_NEEDS_LAWFUL_SOS_IMPORT",
                          county="Union")
    assert ns.next_source == "SOS_IMPORT"
    assert "paid" in ns.blocking_reason.lower()
    assert "scripted" in ns.blocking_reason.lower()


def test_trust_without_a_named_trustee_goes_to_the_instrument():
    ns = next_best_source(owner_class="TRUST", owner_condition="TRUST_HELD",
                          contact_resolution_state="TRUST_NEEDS_TRUSTEE_IDENTITY",
                          county="Wake")
    assert "RECORDED_DEED" in ns.next_source
    assert ns.queue == "TRUSTEE_REQUIRED"


def test_government_is_excluded_not_merely_unresolved():
    ns = next_best_source(owner_class="GOVERNMENT", owner_condition="GOVERNMENT",
                          contact_resolution_state="GOVERNMENT_NOT_A_SELLER_CONTACT")
    assert ns.next_source == "NOT_A_SELLER"
    assert ns.queue == "NOT_A_SELLER_CONTACT"


def test_a_high_phone_short_circuits_every_other_route():
    ns = next_best_source(owner_class="DECEASED", owner_condition="DECEASED",
                          contact_resolution_state="PHONE_APPENDED_HIGH",
                          county="Wake", has_high_phone=True)
    assert ns.queue == "CONTACT_FOUND_HIGH"
    assert ns.next_source == "NONE_FREE"


def test_every_queue_has_an_operator_facing_status():
    for q in OPERATOR_QUEUES:
        assert QUEUE_CONTACT_STATUS.get(q), q


# ---------------------------------------------------------------------------
# 6. data-quality court
# ---------------------------------------------------------------------------

def test_total_mirroring_is_contamination():
    rows = [{"property_zip": "85205", "mailing_zip": "85205"} for _ in range(50)]
    f = quality.check_side_mirroring(rows, "property_zip", "mailing_zip")
    assert f.severity == "CONTAMINATED"
    assert f.measurable == 50 and f.affected == 50


def test_owner_occupancy_agreement_is_not_contamination():
    """76% of CURRENT rows have property_city == mailing_city. That is what
    owner-occupancy looks like, and flagging it would bury the real defect."""
    rows = [{"property_city": "CHARLOTTE", "mailing_city": "CHARLOTTE"} for _ in range(76)]
    rows += [{"property_city": "CHARLOTTE", "mailing_city": "BROOKLYN"} for _ in range(24)]
    f = quality.check_side_mirroring(rows, "property_city", "mailing_city")
    assert f.severity == "CLEAN"


def test_five_digit_house_number_is_not_read_as_a_zip():
    """The detector must not reproduce the very bug it hunts."""
    rows = [{"property_address_raw": "17104 NIBLICK LN CORNELIUS NC",
             "property_zip": "28031"}] * 5
    f = quality.check_against_property_line(rows, "property_zip")
    assert f.measurable == 0
    assert "NOT MEASURABLE" in f.detail
    assert f.severity == "CLEAN"


def test_apn_suffix_letters_are_legitimate_not_defects():
    """Mecklenburg carries 14,144 eight-char APNs and 4 of nine; the nine are
    '09110104B'. Flagging the majority as short is a detector bug."""
    rows = [{"county": "Mecklenburg", "apn": f"0911{i:04d}"} for i in range(100)]
    rows += [{"county": "Mecklenburg", "apn": "09110104B"}]
    f = quality.check_apn_shape(rows)
    assert f.affected == 0
    assert f.severity == "CLEAN"


def test_dropped_leading_zero_is_caught():
    rows = [{"county": "Mecklenburg", "apn": f"0911{i:04d}"} for i in range(100)]
    rows += [{"county": "Mecklenburg", "apn": "9110104"}]
    f = quality.check_apn_shape(rows)
    assert f.affected == 1


def test_small_population_never_reads_as_systematic():
    rows = [{"property_zip": "1", "mailing_zip": "1"}] * 3
    assert quality.check_side_mirroring(rows, "property_zip", "mailing_zip").severity == "CLEAN"


# ---------------------------------------------------------------------------
# 7. tier_c promotion rule
# ---------------------------------------------------------------------------

def test_tier_c_is_certified_but_not_armed_in_production():
    st = tier_c.current_status()
    assert st["status"] == "CERTIFIED_NOT_ARMED"
    assert st["incremental_contacts_today"] == 0
    assert st["incremental_contacts_if_armed"] == 13


def test_tier_c_promotes_nothing_without_arbiter_evidence():
    rows = [{"candidate_id": "x", "subcondition": tier_c.CERTIFIED_SUBCONDITION,
             "address_corroborated": "YES"}]
    d = tier_c.evaluate(rows, ["x"], arbiters=[])
    assert d.promoted == []
    assert d.incremental_contacts == 0


def test_tier_c_arms_only_with_both_independent_arbiters():
    rows = [{"candidate_id": "x", "subcondition": tier_c.CERTIFIED_SUBCONDITION,
             "address_corroborated": "YES"}]
    partial = tier_c.evaluate(rows, ["x"], arbiters=[
        tier_c.ArbiterEvidence("NC_ONEMAP_PARCEL_FABRIC", "h", loaded=True)])
    assert partial.promoted == []
    full = tier_c.evaluate(rows, ["x"], arbiters=[
        tier_c.ArbiterEvidence("NC_ONEMAP_PARCEL_FABRIC", "h1", loaded=True),
        tier_c.ArbiterEvidence("CENSUS_ZCTA_GAZETTEER", "h2", loaded=True)])
    assert full.status == "ARMED" and full.promoted == ["x"]
    assert full.arbiter_hashes == {"NC_ONEMAP_PARCEL_FABRIC": "h1",
                                   "CENSUS_ZCTA_GAZETTEER": "h2"}


def test_tier_c_never_promotes_a_name_only_match():
    rows = [{"candidate_id": "x", "subcondition": tier_c.CERTIFIED_SUBCONDITION,
             "address_corroborated": "NO"}]
    d = tier_c.evaluate(rows, ["x"], arbiters=[
        tier_c.ArbiterEvidence("NC_ONEMAP_PARCEL_FABRIC", "h1", loaded=True),
        tier_c.ArbiterEvidence("CENSUS_ZCTA_GAZETTEER", "h2", loaded=True)])
    assert d.promoted == [] and "x" in d.refused


def test_the_voter_file_may_not_arbitrate_a_voter_file_match():
    ok, why = tier_c.arming_precondition([
        tier_c.ArbiterEvidence("NC_ONEMAP_PARCEL_FABRIC", "h1", loaded=True),
        tier_c.ArbiterEvidence("CENSUS_ZCTA_GAZETTEER", "h2", loaded=True),
        tier_c.ArbiterEvidence("NCSBE_PUBLIC_VOTER_FILE", "h3", loaded=True),
    ])
    assert not ok and "voter file" in why


# ---------------------------------------------------------------------------
# 8. source registry
# ---------------------------------------------------------------------------

def test_no_source_is_marked_automatable_when_its_terms_forbid_it():
    for s in all_sources():
        if "not permitted" in s.terms or "403" in s.terms:
            assert s.acquisition != "AUTOMATABLE", s.official_source


def test_paid_sources_are_blocked_in_a_zero_spend_lane():
    for s in all_sources():
        if s.source_type == "PAID_SUBSCRIPTION":
            assert s.acquisition == "BLOCKED"
            assert "PAID" in s.cost_usd


def test_every_current_county_has_an_estate_route():
    counties = {s.county for s in estate_sources() if s.county}
    assert counties == {"Cabarrus", "Durham", "Gaston", "Johnston",
                        "Mecklenburg", "Union", "Wake"}


def test_every_manual_source_states_the_operator_steps():
    for s in all_sources():
        if s.acquisition == "MANUAL":
            assert s.operator_steps.strip(), s.official_source


def test_every_blocked_source_states_why():
    for s in all_sources():
        if s.acquisition == "BLOCKED":
            assert s.blocking_reason.strip(), s.official_source


# ---------------------------------------------------------------------------
# 9. yield engine
# ---------------------------------------------------------------------------

def test_never_attempted_is_not_a_zero_yield():
    cells = build_yield([])
    assert cells == {} or all(not v for v in cells.values())
    rows = build_yield([{"owner_class": "LLC", "county": "Wake",
                         "primary_recipe": "r", "source_family": "SOS_IMPORT",
                         "identity_resolved": False, "phone_found": False}])
    cell = rows["OWNER_CLASS"][0]
    assert cell["attempted"] == 1
    assert cell["phone_rate"] == 0.0
    assert "NEVER ATTEMPTED" not in cell["note"]


def test_recipe_intelligence_does_not_invent_outcomes():
    rows = recipe_contact_intelligence([{"primary_recipe": "heir", "owner_class": "HEIRS",
                                         "qualification_state": "QUALIFIED_CANDIDATE"}])
    assert rows[0]["conversation"] == "NOT_MEASURED"
    assert rows[0]["close"] == "NOT_MEASURED"


# ---------------------------------------------------------------------------
# 10. identity ledger
# ---------------------------------------------------------------------------

class _Res:
    def __init__(self, city="CHARLOTTE", zip5="28205"):
        self.city, self.zip5 = city, zip5


class _Voter:
    identity_hash = "abc123"
    source_hash = "hash"
    observed_at = "2026-08-16T00:00:00Z"
    status = "ACTIVE"
    res = _Res()

    def __init__(self, phone_raw=""):
        self.phone_raw = phone_raw


class _Decision:
    tier = "TIER_A"
    confidence = "STRONG_IDENTITY"
    reason = "TIER_A:property"

    def __init__(self, voter, phone=None):
        self.voter, self.phone = voter, phone


class _Person:
    full_normalized = "JANE DOE"
    role = "OWNER"


def test_identity_ledger_records_a_named_person_with_no_phone():
    rows = identity_rows([({"candidate_id": "c1", "county": "Wake", "apn": "1",
                            "fips": "37183", "owner_name": "DOE JANE"},
                           _Person(), _Decision(_Voter(phone_raw="")))])
    assert len(rows) == 1
    r = rows[0]
    assert r["matched_person"] == "JANE DOE"
    assert r["phone_present"] == "NO"
    assert r["phone_absent_reason"] == "VOTER_RECORD_CARRIES_NO_PHONE"
    assert r["relationship"] == "RECORDED_OWNER"
    assert r["callability_status"] == "NOT_AUTHORIZED"
    assert set(r) == set(IDENTITY_LEDGER_COLUMNS)


def test_identity_ledger_separates_no_phone_column_from_invalid_phone():
    rows = identity_rows([({"candidate_id": "c1"}, _Person(),
                           _Decision(_Voter(phone_raw="000")))])
    assert rows[0]["phone_absent_reason"].startswith("PHONE_PRESENT_BUT_NOT_VALID_US")


# ---------------------------------------------------------------------------
# 11. assembly end to end
# ---------------------------------------------------------------------------

ESTATE = [
    {"candidate_id": "c1", "county": "Mecklenburg", "fips": "37119", "apn": "01",
     "owner_name": "SMITH JANE", "qualification_state": "RESEARCH_CANDIDATE",
     "primary_recipe": "r1", "score": "46", "global_rank": "637",
     "property_address_raw": "1 MAIN ST", "mailing_address": "1 MAIN ST"},
    {"candidate_id": "c2", "county": "Wake", "fips": "37183", "apn": "02",
     "owner_name": "DOE JOHN HEIRS", "qualification_state": "QUALIFIED_CANDIDATE",
     "primary_recipe": "heir", "score": "66", "global_rank": "31",
     "property_address_raw": "2 OAK ST", "mailing_address": "2 OAK ST"},
]
APPENDS = [
    {"candidate_id": "c1", "matched_person": "JANE SMITH", "person_role": "OWNER",
     "owner_class": "LIVING_PERSON", "phone_role": "OWNER_PERSONAL",
     "phone": "+17045550111", "confidence_category": "HIGH", "confidence_score": "95",
     "phone_source": "NCSBE_PUBLIC_VOTER_FILE", "why_matched": "TIER_A",
     "observed_at": "2026-08-16T00:00:00Z", "source_record_identity": "v1",
     "source_hash": "h", "owner_name": "SMITH JANE"},
]
LEDGER = [
    {"candidate_id": "c1", "owner_class": "LIVING_PERSON",
     "contact_resolution_state": "PHONE_APPENDED_HIGH"},
    {"candidate_id": "c2", "owner_class": "HEIRS",
     "contact_resolution_state": "DECEASED_OWNER_NEEDS_HEIR_IDENTITY"},
]


def test_build_records_assembles_both_dimensions():
    recs = build_records(ESTATE, APPENDS, LEDGER, "rev1")
    by_id = {r.candidate_id: r for r in recs}
    assert by_id["c1"].high_phones and by_id["c1"].operator_queue == "CONTACT_FOUND_HIGH"
    assert by_id["c2"].operator_queue == "ESTATE_IDENTITY_REQUIRED"
    # opportunity carried verbatim
    assert by_id["c2"].score == 66.0 and by_id["c2"].global_rank == 31
    assert all(r.callability_status == "NOT_AUTHORIZED" for r in recs)


def test_identity_ledger_adds_named_parties_without_duplicating_phone_ones():
    idl = [
        {"candidate_id": "c1", "matched_person": "JANE SMITH", "relationship": "RECORDED_OWNER",
         "phone_present": "YES", "identity_source": "NCSBE_PUBLIC_VOTER_FILE",
         "why_matched": "TIER_A", "observed_at": "2026-08-16T00:00:00Z"},
        {"candidate_id": "c2", "matched_person": "MARY DOE", "relationship": "HEIR",
         "phone_present": "NO", "identity_source": "NCSBE_PUBLIC_VOTER_FILE",
         "why_matched": "TIER_A", "observed_at": "2026-08-16T00:00:00Z",
         "phone_absent_reason": "VOTER_RECORD_CARRIES_NO_PHONE"},
    ]
    recs = build_records(ESTATE, APPENDS, LEDGER, "rev1", identity_ledger=idl)
    by_id = {r.candidate_id: r for r in recs}
    # c1's phone-bearing party is not duplicated as a contactless copy
    assert len(by_id["c1"].relationships) == 1
    # c2 gains a named heir with no contact point
    assert by_id["c2"].has_living_identity
    assert by_id["c2"].relationships[0].role == "HEIR"
    assert by_id["c2"].relationships[0].party.contact_points == []
    # and the route moves on accordingly
    assert by_id["c2"].operator_queue == "ESTATE_IDENTITY_FOUND_CONTACT_NEEDED"


def test_qualified_board_explains_every_qualified_row():
    recs = build_records(ESTATE, APPENDS, LEDGER, "rev1")
    board = to_qualified_board(recs)
    assert len(board) == 1
    row = board[0]
    assert row["candidate_id"] == "c2"
    assert row["next_source"] and row["blocker"]
    assert row["callability_status"] == "NOT_AUTHORIZED"

"""T30: the decedent's household, and every promotion it must refuse."""
import unittest

from app.contact.address_normalize import parse_address
from app.contact.household import (
    HouseholdVoter,
    alternate_surnames,
    decedent_surnames,
    household_for_candidate,
    relationship_note,
)
from app.contact.owner_identity import parse_owner_name
from app.resolution.contract import (
    ContractViolation,
    Evidence,
    Party,
    Relationship,
)


def voter(last, first, ncid="", status="ACTIVE", reason="", conf="", phone="",
          mid="", byear="1970"):
    return HouseholdVoter(
        ncid=ncid, last=last, first=first, middle=mid, status=status,
        reason=reason, confidential=conf, phone_raw=phone, birth_year=byear,
        res=parse_address("123 MAIN ST", city="RALEIGH", state="NC", zipcode="27601"),
        source_file="ncvoter92.txt", source_hash="abc123", observed_at="2026-08-20",
    )


class SurnameExtraction(unittest.TestCase):
    def test_both_county_formats_yield_the_same_surname(self):
        """Union writes no comma, Wake writes one. The surname must not depend
        on which county typed the row."""
        a = parse_owner_name("HALVORSEN ELSPETH MAE HEIRS")
        b = parse_owner_name("HALVORSEN, ELSPETH MAE HEIRS")
        self.assertEqual(decedent_surnames(a.people), decedent_surnames(b.people))
        self.assertEqual(decedent_surnames(a.people), {"HALVORSEN"})

    def test_the_second_reading_of_an_ambiguous_line_is_not_a_surname(self):
        """`SURNAME FIRST MIDDLE HEIRS` might be western order, so the middle
        token is kept --
        but as an alternate, never as an equal."""
        op = parse_owner_name("HALVORSEN ELSPETH MAE HEIRS")
        self.assertEqual(alternate_surnames(op.people), {"MAE"})
        self.assertNotIn("MAE", decedent_surnames(op.people))

    def test_a_neighbour_matching_only_the_alternate_reading_stays_review(self):
        op = parse_owner_name("SOWELL FREDDIE HEIRS")
        r = household_for_candidate(
            owner_people=op.people,
            situs_voters=[voter("FREDDIE", "ANNA", ncid="Z1")],
        )
        self.assertEqual(r.surname_members, [])
        self.assertEqual(r.members[0].confidence, "REVIEW")
        self.assertEqual(r.members[0].match_method,
                         "HOUSEHOLD_CO_RESIDENT_SURNAME_ORDER_AMBIGUOUS")


class HouseholdDiscovery(unittest.TestCase):
    def setUp(self):
        self.owner = parse_owner_name("JOHNS, CARL HEIRS").people

    def test_surname_match_at_situs_is_family_evidence(self):
        r = household_for_candidate(
            owner_people=self.owner,
            situs_voters=[voter("JOHNS", "CARL", ncid="AA1")],
            situs_label="123 MAIN ST 27601",
        )
        self.assertEqual(len(r.surname_members), 1)
        m = r.surname_members[0]
        self.assertEqual(m.relationship_class, "FAMILY")
        self.assertEqual(m.relationship_role, "OTHER_VERIFIED_RELATION")
        self.assertEqual(m.family_relation, "SURNAME_CORROBORATED_RELATIVE")
        self.assertEqual(m.confidence, "HIGH")

    def test_the_module_can_never_emit_an_authority_role(self):
        r = household_for_candidate(
            owner_people=self.owner,
            situs_voters=[voter("JOHNS", "CARL"), voter("JOHNS", "MAE")],
        )
        roles = {m.relationship_role for m in r.members}
        self.assertEqual(roles, {"OTHER_VERIFIED_RELATION"})
        self.assertNotIn("HEIR", roles)
        self.assertNotIn("EXECUTOR", roles)
        self.assertNotIn("ADMINISTRATOR", roles)
        self.assertNotIn("PERSONAL_REPRESENTATIVE", roles)
        self.assertEqual({m.relationship_class for m in r.members}, {"FAMILY"})

    def test_different_surname_co_resident_is_review_not_high(self):
        r = household_for_candidate(
            owner_people=self.owner,
            situs_voters=[voter("PEREZ", "ANA", ncid="B1")],
        )
        self.assertEqual(r.surname_members, [])
        self.assertEqual(r.members[0].confidence, "REVIEW")
        self.assertEqual(r.members[0].family_relation, "HOUSEHOLD_CO_RESIDENT")

    def test_confidential_registrant_never_leaves_the_module(self):
        r = household_for_candidate(
            owner_people=self.owner,
            situs_voters=[voter("JOHNS", "SECRET", conf="Y")],
        )
        self.assertEqual(r.members, [])
        self.assertEqual(r.skipped_confidential, 1)

    def test_removed_registration_is_not_a_living_party(self):
        r = household_for_candidate(
            owner_people=self.owner,
            situs_voters=[voter("JOHNS", "OLD", status="REMOVED")],
        )
        self.assertEqual(r.members, [])
        self.assertEqual(r.skipped_not_live, 1)

    def test_deceased_flag_is_corroboration_not_a_living_party(self):
        r = household_for_candidate(
            owner_people=self.owner,
            situs_voters=[voter("JOHNS", "CARL", reason="DECEASED")],
        )
        self.assertEqual(r.members, [])
        self.assertEqual(len(r.decedent_registrations), 1)

    def test_two_relatives_are_two_people_not_a_pick(self):
        r = household_for_candidate(
            owner_people=self.owner,
            situs_voters=[voter("JOHNS", "CARL", ncid="A1"),
                          voter("JOHNS", "MAE", ncid="A2")],
        )
        self.assertEqual(r.distinct_person_count, 2)
        self.assertTrue(r.multiple_relatives)

    def test_near_name_variants_are_not_merged(self):
        """MILLET / MILLETT BOYKIN are two registrations. The file says so."""
        owner = parse_owner_name("BOYKIN, CATHERINE H HEIRS").people
        r = household_for_candidate(
            owner_people=owner,
            situs_voters=[voter("BOYKIN", "MILLET", ncid="C1"),
                          voter("BOYKIN", "MILLETT", ncid="C2")],
        )
        self.assertEqual(r.distinct_person_count, 2)

    def test_same_ncid_at_both_addresses_is_one_person_kept_at_situs(self):
        v = voter("JOHNS", "CARL", ncid="A1")
        r = household_for_candidate(
            owner_people=self.owner, situs_voters=[v], mailing_voters=[v],
        )
        self.assertEqual(len(r.members), 1)
        self.assertEqual(r.members[0].observed_at_address, "SITUS")

    def test_output_is_deterministic(self):
        vs = [voter("JOHNS", "CARL", ncid="A1"), voter("JOHNS", "MAE", ncid="A2")]
        a = household_for_candidate(owner_people=self.owner, situs_voters=vs)
        b = household_for_candidate(owner_people=self.owner, situs_voters=vs)
        self.assertEqual([m.voter.ncid for m in a.members],
                         [m.voter.ncid for m in b.members])

    def test_note_states_the_limit_alongside_the_finding(self):
        r = household_for_candidate(
            owner_people=self.owner, situs_voters=[voter("JOHNS", "CARL")])
        note = relationship_note(r.members[0], "CARL JOHNS")
        self.assertIn("not an adjudicated heirship", note)
        self.assertIn("no authority", note)


class FamilyIsNotAuthority(unittest.TestCase):
    """The contract-level guard, independent of the household module."""

    def _evidence(self, family):
        return Evidence(source_family=family, source_name=family,
                        field="party_identity", raw_value="JANE SMITH",
                        normalized_value="JANE SMITH", match_method="X",
                        confidence="HIGH", observed_at="2026-08-20",
                        retrieved_at="2026-08-20", source_locator="loc")

    def _party(self, name="JANE SMITH", family="COUNTY_PARCEL_ROLL"):
        return Party(kind="PERSON", canonical_name=name,
                     evidence=[self._evidence(family)])

    def test_voter_file_cannot_confer_legal_authority(self):
        with self.assertRaises(ContractViolation):
            Relationship(party=self._party(), role="EXECUTOR",
                         evidence=[self._evidence("NCSBE_PUBLIC_VOTER_FILE")],
                         relationship_class="LEGAL_AUTHORITY")

    def test_parcel_roll_cannot_confer_legal_authority(self):
        with self.assertRaises(ContractViolation):
            Relationship(party=self._party(), role="ADMINISTRATOR",
                         evidence=[self._evidence("COUNTY_PARCEL_ROLL")],
                         relationship_class="LEGAL_AUTHORITY")

    def test_estate_file_may_confer_legal_authority(self):
        rel = Relationship(party=self._party(), role="EXECUTOR",
                           evidence=[self._evidence("ESTATE_IMPORT")],
                           relationship_class="LEGAL_AUTHORITY")
        self.assertTrue(rel.is_legal_authority)

    def test_operator_manual_public_record_may_confer_legal_authority(self):
        rel = Relationship(party=self._party(), role="PERSONAL_REPRESENTATIVE",
                           evidence=[self._evidence("OPERATOR_MANUAL_PUBLIC_RECORD")],
                           relationship_class="LEGAL_AUTHORITY")
        self.assertTrue(rel.is_legal_authority)

    def test_roll_stated_executor_defaults_to_role_stated_not_authority(self):
        rel = Relationship(party=self._party(), role="EXECUTOR",
                           evidence=[self._evidence("COUNTY_PARCEL_ROLL")])
        self.assertEqual(rel.relationship_class, "ROLE_STATED_ON_RECORD")
        self.assertFalse(rel.is_legal_authority)

    def test_recorded_owner_defaults_to_property_interest(self):
        rel = Relationship(party=self._party(), role="RECORDED_OWNER",
                           evidence=[self._evidence("COUNTY_PARCEL_ROLL")])
        self.assertEqual(rel.relationship_class, "PROPERTY_INTEREST")

    def test_family_relation_requires_family_class(self):
        with self.assertRaises(ContractViolation):
            Relationship(party=self._party(), role="RECORDED_OWNER",
                         evidence=[self._evidence("COUNTY_PARCEL_ROLL")],
                         family_relation="CHILD")

    def test_unknown_family_relation_is_refused(self):
        with self.assertRaises(ContractViolation):
            Relationship(party=self._party(), role="OTHER_VERIFIED_RELATION",
                         evidence=[self._evidence("NCSBE_PUBLIC_VOTER_FILE")],
                         relationship_class="FAMILY", family_relation="COUSIN_TWICE")

    def test_family_relationship_serialises_both_facts(self):
        rel = Relationship(party=self._party(), role="OTHER_VERIFIED_RELATION",
                           evidence=[self._evidence("NCSBE_PUBLIC_VOTER_FILE")],
                           relationship_class="FAMILY",
                           family_relation="SURNAME_CORROBORATED_RELATIVE")
        d = rel.to_dict()
        self.assertEqual(d["relationship_class"], "FAMILY")
        self.assertEqual(d["family_relation"], "SURNAME_CORROBORATED_RELATIVE")
        self.assertFalse(d["is_legal_authority"])


if __name__ == "__main__":
    unittest.main()


class OperatorWritebackContract(unittest.TestCase):
    """T30 §12 — the one path human research enters the graph, and its refusals."""

    def setUp(self):
        import importlib.util, pathlib
        spec = importlib.util.spec_from_file_location(
            "t30_writeback",
            pathlib.Path(__file__).resolve().parents[1]
            / "scripts/import_operator_estate_research.py")
        self.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        self.known = {"cand-1"}

    def _row(self, **over):
        base = {
            "candidate_id": "cand-1", "decedent_name": "SMITH JOHN",
            "county": "Wake", "estate_file_number": "24 E 1", "estate_status": "OPEN",
            "filing_date": "2026-01-02", "person_name": "MARY SMITH",
            "relationship_role": "EXECUTOR", "relationship_class": "LEGAL_AUTHORITY",
            "family_relation": "", "source_family": "ESTATE_IMPORT",
            "source_name": "WAKE_CLERK", "source_locator": "file 24 E 1",
            "observed_at": "2026-08-20", "confidence": "HIGH",
            "evidence_note": "the file names her as executor", "operator": "op",
        }
        base.update(over)
        return base

    def test_estate_file_may_confer_authority(self):
        ok, bad = self.mod.validate([self._row()], self.known)
        self.assertEqual(bad, [])
        self.assertEqual(len(ok), 1)

    def test_parcel_roll_may_not_confer_authority(self):
        ok, bad = self.mod.validate(
            [self._row(source_family="COUNTY_PARCEL_ROLL")], self.known)
        self.assertEqual(ok, [])
        self.assertIn("LEGAL_AUTHORITY refused", bad[0]["errors"][0])

    def test_voter_file_may_not_confer_authority(self):
        ok, bad = self.mod.validate(
            [self._row(source_family="NCSBE_PUBLIC_VOTER_FILE")], self.known)
        self.assertEqual(ok, [])

    def test_the_same_person_may_be_recorded_as_role_stated(self):
        """The refusal is of the CLAIM, not of the research. Downgrade and it lands."""
        ok, bad = self.mod.validate(
            [self._row(source_family="COUNTY_PARCEL_ROLL",
                       relationship_class="ROLE_STATED_ON_RECORD")], self.known)
        self.assertEqual(bad, [])
        self.assertEqual(len(ok), 1)

    def test_unknown_candidate_is_refused(self):
        ok, bad = self.mod.validate([self._row(candidate_id="nope")], self.known)
        self.assertEqual(ok, [])
        self.assertTrue(any("not a current candidate" in e for e in bad[0]["errors"]))

    def test_family_relation_without_family_class_is_refused(self):
        ok, bad = self.mod.validate(
            [self._row(family_relation="CHILD")], self.known)
        self.assertEqual(ok, [])

    def test_free_text_alone_cannot_enter_the_graph(self):
        """A note is not evidence: the locator and the source are required."""
        ok, bad = self.mod.validate(
            [self._row(source_locator="", evidence_note="I called and she said so")],
            self.known)
        self.assertEqual(ok, [])
        self.assertTrue(any("source_locator is required" in e for e in bad[0]["errors"]))

    def test_bad_date_is_refused(self):
        ok, bad = self.mod.validate([self._row(observed_at="last tuesday")], self.known)
        self.assertEqual(ok, [])

    def test_built_evidence_is_a_content_address(self):
        built_a = self.mod.build([self._row()], {"cand-1": "t1"})
        built_b = self.mod.build([self._row()], {"cand-1": "t1"})
        self.assertEqual(built_a[0]["evidence_id"], built_b[0]["evidence_id"])

"""T30A learning capture: the procedure must encode what the court actually paid for."""
import unittest

from app.resolution.source_procedure import (
    NC_ECOURTS_ESTATE_SMART_SEARCH_V1 as P,
    NEGATIVE_EVIDENCE_STATES,
    NON_EVIDENTIAL_STATES,
    RESULT_STATES,
    procedure_for,
    supports_replay_suppression,
)
from app.resolution.sources import estate_sources


class ProcedureIdentity(unittest.TestCase):
    def test_it_knows_its_own_name_version_and_provenance(self):
        self.assertEqual(P.name, "NC_ECOURTS_ESTATE_SMART_SEARCH")
        self.assertEqual(P.version, "1.0.0")
        self.assertEqual(P.source_family, "PROBATE_INDEX")
        self.assertIn("T30A", P.taught_by)
        self.assertEqual(P.observed_at, "2026-08-20")

    def test_it_is_reachable_by_source_family(self):
        self.assertIs(procedure_for("PROBATE_INDEX"), P)
        self.assertIs(procedure_for("probate_index"), P)

    def test_an_unlearned_source_returns_none_not_a_guess(self):
        self.assertIsNone(procedure_for("SOME_SOURCE_NOBODY_HAS_WORKED"))

    def test_it_records_when_the_behavior_was_last_verified(self):
        self.assertIn("2026-08-20", P.last_verified_behavior)


class TheLessonsThisCourtPaidFor(unittest.TestCase):
    def test_the_middle_name_lesson_is_captured(self):
        steps = " ".join(P.steps).lower()
        self.assertIn("last, first", steps)
        self.assertIn("middle token literally", steps)

    def test_the_county_scope_trap_is_captured(self):
        steps = " ".join(P.steps).lower()
        self.assertIn("uncheck", steps)
        self.assertIn("all locations", steps)
        self.assertIn("no-op", steps)

    def test_the_403_is_not_recorded_as_source_unavailable(self):
        blob = (P.last_verified_behavior + " " + " ".join(P.steps)).lower()
        self.assertIn("403", blob)
        self.assertIn("ordinary browser", blob)

    def test_the_decedent_not_candidate_dedupe_lesson_is_captured(self):
        self.assertTrue(any("decedent" in c.lower() and "parcel" in c.lower()
                            for c in P.precheck))

    def test_false_match_examples_are_concrete(self):
        blob = " ".join(P.false_match_examples).lower()
        self.assertIn("different county", blob)
        self.assertIn("party on somebody else", blob)
        self.assertIn("three coincidences", blob)

    def test_case_found_is_not_authority_found(self):
        blob = " ".join(P.evidence_law).lower()
        self.assertIn("found does not create authority", blob)
        self.assertIn("small-estate affidavit", blob)

    def test_family_vs_authority_law_is_carried(self):
        blob = " ".join(P.evidence_law).lower()
        self.assertIn("obituary -> family evidence", blob)
        self.assertIn("role_stated_on_record", blob)
        self.assertIn("legal_authority, and nothing else earns it", blob)

    def test_no_match_never_claims_no_estate_exists(self):
        blob = " ".join(P.limitations).lower()
        self.assertIn("never establishes that no estate exists", blob)


class HumanBoundary(unittest.TestCase):
    def test_verification_is_an_interaction_not_a_failure(self):
        b = P.human_boundary.lower()
        self.assertIn("not a source failure", b)
        self.assertIn("not a no-match", b)
        self.assertIn("never be solved by the agent", b)

    def test_a_captcha_may_never_suppress_a_replay(self):
        self.assertFalse(supports_replay_suppression("HUMAN_VERIFICATION_REQUIRED"))
        self.assertFalse(supports_replay_suppression("SEARCH_INCONCLUSIVE"))

    def test_only_a_real_negative_suppresses_a_replay(self):
        self.assertTrue(supports_replay_suppression("PUBLIC_INDEX_NO_MATCH"))
        for state in RESULT_STATES:
            if state != "PUBLIC_INDEX_NO_MATCH":
                self.assertFalse(supports_replay_suppression(state), state)

    def test_non_evidential_states_are_disjoint_from_negative_evidence(self):
        self.assertEqual(NON_EVIDENTIAL_STATES & NEGATIVE_EVIDENCE_STATES, frozenset())

    def test_the_states_are_not_collapsible_to_match_or_no_match(self):
        self.assertGreaterEqual(len(RESULT_STATES), 6)
        self.assertIn("AMBIGUOUS_MATCH", RESULT_STATES)
        self.assertIn("OFFICIAL_COPY_REQUIRED", RESULT_STATES)


class SourceRegistryCorrected(unittest.TestCase):
    def _ecourts(self):
        return next(s for s in estate_sources()
                    if "eCourts" in s.official_source and s.county == "")

    def test_the_portal_is_no_longer_recorded_as_blocked(self):
        """It was BLOCKED on the strength of a 403 to a script. A browser reaches it."""
        e = self._ecourts()
        self.assertEqual(e.acquisition, "MANUAL")
        self.assertEqual(e.blocking_reason, "")

    def test_it_still_forbids_defeating_the_controls(self):
        e = self._ecourts()
        self.assertIn("Neither control may be defeated", e.terms)
        self.assertIn("human-verification", e.terms)

    def test_it_points_at_the_procedure_rather_than_repeating_it(self):
        e = self._ecourts()
        self.assertIn("NC_ECOURTS_ESTATE_SMART_SEARCH_V1", e.operator_steps)

    def test_the_recheck_date_moved(self):
        self.assertEqual(self._ecourts().last_checked, "2026-08-20")


if __name__ == "__main__":
    unittest.main()

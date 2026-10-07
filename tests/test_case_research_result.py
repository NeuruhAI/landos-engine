"""The seam between this engine's result vocabulary and the product's.

These tests exist because the failure they prevent is silent. If the engine
adds a result state and the mapping does not, a real finding arrives at the
product as unrecognised and is dropped -- and nothing anywhere raises.
"""
import unittest

from app.case import (
    ENGINE_TO_GENERIC,
    GENERIC_RESULT_STATES,
    NON_EVIDENTIAL_GENERIC_STATES,
    REPLAY_SUPPRESSING_GENERIC_STATES,
    CaseSourceAttempt,
    generic_result_state,
    is_evidential,
    is_positive,
    supports_replay_suppression_generic,
)
from app.case.attempt import AttemptRefused, insert_params
from app.case.research_result import audit
from app.resolution.source_procedure import (
    NEGATIVE_EVIDENCE_STATES,
    NON_EVIDENTIAL_STATES,
    RESULT_STATES as ENGINE_RESULT_STATES,
)


class SeamTests(unittest.TestCase):
    def test_every_engine_state_is_mapped(self):
        # Exhaustive by test, not by intention. An engine state with no mapping
        # is a finding the product will never see.
        self.assertEqual(set(ENGINE_RESULT_STATES) - set(ENGINE_TO_GENERIC), set())
        self.assertEqual(audit()["unmapped_engine_states"], [])

    def test_the_generic_vocabulary_matches_the_product(self):
        # Byte-identical to RESULT_STATES in src/lib/case/vocabulary.ts. If this
        # list drifts, a state the product persists cannot be constructed here.
        self.assertEqual(
            list(GENERIC_RESULT_STATES),
            [
                "FOUND", "PARTIAL", "NO_MATCH", "AMBIGUOUS", "INCONCLUSIVE",
                "OFFICIAL_COPY_REQUIRED", "OPERATOR_REQUIRED", "BLOCKED",
                "NOT_APPLICABLE", "DEFERRED",
            ],
        )

    def test_the_distinctions_the_engine_paid_for_survive_the_mapping(self):
        # Each of these cost something to learn. The middle-name lesson cost
        # eight would-be false negatives; the county-scope lesson cost a search
        # window.
        self.assertEqual(generic_result_state("PUBLIC_INDEX_NO_MATCH"), "NO_MATCH")
        self.assertEqual(generic_result_state("HUMAN_VERIFICATION_REQUIRED"), "OPERATOR_REQUIRED")
        self.assertEqual(generic_result_state("SEARCH_INCONCLUSIVE"), "INCONCLUSIVE")
        # A person and a scope are different problems with different next steps.
        self.assertNotEqual(
            generic_result_state("HUMAN_VERIFICATION_REQUIRED"),
            generic_result_state("SEARCH_INCONCLUSIVE"),
        )

    def test_it_accepts_either_vocabulary_and_guesses_at_neither(self):
        self.assertEqual(generic_result_state("OFFICIAL_ESTATE_CASE_FOUND"), "FOUND")
        self.assertEqual(generic_result_state("no_match"), "NO_MATCH")
        self.assertIsNone(generic_result_state("SOMETHING_ELSE"))
        self.assertIsNone(generic_result_state(""))
        self.assertIsNone(generic_result_state(None))

    def test_the_generic_guard_is_a_superset_of_the_engine_guard(self):
        # The engine has no equivalent of BLOCKED or DEFERRED and both are just
        # as non-evidential: a source that refused us, and a source nobody has
        # opened, say exactly nothing about the subject.
        mapped = {generic_result_state(s) for s in NON_EVIDENTIAL_STATES}
        self.assertTrue(mapped <= NON_EVIDENTIAL_GENERIC_STATES)
        self.assertIn("BLOCKED", NON_EVIDENTIAL_GENERIC_STATES)
        self.assertIn("DEFERRED", NON_EVIDENTIAL_GENERIC_STATES)

    def test_negative_evidence_stays_narrow(self):
        mapped = {generic_result_state(s) for s in NEGATIVE_EVIDENCE_STATES}
        self.assertTrue(mapped <= REPLAY_SUPPRESSING_GENERIC_STATES)
        self.assertEqual(REPLAY_SUPPRESSING_GENERIC_STATES, {"NO_MATCH", "NOT_APPLICABLE"})
        # An unresolved identity is not a negative and not a match.
        self.assertFalse(supports_replay_suppression_generic("AMBIGUOUS_MATCH"))

    def test_positive_means_produced_something(self):
        self.assertTrue(is_positive("OFFICIAL_ESTATE_CASE_FOUND"))
        self.assertTrue(is_positive("PARTIAL"))
        self.assertFalse(is_positive("NO_MATCH"))
        self.assertFalse(is_positive("BLOCKED"))

    def test_an_obstacle_is_never_evidence(self):
        for state in ("HUMAN_VERIFICATION_REQUIRED", "BLOCKED", "DEFERRED", "SEARCH_INCONCLUSIVE"):
            self.assertFalse(is_evidential(state), state)


def _attempt(**over):
    base = dict(
        candidate_id="11111111-1111-4111-8111-111111111111",
        subject_kind="PERSON",
        subject_ref="DAVIS JOHN WESLEY",
        domain="AUTHORITY",
        source_id="NC_ECOURTS_ESTATE",
        source_name="NC eCourts estates index",
        source_family="PROBATE_INDEX",
        result_state="PUBLIC_INDEX_NO_MATCH",
        started_at="2026-08-21T10:00:00+00:00",
    )
    base.update(over)
    return CaseSourceAttempt(**base)


class AttemptTests(unittest.TestCase):
    def test_a_positive_result_can_finally_be_written(self):
        # THE T30A GAP. The negative could be persisted by extending a CHECK
        # constraint; the positive -- an official estate case actually located --
        # had nowhere to go at all, so the most expensive result the lane
        # produced was the one it could not write down.
        found = _attempt(
            result_state="OFFICIAL_ESTATE_CASE_FOUND",
            notes="Estate 26E1234 opened 2026-02-02; administrator appointed.",
        )
        self.assertEqual(found.generic_state, "FOUND")
        self.assertTrue(found.is_evidential)
        row = found.to_row()
        self.assertEqual(row["result_state"], "FOUND")
        self.assertIn("26E1234", row["notes"])

    def test_a_negative_result_is_a_finding_and_says_so(self):
        no_match = _attempt().for_negative_evidence()
        self.assertEqual(no_match.generic_state, "NO_MATCH")
        self.assertTrue(no_match.suppresses_replay)

    def test_an_instrument_state_cannot_be_written_as_evidence(self):
        # Raises rather than returning a flag, so a caller cannot proceed by
        # ignoring a boolean.
        for state in ("HUMAN_VERIFICATION_REQUIRED", "SEARCH_INCONCLUSIVE", "BLOCKED", "DEFERRED"):
            with self.assertRaises(AttemptRefused, msg=state):
                _attempt(result_state=state).for_negative_evidence()

    def test_an_ambiguous_match_is_neither_a_negative_nor_a_match(self):
        with self.assertRaises(AttemptRefused):
            _attempt(result_state="AMBIGUOUS_MATCH").for_negative_evidence()

    def test_it_refuses_a_state_it_does_not_recognise(self):
        with self.assertRaises(AttemptRefused):
            _attempt(result_state="PROBABLY_FINE")

    def test_it_refuses_an_attempt_with_no_subject(self):
        with self.assertRaises(AttemptRefused):
            _attempt(subject_ref="   ")

    def test_it_refuses_a_cost_with_no_currency(self):
        with self.assertRaises(AttemptRefused):
            _attempt(cost=250.0)
        self.assertEqual(_attempt(cost=250.0, currency="USD").cost, 250.0)

    def test_idempotency_is_scoped_to_a_day(self):
        # A double submit is one attempt; the same search tomorrow is two,
        # because a portal's answer changes.
        a = _attempt(started_at="2026-08-21T09:00:00+00:00")
        b = _attempt(started_at="2026-08-21T23:59:00+00:00")
        c = _attempt(started_at="2026-08-22T09:00:00+00:00")
        self.assertEqual(a.idempotency_key(), b.idempotency_key())
        self.assertNotEqual(a.idempotency_key(), c.idempotency_key())

    def test_the_natural_key_matches_the_products_unique_index(self):
        a = _attempt(subject_ref="davis john wesley", source_id="nc_ecourts_estate")
        b = _attempt()
        self.assertEqual(a.natural_key(), b.natural_key())
        self.assertNotEqual(a.natural_key(), _attempt(query="different").natural_key())

    def test_insert_params_covers_every_column_the_sql_binds(self):
        params = insert_params(_attempt())
        for key in (
            "tenant_id", "candidate_id", "crm_lead_id", "parcel_apn", "subject_kind",
            "subject_ref", "subject_label", "work_unit", "domain", "source_id",
            "source_name", "source_family", "procedure", "procedure_version",
            "query", "actor_type", "started_at", "completed_at", "result_state",
            "notes", "next_action", "cost", "currency",
        ):
            self.assertIn(key, params)
        # Absent optional fields bind as NULL rather than being missing.
        self.assertIsNone(params["crm_lead_id"])

    def test_the_engine_identifies_itself_as_the_actor(self):
        self.assertEqual(_attempt().actor_type, "ENGINE")


if __name__ == "__main__":
    unittest.main()

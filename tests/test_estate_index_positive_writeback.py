"""T50A — the positive half of the estate index recorder.

T30A could persist "the index was searched and holds no matching case", because
extending a CHECK constraint by one enum value was enough for it. It could not
persist "the index was searched and an official case IS on file", because there
was no state for that and no table for the look itself. So the most expensive
result the lane produced was the one it could not write down, and the next lane
to work the candidate would search the index again.

These tests drive the planner directly, with no database, and assert what it
would write.
"""
import importlib.util
import unittest
from pathlib import Path

from app.case.attempt import AttemptRefused

ROOT = Path(__file__).resolve().parents[1]


def _executable_source(path: Path) -> str:
    """The module's code with every docstring and comment removed.

    A prose scan over a file whose prose is ABOUT the thing it must not do will
    always fail. This strips the prose and leaves the statements.
    """
    import ast

    import tokenize

    tree = ast.parse(path.read_text())
    docstring_lines = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(
            first.value.value, str
        ):
            docstring_lines.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))

    out = []
    with path.open() as fh:
        for token in tokenize.generate_tokens(fh.readline):
            if token.type == tokenize.COMMENT:
                continue
            if token.start[0] in docstring_lines:
                continue
            out.append(token.string)
    return " ".join(out)

_spec = importlib.util.spec_from_file_location(
    "record_estate_index_search", ROOT / "scripts" / "record_estate_index_search.py"
)
recorder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recorder)


TARGETS = {
    1: {"candidate_id": "11111111-1111-4111-8111-111111111111", "source_county": "Wake"},
    2: {"candidate_id": "22222222-2222-4222-8222-222222222222", "source_county": "Wake"},
    3: {"candidate_id": "33333333-3333-4333-8333-333333333333", "source_county": "Durham"},
    4: {"candidate_id": "44444444-4444-4444-8444-444444444444", "source_county": "Durham"},
}


def finding(rank, verdict, **over):
    base = {
        "rank": rank,
        "decedent_search": f"DECEDENT {rank}",
        "verdict": verdict,
        "scope": "county-scoped, verified",
    }
    base.update(over)
    return base


class PositiveWritebackTests(unittest.TestCase):
    def test_a_located_case_is_planned_for_writing(self):
        write, refuse = recorder.plan(TARGETS, [finding(1, "OFFICIAL_ESTATE_CASE_FOUND")])
        self.assertEqual(len(write), 1)
        self.assertEqual(refuse, [])
        self.assertEqual(write[0]["state"], "ESTATE_OFFICIAL_CASE_FOUND")
        self.assertTrue(write[0]["positive"])

    def test_the_negative_still_writes_exactly_as_it_did(self):
        write, _ = recorder.plan(TARGETS, [finding(2, "PUBLIC_INDEX_NO_MATCH")])
        self.assertEqual(write[0]["state"], "ESTATE_NO_PUBLIC_CASE_FOUND")
        self.assertFalse(write[0]["positive"])

    def test_an_instrument_verdict_is_still_refused(self):
        # A challenge that interrupted a session says nothing whatever about
        # whether an estate exists. This has not been loosened by adding the
        # positive path.
        for verdict in ("HUMAN_VERIFICATION_REQUIRED", "SEARCH_INCONCLUSIVE"):
            write, refuse = recorder.plan(TARGETS, [finding(3, verdict)])
            self.assertEqual(write, [], verdict)
            self.assertEqual(len(refuse), 1, verdict)
            self.assertIn("does not support replay suppression", refuse[0]["refused_because"])

    def test_an_ambiguous_match_is_neither(self):
        write, refuse = recorder.plan(TARGETS, [finding(4, "AMBIGUOUS_MATCH")])
        self.assertEqual(write, [])
        self.assertEqual(len(refuse), 1)

    def test_one_decedent_marks_every_parcel_they_own(self):
        # T30A's nine candidates were eight decedents, and two pairs shared an
        # estate. A result found once has to reach every parcel it answers for.
        write, _ = recorder.plan(
            TARGETS, [finding(1, "OFFICIAL_ESTATE_CASE_FOUND", also_ranks=[2])]
        )
        self.assertEqual(
            sorted(r["candidate_id"] for r in write),
            sorted([TARGETS[1]["candidate_id"], TARGETS[2]["candidate_id"]]),
        )

    def test_a_rank_the_manifest_does_not_carry_is_skipped_not_guessed(self):
        write, refuse = recorder.plan(TARGETS, [finding(99, "PUBLIC_INDEX_NO_MATCH")])
        self.assertEqual(write, [])
        self.assertEqual(refuse, [])


class AttemptShapeTests(unittest.TestCase):
    def _row(self, verdict="OFFICIAL_ESTATE_CASE_FOUND"):
        write, _ = recorder.plan(TARGETS, [finding(1, verdict)])
        return write[0]

    def test_the_attempt_subject_is_the_decedent_not_the_parcel(self):
        # One decedent can own several parcels. Keying the attempt on the
        # parcel would make the same person searched once look unsearched on
        # every other parcel they hold.
        attempt = recorder.attempt_for(self._row())
        self.assertEqual(attempt.subject_kind, "PERSON")
        self.assertEqual(attempt.subject_ref, "DECEDENT 1")

    def test_the_attempt_carries_the_procedure_and_its_version(self):
        attempt = recorder.attempt_for(self._row())
        self.assertEqual(attempt.procedure, recorder.PROC.name)
        self.assertEqual(attempt.procedure_version, recorder.PROC.version)
        self.assertEqual(attempt.source_family, recorder.PROC.source_family)

    def test_the_engine_signs_its_own_work(self):
        self.assertEqual(recorder.attempt_for(self._row()).actor_type, "ENGINE")

    def test_a_positive_lands_as_FOUND_in_the_products_vocabulary(self):
        attempt = recorder.attempt_for(self._row("OFFICIAL_ESTATE_CASE_FOUND"))
        self.assertEqual(attempt.generic_state, "FOUND")
        self.assertTrue(attempt.is_evidential)

    def test_a_negative_lands_as_NO_MATCH_and_suppresses_a_replay(self):
        attempt = recorder.attempt_for(self._row("PUBLIC_INDEX_NO_MATCH"))
        self.assertEqual(attempt.generic_state, "NO_MATCH")
        self.assertTrue(attempt.suppresses_replay)
        attempt.for_negative_evidence()  # does not raise

    def test_a_positive_does_not_pretend_to_suppress_a_replay(self):
        # A located case is a reason to open the FILE, not a reason to stop
        # looking. for_negative_evidence is about absence and refuses.
        attempt = recorder.attempt_for(self._row("OFFICIAL_ESTATE_CASE_FOUND"))
        with self.assertRaises(AttemptRefused):
            attempt.for_negative_evidence()

    def test_the_attempt_records_the_scope_that_was_verified(self):
        # The county-scope lesson cost a wasted search window; the note is how
        # the next lane knows the scope was checked rather than assumed.
        notes = recorder.attempt_for(self._row()).notes
        self.assertIn("county-scoped, verified", notes)
        self.assertIn("Wake", notes)

    def test_the_attempt_binds_every_column_the_insert_needs(self):
        from app.case.attempt import insert_params

        params = insert_params(recorder.attempt_for(self._row()))
        self.assertEqual(params["result_state"], "FOUND")
        self.assertEqual(params["domain"], "AUTHORITY")
        self.assertIsNotNone(params["started_at"])


class NoAuthorityClaimTests(unittest.TestCase):
    def test_a_located_case_writes_no_party_and_no_authority(self):
        # An index entry is not letters. The appointment is in the file, and
        # somebody has to read it. This module names nobody.
        #
        # Scanned over EXECUTABLE code only: docstrings and comments discuss
        # these names precisely because the module must not write them, and a
        # naive text scan would fail on its own explanation.
        code = _executable_source(ROOT / "scripts" / "record_estate_index_search.py")
        for forbidden in (
            "candidate_parties",
            "relationship_class",
            "LEGAL_AUTHORITY",
            "party_phones",
            "party_emails",
        ):
            self.assertNotIn(forbidden, code, forbidden)

    def test_the_state_map_covers_only_the_two_verdicts_that_are_results(self):
        self.assertEqual(
            set(recorder.STATE_FOR_VERDICT),
            {"PUBLIC_INDEX_NO_MATCH", "OFFICIAL_ESTATE_CASE_FOUND"},
        )


if __name__ == "__main__":
    unittest.main()

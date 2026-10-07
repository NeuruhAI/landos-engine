"""T30A: a searched-and-absent public estate index is a RESULT, not a blank."""
import unittest

from app.resolution.router import (
    QUEUE_CONTACT_STATUS,
    Q_ESTATE_ID_FOUND,
    Q_ESTATE_ID_REQUIRED,
    Q_ESTATE_NO_PUBLIC_CASE,
    next_best_source,
)


def route(**over):
    kwargs = dict(
        owner_class="DECEASED",
        owner_condition="DECEASED",
        contact_resolution_state="DECEASED_OWNER_NEEDS_HEIR_IDENTITY",
        county="Union",
        has_living_identity=False,
        has_high_phone=False,
    )
    kwargs.update(over)
    return next_best_source(**kwargs)


class SearchedAndAbsent(unittest.TestCase):
    def test_unsearched_still_goes_to_fetch_the_estate_file(self):
        r = route()
        self.assertEqual(r.next_source, "ESTATE_IMPORT")
        self.assertEqual(r.queue, Q_ESTATE_ID_REQUIRED)

    def test_searched_and_absent_goes_to_a_different_desk(self):
        r = route(estate_public_index_searched=True)
        self.assertEqual(r.queue, Q_ESTATE_NO_PUBLIC_CASE)
        self.assertNotEqual(r.next_source, "ESTATE_IMPORT")
        self.assertIn("RECORDED_DEED", r.next_source)

    def test_the_two_desks_are_distinguishable(self):
        """The whole point: these produce different work."""
        a = route()
        b = route(estate_public_index_searched=True)
        self.assertNotEqual(a.queue, b.queue)
        self.assertNotEqual(a.next_source, b.next_source)
        self.assertNotEqual(a.required_input, b.required_input)

    def test_it_does_not_claim_no_estate_exists(self):
        r = route(estate_public_index_searched=True)
        blob = f"{r.why} {r.expected_information}".lower()
        self.assertNotIn("no estate exists", blob)
        self.assertNotIn("never existed", blob)
        # It must say the opposite: both possibilities stay open.
        self.assertIn("does not carry every older filing", blob)
        self.assertIn("may never have been opened", blob)

    def test_it_recommends_no_legal_action(self):
        r = route(estate_public_index_searched=True)
        blob = " ".join([r.why, r.required_input, r.expected_information]).lower()
        for forbidden in ("petition", "file a ", "should file", "attorney should",
                          "sue", "quiet title", "you must"):
            self.assertNotIn(forbidden, blob)

    def test_a_living_identity_still_wins_over_the_index_result(self):
        """Someone already named beats any estate lookup, searched or not."""
        r = route(has_living_identity=True, estate_public_index_searched=True)
        self.assertEqual(r.queue, Q_ESTATE_ID_FOUND)

    def test_a_high_phone_still_short_circuits_everything(self):
        r = route(has_high_phone=True, estate_public_index_searched=True)
        self.assertEqual(r.next_source, "NONE_FREE")

    def test_the_new_queue_has_operator_facing_words(self):
        self.assertIn(Q_ESTATE_NO_PUBLIC_CASE, QUEUE_CONTACT_STATUS)
        self.assertEqual(QUEUE_CONTACT_STATUS[Q_ESTATE_NO_PUBLIC_CASE],
                         "NO PUBLIC ESTATE CASE FOUND")

    def test_the_searched_route_is_not_blocked(self):
        """It is a different route, not a wall. A blocking_reason would send it
        back to the 'stuck' pile it just earned its way out of."""
        r = route(estate_public_index_searched=True)
        self.assertEqual(r.blocking_reason, "")

    def test_default_is_unchanged_for_every_existing_caller(self):
        """No caller that never heard of T30A sees a different route."""
        self.assertEqual(route().to_dict(),
                         route(estate_public_index_searched=False).to_dict())


class StaleBlockingReasonCorrected(unittest.TestCase):
    def test_it_no_longer_says_the_portal_refuses_browsers(self):
        """T30A drove the Portal in a real browser. The old sentence implied the
        source was unreachable; what is true is that it is bot-protected."""
        r = route()
        self.assertIn("human-verification", r.blocking_reason)
        self.assertIn("browser", r.blocking_reason)
        self.assertNotIn("both refuse", r.blocking_reason)


if __name__ == "__main__":
    unittest.main()

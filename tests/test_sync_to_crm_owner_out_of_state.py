"""owner_out_of_state is property-relative, never compared against a hard-coded state.

The projection used `bool(st) and st != "IN"` — every NC owner mailing inside NC and every
MI owner mailing inside MI was projected out-of-state (T8 2026-09-15: 6,965 NC + 479 MI).
"""
import inspect
import unittest

from scripts import sync_to_crm as s


class OwnerOutOfStateTests(unittest.TestCase):
    def test_in_state_owners_are_not_out_of_state_in_every_market(self):
        self.assertIs(s.owner_out_of_state("NC", "37179"), False)   # Union NC (the tracer)
        self.assertIs(s.owner_out_of_state("MI", "26021"), False)   # Berrien MI
        self.assertIs(s.owner_out_of_state("IN", "18163"), False)   # Vanderburgh IN
        self.assertIs(s.owner_out_of_state(" nc ", "37179"), False)  # case / whitespace

    def test_owners_mailing_from_another_state_are_out_of_state(self):
        self.assertIs(s.owner_out_of_state("FL", "37025"), True)
        self.assertIs(s.owner_out_of_state("IN", "26021"), True)
        self.assertIs(s.owner_out_of_state("NC", "18163"), True)

    def test_unknown_when_either_state_is_missing_never_a_guess(self):
        self.assertIsNone(s.owner_out_of_state(None, "37179"))
        self.assertIsNone(s.owner_out_of_state("", "37179"))
        self.assertIsNone(s.owner_out_of_state("NC", None))
        self.assertIsNone(s.owner_out_of_state("NC", "99001"))      # no such state FIPS

    def test_property_state_from_fips(self):
        self.assertEqual(s.property_state_from_fips("37179"), "NC")
        self.assertEqual(s.property_state_from_fips("26021"), "MI")
        self.assertEqual(s.property_state_from_fips("48491"), "TX")
        self.assertIsNone(s.property_state_from_fips(""))

    def test_the_projection_uses_the_property_relative_rule(self):
        src = inspect.getsource(s.main)
        self.assertIn("owner_out_of_state(", src)
        self.assertNotRegex(src, r"!=\s*[\"']IN[\"']")


if __name__ == "__main__":
    unittest.main()

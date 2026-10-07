from __future__ import annotations

import unittest

from scripts.build_nc_dps_public_enrichment_v2 import query_variants


class PublicEnrichmentV2Tests(unittest.TestCase):
    def test_union_parcel_tries_raw_and_normalized_forms(self):
        values = query_variants("02.239.077")
        self.assertIn("02.239.077", values)
        self.assertIn("02239077", values)

    def test_excel_wrapped_parcel_is_cleaned(self):
        values = query_variants('="10000057"')
        self.assertEqual(values, {"10000057"})

    def test_pseudo_ids_are_not_sent_to_onemap(self):
        self.assertEqual(query_variants("ACCOUNT:123"), set())
        self.assertEqual(query_variants("ADDR:abc"), set())


if __name__ == "__main__":
    unittest.main(verbosity=2)

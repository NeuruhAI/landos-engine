from __future__ import annotations

import unittest

from scripts.build_nc_dps_public_enrichment import (
    aggregate,
    build_mecklenburg_signals,
    norm_parcel,
)


class PublicEnrichmentTests(unittest.TestCase):
    def test_parcel_normalization_preserves_real_identity(self):
        self.assertEqual(norm_parcel('02.239.077'), '02239077')
        self.assertEqual(norm_parcel('="10000057"'), '10000057')
        self.assertEqual(norm_parcel('ACCOUNT:123'), 'ACCOUNT:123')

    def test_onemap_context_enriches_without_faking_equity(self):
        signal = {
            'signal_id': 'a' * 64,
            'source_type': 'taxdelinquent',
            'geography': '37057',
            'parcel_apn': '01.001',
            'owner_name': 'OLD OWNER',
            'raw_address': '',
            'detail_json': {'tax_balance': 2500.0, 'source_url': 'https://county.example'},
        }
        one = {
            'parno': '01.001',
            'ownname': 'CURRENT OWNER',
            'parval': 225000,
            'mailadd': '1 MAIN ST',
            'mcity': 'CHICAGO',
            'mstate': 'IL',
            'mzip': '60601',
            'siteadd': '10 OAK ST',
            'saledatetx': '01/01/2000',
        }
        rows = aggregate([signal], {('37057', '01001'): one})
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row['out_of_state'])
        self.assertGreaterEqual(row['years_owned'], 20)
        self.assertEqual(row['assessed_value'], 225000.0)
        self.assertEqual(row['estimated_equity'], '')
        self.assertEqual(row['equity_status'], 'UNKNOWN_NEEDS_DEBT_SEARCH')
        self.assertFalse(row['contact_authorized'])

    def test_mecklenburg_requires_unique_owner_property_match(self):
        staged = [{
            'staging_id': 'x',
            'owner_name': 'SMITH JOHN',
            'tax_balance': 1000,
            'source_url': 'https://meck.example',
            'source_label': 'Delinquent Individual Taxpayers',
            'source_as_of': '2026-08-04',
        }]
        matches = {'SMITHJOHN': [{'parno': 'P-1', 'ownname': 'SMITH JOHN', 'siteadd': '1 TEST ST'}]}
        signals, unresolved = build_mecklenburg_signals(staged, matches)
        self.assertEqual(len(signals), 1)
        self.assertEqual(unresolved, [])
        self.assertEqual(signals[0]['parcel_apn'], 'P-1')
        self.assertFalse(signals[0]['detail_json']['contact_authorized'])

        matches['SMITHJOHN'].append({'parno': 'P-2', 'ownname': 'SMITH JOHN'})
        signals, unresolved = build_mecklenburg_signals(staged, matches)
        self.assertEqual(signals, [])
        self.assertEqual(unresolved[0]['candidate_parcel_count'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)

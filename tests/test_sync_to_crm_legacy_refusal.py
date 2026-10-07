"""sync_to_crm must not remain a silent second CRM/contact writer on the legacy spine.

DeedSonar's CRM lives in efxowqudmsfqjmbeidjc; the legacy CRM on lvdi is retired.
A WRITE run against lvdi is refused before any connection is opened; a dry run
(read-only) is still allowed.
"""
import unittest
from unittest import mock

from scripts import sync_to_crm

LVDI = "postgresql://postgres.lvdimujkfheeyqchjzra:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
EFXO = "postgresql://postgres.efxowqudmsfqjmbeidjc:pw@aws-0-us-east-2.pooler.supabase.com:5432/postgres"


class SyncToCrmLegacyRefusalTests(unittest.TestCase):
    def test_write_run_on_legacy_spine_is_refused_before_connecting(self):
        with mock.patch.object(sync_to_crm, "DATABASE_URL", LVDI), \
             mock.patch.object(sync_to_crm.psycopg, "connect") as connect:
            with self.assertRaises(sync_to_crm.LegacyCrmWriteRefused) as ctx:
                sync_to_crm.main(dry_run=False)
            connect.assert_not_called()
        self.assertIn("second CRM writer", str(ctx.exception))

    def test_refusal_is_a_nonzero_exit(self):
        with self.assertRaises(SystemExit) as ctx:
            sync_to_crm.refuse_legacy_crm_write(LVDI, dry_run=False)
        self.assertTrue(ctx.exception.code)

    def test_dry_run_on_legacy_spine_is_still_allowed(self):
        sync_to_crm.refuse_legacy_crm_write(LVDI, dry_run=True)

    def test_non_legacy_dsn_is_not_refused_by_this_guard(self):
        sync_to_crm.refuse_legacy_crm_write(EFXO, dry_run=False)
        sync_to_crm.refuse_legacy_crm_write("postgresql://u:x@127.0.0.1:5432/t", dry_run=False)

    def test_the_dsn_password_is_not_in_the_message(self):
        with self.assertRaises(SystemExit) as ctx:
            sync_to_crm.refuse_legacy_crm_write(LVDI, dry_run=False)
        self.assertNotIn(":pw@", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()

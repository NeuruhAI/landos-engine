"""scripts/backup_deedsonar_production.sh: production backup authority.

Production is efxowqudmsfqjmbeidjc. The legacy spine lvdimujkfheeyqchjzra must never
stand in for it. The script must fail loudly rather than write to the internal disk,
and it must never install an unverified dump.

pg_dump / pg_restore are replaced by stubs, so no database is touched.
"""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backup_deedsonar_production.sh"
EFXO = "postgresql://postgres.efxowqudmsfqjmbeidjc:pw@aws-0-us-east-2.pooler.supabase.com:5432/postgres"
LVDI = "postgresql://postgres.lvdimujkfheeyqchjzra:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
GOOD_TOC = (
    "10; 0 1 TABLE DATA public lead_import_rows postgres\n"
    "11; 0 1 TABLE DATA supabase_migrations schema_migrations postgres\n"
    "12; 0 1 TABLE DATA public contact_evidence postgres\n"
)


def _exe(path: Path, body: str) -> Path:
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class BackupDeedsonarProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.volume = self.tmp / "NEURUH-HD"
        self.volume.mkdir()
        self.out = self.volume / "NEURUH_BACKUPS" / "deedsonar-efxo"
        self.calls = self.tmp / "pg_dump.calls"
        # the stub pg_dump records its call and writes a fake dump to --file=<path>
        self.pg_dump = _exe(
            self.tmp / "pg_dump",
            f'echo "$@" >> "{self.calls}"\n'
            'for a in "$@"; do case "$a" in --file=*) printf dump > "${a#--file=}";; esac; done\n',
        )
        self.toc = self.tmp / "toc.txt"
        self.toc.write_text(GOOD_TOC)
        self.pg_restore = _exe(self.tmp / "pg_restore", f'cat "{self.toc}"\n')

    def run_script(self, env_lines, volume=None):
        env_file = self.tmp / ".env"
        env_file.write_text("\n".join(env_lines) + "\n")
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.tmp),
            "BACKUP_ENV_FILE": str(env_file),
            "DEEDSONAR_BACKUP_VOLUME": str(volume or self.volume),
            "PG_DUMP_BIN": str(self.pg_dump),
            "PG_RESTORE_BIN": str(self.pg_restore),
        }
        return subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    def dumps(self):
        return sorted(p.name for p in self.out.glob("*")) if self.out.exists() else []

    def test_efxo_backup_is_written_verified_and_sidecar_sha(self):
        r = self.run_script([f"DEEDSONAR_DATABASE_URL={EFXO}"])
        self.assertEqual(r.returncode, 0, r.stderr)
        names = self.dumps()
        self.assertEqual(len([n for n in names if n.endswith(".dump")]), 1, names)
        self.assertEqual(len([n for n in names if n.endswith(".dump.sha256")]), 1, names)
        self.assertFalse([n for n in names if n.endswith(".partial")])
        self.assertIn("--format=custom", self.calls.read_text())

    def test_the_dsn_is_never_printed(self):
        r = self.run_script([f"DEEDSONAR_DATABASE_URL={EFXO}"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn(":pw@", r.stdout + r.stderr)

    def test_legacy_spine_is_refused_and_pg_dump_never_runs(self):
        r = self.run_script([f"DEEDSONAR_DATABASE_URL={LVDI}"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("legacy", r.stderr)
        self.assertFalse(self.calls.exists())

    def test_no_fallback_to_database_url(self):
        r = self.run_script([f"DATABASE_URL={EFXO}"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no DATABASE_URL fallback", r.stderr)
        self.assertFalse(self.calls.exists())

    def test_an_unproven_target_is_refused(self):
        r = self.run_script(["DEEDSONAR_DATABASE_URL=postgresql://u:pw@10.0.0.5:5432/postgres"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("does not name production", r.stderr)
        self.assertFalse(self.calls.exists())

    def test_missing_cold_volume_fails_loudly_and_never_uses_internal_disk(self):
        missing = self.tmp / "NOT-MOUNTED"
        r = self.run_script([f"DEEDSONAR_DATABASE_URL={EFXO}"], volume=missing)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("backup volume not present", r.stderr)
        self.assertFalse(missing.exists())
        self.assertFalse(self.calls.exists())

    def test_a_real_sized_toc_is_verified(self):
        # A production TOC runs to thousands of lines. Under `set -o pipefail`, piping it
        # into `grep -q` lets grep exit on the first match; printf then dies of SIGPIPE,
        # the pipe "fails", and a good dump is refused and deleted (2026-09-12, 0/3 markers
        # on a real efxo dump). The markers sit at the top here so grep exits early.
        filler = "".join(f"{n}; 2615 1 TABLE public t{n} postgres\n" for n in range(100, 60100))
        self.toc.write_text(GOOD_TOC + filler)
        r = self.run_script([f"DEEDSONAR_DATABASE_URL={EFXO}"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len([n for n in self.dumps() if n.endswith(".dump")]), 1, self.dumps())

    def test_a_hollow_dump_is_never_installed(self):
        self.toc.write_text("10; 0 1 TABLE DATA public some_other_table postgres\n")
        r = self.run_script([f"DEEDSONAR_DATABASE_URL={EFXO}"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("missing expected production markers", r.stderr)
        self.assertEqual(self.dumps(), [])

    def test_a_failed_dump_leaves_no_partial(self):
        _exe(self.pg_dump, 'for a in "$@"; do case "$a" in --file=*) printf x > "${a#--file=}";; esac; done\nexit 1\n')
        r = self.run_script([f"DEEDSONAR_DATABASE_URL={EFXO}"])
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.dumps(), [])


if __name__ == "__main__":
    unittest.main()

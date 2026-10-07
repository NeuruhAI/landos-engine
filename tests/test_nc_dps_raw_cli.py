from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class RawCliTests(unittest.TestCase):
    def test_dry_run_needs_no_database_url_or_psycopg(self):
        repo = Path(__file__).resolve().parents[1]
        signal = {
            "signal_id": "a" * 64,
            "source": "official_tax_distress:orange",
            "source_type": "taxdelinquent",
            "geography": "37135",
            "parcel_apn": "TEST-1",
            "owner_name": "EXAMPLE OWNER",
            "detail_json": {
                "observation_kind": "published_tax_distress",
                "source_url": "https://official.example/tax-liens",
            },
            "confidence": 0.9,
        }

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "signals.jsonl"
            path.write_text(json.dumps(signal) + "\n", encoding="utf-8")
            env = dict(os.environ)
            env.pop("DATABASE_URL", None)
            proc = subprocess.run(
                [sys.executable, "scripts/mint_nc_dps_raw_opportunities.py", "--signals", str(path)],
                cwd=repo,
                env=env,
                text=True,
                capture_output=True,
            )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DRY RUN", proc.stdout)
        self.assertIn("NC:37135:TEST-1", proc.stdout)
        self.assertNotIn("DATABASE_URL", proc.stderr)
        self.assertNotIn("psycopg", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

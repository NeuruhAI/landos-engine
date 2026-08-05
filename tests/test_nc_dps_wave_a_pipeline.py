from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.nc_dps_wave_a_pipeline import build

ROOT = Path(__file__).resolve().parents[1]


class WaveAEngineTests(unittest.TestCase):
    def test_builds_ranked_property_and_projections(self):
        signals = [
            {
                "signal_id": "a" * 64,
                "source": "official_parcel_record:orange",
                "source_type": "title_instrument",
                "geography": "37135",
                "event_date": "",
                "raw_address": "1 MAIN ST",
                "parcel_apn": "123-456",
                "owner_name": "EXAMPLE OWNER",
                "detail_json": {
                    "source_url": "https://official.example/parcel",
                    "out_of_state": True,
                    "mailing_address": "9 OTHER ST, RICHMOND, VA 23220",
                    "assessed_value": 250000,
                },
                "confidence": 0.92,
                "fetched_at": "2026-08-05T00:00:00+00:00",
            },
            {
                "signal_id": "b" * 64,
                "source": "official_tax_distress:orange",
                "source_type": "taxdelinquent",
                "geography": "37135",
                "event_date": "2026-03-14",
                "raw_address": "1 MAIN ST",
                "parcel_apn": "123-456",
                "owner_name": "EXAMPLE OWNER",
                "detail_json": {"source_url": "https://official.example/tax.xlsx", "tax_balance": 5000, "tax_year": 2025},
                "confidence": 0.9,
                "fetched_at": "2026-08-05T00:00:00+00:00",
            },
        ]
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "signals.jsonl"
            source.write_text("\n".join(json.dumps(r) for r in signals) + "\n", encoding="utf-8")
            out = root / "out"
            summary = build(source, out, ROOT / "contracts/nc_dps_property_packet.v1.schema.json")
            self.assertEqual(summary["property_packets"], 1)
            self.assertEqual(summary["crm_projections"], 1)
            packet = json.loads((out / "property_packets.jsonl").read_text().splitlines()[0])
            self.assertIn("tax_distress", packet["scores"]["trigger_classes"])
            self.assertIn("absentee_or_out_of_state", packet["scores"]["trigger_classes"])
            self.assertFalse(any(packet["governance"].values()))


if __name__ == "__main__":
    unittest.main(verbosity=2)

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.nc_dps_wave_a_pipeline import build

ROOT = Path(__file__).resolve().parents[1]


def row(signal_id, source_type, source_url, *, assessed=None, out_of_state=False):
    detail = {"source_url": source_url}
    if source_type == "taxdelinquent":
        detail.update({"tax_balance": 5000, "tax_year": 2025})
    else:
        detail.update({
            "assessed_value": assessed,
            "out_of_state": out_of_state,
            "mailing_address": "9 OTHER ST, RICHMOND, VA",
        })
    return {
        "signal_id": signal_id * 64,
        "source": source_type,
        "source_type": source_type,
        "geography": "37135",
        "event_date": "2026-03-14",
        "raw_address": "1 MAIN ST",
        "parcel_apn": "123-456",
        "owner_name": "EXAMPLE OWNER",
        "detail_json": detail,
        "confidence": 0.9,
        "fetched_at": "2026-08-05T00:00:00+00:00",
    }


class WaveAEngineTests(unittest.TestCase):
    def execute(self, signals):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        source = root / "signals.jsonl"
        source.write_text("\n".join(json.dumps(item) for item in signals) + "\n")
        output = root / "out"
        summary = build(source, output, ROOT / "contracts/nc_dps_property_packet.v1.schema.json")
        return summary, output

    def test_single_source_distress_stays_in_enrichment(self):
        summary, output = self.execute([
            row("a", "taxdelinquent", "https://official.example/delinquent.csv")
        ])
        self.assertEqual(summary["property_packets"], 1)
        self.assertEqual(summary["enrichment_backlog"], 1)
        self.assertEqual(summary["crm_projections"], 0)
        receipt = json.loads((output / "score_receipts.jsonl").read_text().splitlines()[0])
        self.assertFalse(receipt["corroborated"])
        self.assertEqual(receipt["queue"], "enrichment_backlog")

    def test_corroborated_high_value_absentee_can_enter_crm_research(self):
        summary, output = self.execute([
            row("a", "taxdelinquent", "https://official.example/delinquent.csv"),
            row("b", "title_instrument", "https://official.example/parcel", assessed=300000, out_of_state=True),
        ])
        self.assertEqual(summary["property_packets"], 1)
        self.assertEqual(summary["research_queue"], 1)
        self.assertEqual(summary["crm_projections"], 1)
        receipt = json.loads((output / "score_receipts.jsonl").read_text().splitlines()[0])
        self.assertTrue(receipt["corroborated"])
        self.assertEqual(receipt["queue"], "crm_research")


if __name__ == "__main__":
    unittest.main(verbosity=2)

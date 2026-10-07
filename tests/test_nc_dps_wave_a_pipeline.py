from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.nc_dps_wave_a_pipeline import build


def schema(path: Path) -> Path:
    payload = {
        "properties": {
            "governance": {
                "properties": {
                    key: {"const": False}
                    for key in [
                        "legal_heir_declared",
                        "title_clear_declared",
                        "seller_contact_authorized",
                        "skip_trace_authorized",
                        "offer_authorized",
                        "funds_authorized",
                        "filing_authorized",
                        "recording_authorized",
                    ]
                }
            }
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def context():
    return {
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
            "qualification_status": "supporting_context",
            "out_of_state": True,
            "mailing_address": "9 OTHER ST, RICHMOND, VA 23220",
            "assessed_value": 250000,
        },
        "confidence": 0.92,
        "fetched_at": "2026-08-05T00:00:00+00:00",
    }


def distress():
    return {
        "signal_id": "b" * 64,
        "source": "official_tax_distress:orange",
        "source_type": "taxdelinquent",
        "geography": "37135",
        "event_date": "2026-03-14",
        "raw_address": "1 MAIN ST",
        "parcel_apn": "123-456",
        "owner_name": "EXAMPLE OWNER",
        "detail_json": {
            "source_url": "https://orangecountync.gov/Orange-County-NC-Tax-Liens.xlsx",
            "qualification_status": "credible_distress",
            "tax_balance": 5000,
            "tax_year": 2025,
        },
        "confidence": 0.9,
        "fetched_at": "2026-08-05T00:00:00+00:00",
    }


class WaveAEngineTests(unittest.TestCase):
    def _run(self, signals):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        root = Path(td.name)
        source = root / "signals.jsonl"
        source.write_text(
            "\n".join(json.dumps(row) for row in signals) + "\n",
            encoding="utf-8",
        )
        out = root / "out"
        summary = build(source, out, schema(root / "schema.json"))
        return summary, out

    def test_unqualified_raw_signal_is_refused(self):
        raw = context()
        raw["detail_json"].pop("qualification_status")
        with self.assertRaisesRegex(ValueError, "refuses unqualified raw signals"):
            self._run([raw])

    def test_qualified_distress_with_context_can_reach_crm_research_queue(self):
        summary, out = self._run([context(), distress()])
        self.assertEqual(summary["property_packets"], 1)
        self.assertEqual(summary["crm_projections"], 1)
        self.assertEqual(summary["research_backlog_properties"], 0)
        crm = json.loads((out / "crm_projection.jsonl").read_text().splitlines()[0])
        self.assertFalse(crm["write_authorized"])

    def test_distress_without_identity_context_stays_out_of_crm(self):
        summary, out = self._run([distress()])
        self.assertEqual(summary["crm_projections"], 0)
        self.assertEqual(summary["research_backlog_properties"], 1)
        self.assertEqual((out / "crm_projection.jsonl").read_text(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)

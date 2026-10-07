from __future__ import annotations

import unittest

from app.nc_dps_writer import build_candidates


def signal(status, source_type, sid, parcel="123-456"):
    return {
        "signal_id": sid * 64,
        "source_type": source_type,
        "geography": "37135",
        "parcel_apn": parcel,
        "owner_name": "EXAMPLE OWNER",
        "detail_json": {
            "qualification_status": status,
            "source_url": "https://official.example/record",
        },
        "confidence": 0.9,
    }


def packet():
    return {
        "schema_version": "nc_dps_property_packet.v1",
        "property_id": "NC:37135:123-456",
        "county_fips": "37135",
        "parcel_id": "123-456",
        "identity": {
            "owner_of_record": "EXAMPLE OWNER",
            "property_address": "1 MAIN ST",
            "owner_mailing_address": "9 OTHER ST, RICHMOND, VA 23220",
        },
        "valuation": {"tax_balance": 5000},
        "ownership": {"absentee": True, "out_of_state": True},
        "scores": {
            "composite_priority": 61.2,
            "opportunity_score": 60.0,
            "urgency_score": 55.0,
            "evidence_confidence": 82.0,
            "trigger_classes": ["tax_distress", "absentee_or_out_of_state"],
        },
        "recipes": {"recommended_recipe": "tax_distress_absentee_owner"},
        "source_records": [{"source_id": "t" * 64, "source_url": "https://official.example/record"}],
    }


def receipt(crm_ready=True):
    return {
        "property_id": "NC:37135:123-456",
        "route": "automated_enrichment_then_analyst",
        "receipt_sha256": "a" * 64,
        "qualification": {
            "credible_distress_signal_count": 1,
            "credible_distress_types": ["taxdelinquent"],
            "supporting_context_present": True,
            "corroborated": False,
            "crm_ready": crm_ready,
        },
    }


class NCDPSWriterTests(unittest.TestCase):
    def test_crm_ready_property_maps_to_existing_discovered_writer_shape(self):
        signals = [
            signal("credible_distress", "taxdelinquent", "t"),
            signal("supporting_context", "title_instrument", "c"),
        ]
        candidates, summary = build_candidates(signals, [packet()], [receipt(True)])
        self.assertEqual(summary["crm_ready_properties"], 1)
        self.assertEqual(len(candidates), 1)
        row = candidates[0]
        self.assertEqual(row["provenance"], "discovered_nc_dps")
        self.assertIn("nc_dps:tax_distress", row["raw_meta"]["signal_tags"])
        self.assertFalse(summary["contact_authorized"])
        self.assertFalse(summary["execution_authorized"])

    def test_non_crm_ready_property_cannot_reach_owner_leads(self):
        signals = [
            signal("credible_distress", "taxdelinquent", "t"),
            signal("supporting_context", "title_instrument", "c"),
        ]
        candidates, summary = build_candidates(signals, [packet()], [receipt(False)])
        self.assertEqual(candidates, [])
        self.assertEqual(summary["blocked_properties"], 1)

    def test_unqualified_signal_is_rejected(self):
        signals = [signal("", "taxdelinquent", "t")]
        with self.assertRaisesRegex(ValueError, "refuses unqualified input"):
            build_candidates(signals, [packet()], [receipt(True)])


if __name__ == "__main__":
    unittest.main(verbosity=2)

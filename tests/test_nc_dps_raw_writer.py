from __future__ import annotations

import unittest

from app.nc_dps_raw_writer import build_raw_candidates


def signal(source_type: str, *, parcel: str = "1", detail: dict | None = None):
    return {
        "signal_id": (source_type + parcel).encode().hex().ljust(64, "0")[:64],
        "source": "official",
        "source_type": source_type,
        "geography": "37135",
        "parcel_apn": parcel,
        "owner_name": "EXAMPLE OWNER",
        "raw_address": "101 MAIN ST",
        "detail_json": {"source_url": "https://official.example/source", **(detail or {})},
        "confidence": 0.7,
    }


class RawWriterTests(unittest.TestCase):
    def test_single_tax_signal_can_enter_raw_pool(self):
        candidates, summary = build_raw_candidates([signal("taxdelinquent")])
        self.assertEqual(len(candidates), 1)
        self.assertEqual(summary["raw_opportunity_properties"], 1)
        self.assertFalse(summary["score_threshold_required"])
        self.assertFalse(summary["qualification_required_for_intake"])
        self.assertEqual(candidates[0]["emerging_score"], 0.0)
        self.assertEqual(candidates[0]["provenance"], "discovered_nc_dps_raw")
        self.assertFalse(candidates[0]["raw_meta"]["contact_authorized"])

    def test_context_and_distress_collapse_to_one_property(self):
        candidates, _ = build_raw_candidates([
            signal("title_instrument", detail={"out_of_state": True, "mailing_address": "9 OAK ST, CHICAGO, IL"}),
            signal("taxdelinquent", detail={"tax_balance": 5200.0, "tax_year": 2024}),
        ])
        self.assertEqual(len(candidates), 1)
        row = candidates[0]
        self.assertTrue(row["absentee_flag"])
        self.assertEqual(row["raw_meta"]["amount_due"], 5200.0)
        self.assertIn("nc_dps:raw_opportunity", row["raw_meta"]["signal_tags"])

    def test_missing_parcel_does_not_enter_pool(self):
        candidates, summary = build_raw_candidates([signal("taxdelinquent", parcel="")])
        self.assertEqual(candidates, [])
        self.assertEqual(summary["raw_opportunity_properties"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


def test_operator_ranked_metadata_rides_into_emerging_signals():
    from app.nc_dps_raw_writer import build_raw_candidates

    signal = {
        "signal_id": "s1",
        "source": "operator_raw_upload:t4",
        "source_type": "operator_raw_opportunity",
        "geography": "37179",
        "parcel_apn": "09222012",
        "owner_name": "CHAMBERS EDNA MAE HEIRS",
        "raw_address": "203 E EAST AVE",
        "confidence": 0.7,
        "fetched_at": "2026-08-12T00:00:00Z",
        "detail_json": {
            "raw": {
                "recipe_id": "stalled_tax_foreclosure_heirship",
                "curative_priority_score": "93",
                "priority_tier": "A",
                "trigger_classes": "tax_distress|court_action|heir_or_estate",
                "scoring_version": "nc_curative_rank.v3",
                "recipe_version": "nc_curative_recipes.v1",
            },
        },
    }
    # operator_raw_opportunity is enabled by the v2 minter; emulate that here.
    import app.nc_dps_raw_writer as w

    original = w.RAW_OPPORTUNITY_TYPES
    try:
        w.RAW_OPPORTUNITY_TYPES = original | {"operator_raw_opportunity"}
        candidates, summary = build_raw_candidates([signal])
    finally:
        w.RAW_OPPORTUNITY_TYPES = original

    assert len(candidates) == 1
    nc = candidates[0]["emerging_signals"]["nc_dps"]
    assert nc["recommended_recipe"] == "stalled_tax_foreclosure_heirship"
    assert nc["curative_priority_score"] == 93.0
    assert nc["priority_tier"] == "A"
    assert nc["scoring_version"] == "nc_curative_rank.v3"
    assert nc["recipe_version"] == "nc_curative_recipes.v1"
    assert nc["contact_authorized"] is False


from __future__ import annotations

import ast
import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app/nc_dps_contract.py"
PROPERTY_SCHEMA_PATH = (
    ROOT / "contracts/nc_dps_property_packet.v1.schema.json"
)
SCORE_SCHEMA_PATH = (
    ROOT / "contracts/nc_dps_score_receipt.v1.schema.json"
)
WRITER_MAP_PATH = ROOT / "contracts/nc_dps_single_writer.v1.json"


def load_module():
    spec = importlib.util.spec_from_file_location(
        "nc_dps_contract_test_module",
        MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load nc_dps_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class NCDPSEngineContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.property_schema = json.loads(PROPERTY_SCHEMA_PATH.read_text())
        cls.score_schema = json.loads(SCORE_SCHEMA_PATH.read_text())
        cls.writer_map = json.loads(WRITER_MAP_PATH.read_text())

    def test_trigger_classes_are_exact_and_unique(self):
        self.assertEqual(len(self.module.TRIGGER_CLASSES), 10)
        self.assertEqual(
            len(set(self.module.TRIGGER_CLASSES)),
            10,
        )
        self.assertIn(
            "ownership_fragmentation",
            self.module.TRIGGER_CLASSES,
        )
        self.assertIn(
            "recorded_lien_or_judgment",
            self.module.TRIGGER_CLASSES,
        )

    def test_opportunity_lanes_sum_to_one_hundred(self):
        self.assertEqual(
            sum(self.module.OPPORTUNITY_LANE_CAPS.values()),
            100.0,
        )

    def test_priority_weights_sum_to_one(self):
        self.assertAlmostEqual(
            sum(self.module.PRIORITY_WEIGHTS.values()),
            1.0,
        )

    def test_raw_dps_points_preserve_handoff_values(self):
        signals = {
            "tax_lawsuit_over_365_days",
            "confirmed_deceased_owner",
            "equity_above_150k",
            "out_of_state_owner",
        }

        self.assertEqual(
            self.module.raw_dps_score(signals),
            65,
        )

        penalties = {
            "equity_below_50k",
            "active_bankruptcy",
        }

        self.assertEqual(
            self.module.raw_dps_score(penalties),
            -45,
        )

    def test_unknown_raw_signal_fails_loudly(self):
        with self.assertRaises(ValueError):
            self.module.raw_dps_score(
                {"invented_signal"},
            )

    def test_composite_priority_is_deterministic(self):
        self.assertEqual(
            self.module.composite_priority(
                opportunity_score=80,
                urgency_score=50,
                evidence_confidence=90,
            ),
            75.0,
        )

    def test_routing_thresholds(self):
        self.assertEqual(
            self.module.route_priority(90),
            "immediate_senior_review",
        )
        self.assertEqual(
            self.module.route_priority(75),
            "full_ai_dps_analysis",
        )
        self.assertEqual(
            self.module.route_priority(60),
            "automated_enrichment_then_analyst",
        )
        self.assertEqual(
            self.module.route_priority(40),
            "monitor_or_nurture",
        )
        self.assertEqual(
            self.module.route_priority(20),
            "archive_until_new_signal",
        )

    def test_lane_values_cannot_exceed_caps(self):
        valid = {
            "equity_quality": 30,
            "tax_and_court_distress": 20,
            "title_curative_opportunity": 20,
            "owner_detachment": 8,
            "market_liquidity": 4,
        }

        self.assertEqual(
            self.module.validate_opportunity_lanes(valid),
            82.0,
        )

        invalid = dict(valid)
        invalid["equity_quality"] = 36

        with self.assertRaises(ValueError):
            self.module.validate_opportunity_lanes(invalid)


    def test_property_schema_pins_trigger_vocabulary(self):
        actual = set(
            self.property_schema["properties"]["scores"]["properties"][
                "trigger_classes"
            ]["items"]["enum"]
        )

        self.assertEqual(
            actual,
            set(self.module.TRIGGER_CLASSES),
        )

    def test_property_schema_requires_five_next_steps(self):
        next_steps = self.property_schema["properties"][
            "next_research_steps"
        ]

        self.assertEqual(next_steps["minItems"], 5)
        self.assertEqual(next_steps["maxItems"], 5)

    def test_property_schema_hard_stops_are_false(self):
        governance = self.property_schema["properties"][
            "governance"
        ]["properties"]

        for field, rule in governance.items():
            self.assertIs(
                rule["const"],
                False,
                field,
            )

    def test_score_receipt_requires_chain_hash(self):
        required = set(self.score_schema["required"])

        self.assertIn("section_hashes", required)
        self.assertIn("chain_sha256", required)
        self.assertIn("input_sha256", required)
        self.assertIn("weights_sha256", required)

    def test_single_writer_boundaries(self):
        domains = self.writer_map["domains"]

        self.assertEqual(
            domains["source_acquisition"]["writer"],
            "NeuruhAI/landos-ingest",
        )
        self.assertEqual(
            domains["dps_scoring"]["writer"],
            "NeuruhAI/landos-engine",
        )
        self.assertEqual(
            domains["strategy_comparison"]["writer"],
            "NeuruhAI/recipe-engine",
        )
        self.assertEqual(
            domains["legal_ownership_determination"]["writer"],
            "Human counsel",
        )

    def test_contract_module_is_pure(self):
        tree = ast.parse(MODULE_PATH.read_text())
        forbidden = {
            "psycopg",
            "requests",
            "httpx",
            "urllib",
            "socket",
            "subprocess",
        }

        found = set()

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(
                    alias.name.split(".")[0]
                    for alias in node.names
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])

        self.assertFalse(
            found & forbidden,
            f"forbidden imports: {sorted(found & forbidden)}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

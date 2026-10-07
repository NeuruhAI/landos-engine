import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import build_nc_dps_public_enrichment_v3 as v3


def _write_csv(path: Path, rows: list[dict]) -> None:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


ROW_MECK = {
    "county_fips": "37119",
    "parcel_apn": "06724316",
    "owner_name": "DOE JOHN",
    "parcel_match_status": "ONEMAP_MATCHED",
    "out_of_state": "True",
    "years_owned": "21",
    "heir_indicator": "False",
    "fragmented_ownership_indicator": "False",
    "tax_foreclosure_indicator": "False",
}
ROW_UNION = dict(ROW_MECK, county_fips="37179", parcel_apn="09125043")


class V3FreshMecklenburgTests(unittest.TestCase):
    def test_taxpayers_path_is_passed_through_and_meck_rows_are_kept(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            taxpayers = Path(tmp) / "meck.jsonl"
            taxpayers.write_text("", encoding="utf-8")
            captured = {}

            def fake_v2_run(signals_path, meck_path, fresh_dir):
                captured["meck_path"] = meck_path
                _write_csv(
                    Path(fresh_dir) / "LANDOS_ENRICHED_OPPORTUNITIES.csv",
                    [ROW_MECK, ROW_UNION],
                )
                return {
                    "source_signals": 2,
                    "mecklenburg_staging_rows": 10,
                    "mecklenburg_unique_property_matches": 1,
                    "source_errors": [],
                }

            with mock.patch.object(v3.v2, "run", side_effect=fake_v2_run):
                summary = v3.run(Path(tmp) / "signals.jsonl", out, None, taxpayers)

            self.assertEqual(captured["meck_path"], taxpayers)
            self.assertEqual(summary["enriched_properties"], 2)
            self.assertEqual(summary["fresh_mecklenburg_property_matches"], 1)
            self.assertEqual(summary["reused_mecklenburg_properties"], 0)
            combined = (out / "LANDOS_ENRICHED_OPPORTUNITIES.csv").read_text(encoding="utf-8")
            self.assertIn("37119", combined)
            self.assertIn("37179", combined)

    def test_without_taxpayers_fresh_meck_rows_are_still_replaced_by_reuse_csv(self):
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            reuse = Path(tmp) / "reuse.csv"
            _write_csv(reuse, [dict(ROW_MECK, owner_name="SMITH ESTATE OF ALICE")])

            def fake_v2_run(signals_path, meck_path, fresh_dir):
                self.assertIsNone(meck_path)
                _write_csv(
                    Path(fresh_dir) / "LANDOS_ENRICHED_OPPORTUNITIES.csv",
                    [ROW_MECK, ROW_UNION],
                )
                return {"source_signals": 2, "source_errors": []}

            with mock.patch.object(v3.v2, "run", side_effect=fake_v2_run):
                summary = v3.run(Path(tmp) / "signals.jsonl", out, reuse, None)

            self.assertEqual(summary["reused_mecklenburg_properties"], 1)
            rows = (out / "LANDOS_ENRICHED_OPPORTUNITIES.csv").read_text(encoding="utf-8")
            self.assertIn("SMITH ESTATE OF ALICE", rows)
            self.assertNotIn("DOE JOHN,", rows.split("37119")[0])
            summary_file = json.loads(
                (out / "LANDOS_ENRICHMENT_SUMMARY.json").read_text(encoding="utf-8")
            )
            self.assertEqual(summary_file["onemap_transport"], "POST")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class MecklenburgAddressCorroborationTests(unittest.TestCase):
    ROW = {
        "staging_id": "abc",
        "owner_name": "DOE JOHN",
        "tax_balance": 4580.24,
        "property_address": "127 601 N TRYON ST CHARLOTTE NC 28202",
        "source_url": "https://example.test",
        "source_label": "IND",
        "source_as_of": "2026-08-04",
    }

    def test_situs_key_handles_unit_prefix_and_directionals(self):
        self.assertEqual(v3.situs_key("127 601 N TRYON ST CHARLOTTE NC 28202"), ("601", "TRYON"))
        self.assertEqual(v3.situs_key("601 N TRYON ST"), ("601", "TRYON"))
        self.assertEqual(v3.situs_key("1069 MCALWAY RD"), ("1069", "MCALWAY"))
        self.assertIsNone(v3.situs_key(""))

    def test_unique_owner_match_with_matching_address_is_corroborated(self):
        matches = {v3.core.norm_owner("DOE JOHN"): [{"parno": "06724316", "ownname": "DOE JOHN", "siteadd": "601 N TRYON ST"}]}
        signals, report = v3.build_mecklenburg_signals_addr([dict(self.ROW)], matches)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["detail_json"]["match_basis"], "unique_owner_match_address_corroborated")
        self.assertEqual(signals[0]["confidence"], 0.88)
        self.assertEqual(report, [])

    def test_unique_owner_match_with_conflicting_address_is_refused(self):
        matches = {v3.core.norm_owner("DOE JOHN"): [{"parno": "06724316", "ownname": "DOE JOHN", "siteadd": "9 OAK AV"}]}
        signals, report = v3.build_mecklenburg_signals_addr([dict(self.ROW)], matches)
        self.assertEqual(signals, [])
        self.assertEqual(report[0]["status"], "ADDRESS_CONFLICT_ON_UNIQUE_OWNER_MATCH")

    def test_multi_parcel_owner_resolves_via_single_address_hit(self):
        matches = {v3.core.norm_owner("DOE JOHN"): [
            {"parno": "P1", "ownname": "DOE JOHN", "siteadd": "601 N TRYON ST"},
            {"parno": "P2", "ownname": "DOE JOHN", "siteadd": "9 OAK AV"},
        ]}
        signals, report = v3.build_mecklenburg_signals_addr([dict(self.ROW)], matches)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["parcel_apn"], "P1")
        self.assertEqual(signals[0]["detail_json"]["match_basis"], "owner_plus_address_match")
        self.assertEqual(signals[0]["confidence"], 0.80)

    def test_ambiguous_owner_without_address_stays_unresolved(self):
        row = dict(self.ROW, property_address=None)
        matches = {v3.core.norm_owner("DOE JOHN"): [
            {"parno": "P1", "ownname": "DOE JOHN", "siteadd": "601 N TRYON ST"},
            {"parno": "P2", "ownname": "DOE JOHN", "siteadd": "9 OAK AV"},
        ]}
        signals, report = v3.build_mecklenburg_signals_addr([row], matches)
        self.assertEqual(signals, [])
        self.assertEqual(report[0]["status"], "NO_UNIQUE_PROPERTY_MATCH")
        self.assertEqual(report[0]["candidate_parcel_count"], 2)

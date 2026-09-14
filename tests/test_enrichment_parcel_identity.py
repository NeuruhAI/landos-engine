"""The enrichment keeps the parcel identity its source named.

Regression for the 2026-09-13 estate producer run, refused at 3_export on 12
repeated Durham (37063) keys. The fixture is cut from that run's own inputs:
every source signal and OneMap record behind the 12 keys (31 condo units),
plus the Union tracer parcel 09222012.
"""
from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from scripts import build_nc_dps_public_enrichment as core
from scripts import build_nc_dps_public_enrichment_v3 as v3

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "durham_condo_pin_collision_20260913.json").read_text(encoding="utf-8")
)


def _context(records):
    context = {}
    by_fips = {}
    for record in records:
        by_fips.setdefault(record["stcntyfips"], []).append(record)
    for fips, recs in by_fips.items():
        core.index_parcel_context(context, fips, recs)
    return context


class DurhamCondoPinCollisionTests(unittest.TestCase):
    def setUp(self):
        self.rows = core.aggregate(FIXTURE["signals"], _context(FIXTURE["onemap_records"]))
        self.durham = [r for r in self.rows if r["county_fips"] == "37063"]

    def test_fixture_is_the_refused_pattern(self):
        # Keyed by OneMap parno, the 31 Durham units collapse onto exactly the 12
        # keys the 09-13 export refused.
        pins = Counter(r["parno"] for r in FIXTURE["onemap_records"] if r["stcntyfips"] == "37063")
        repeated = sorted(f"NC:37063:{pin}" for pin, n in pins.items() if n > 1)
        self.assertEqual(repeated, FIXTURE["repeated_keys_before_fix"])
        self.assertEqual(len(repeated), 12)
        self.assertEqual(sum(pins.values()), 31)

    def test_each_unit_keeps_its_reid_and_no_key_repeats(self):
        self.assertEqual(len(self.durham), 31)
        self.assertEqual(core.duplicate_parcel_keys(self.rows), [])
        source_reids = {s["parcel_apn"] for s in FIXTURE["signals"] if s["geography"] == "37063"}
        self.assertEqual({r["parcel_apn"] for r in self.durham}, source_reids)

    def test_the_shared_pin_rides_as_alternate_parcel(self):
        pin_by_reid = {r["altparno"]: r["parno"] for r in FIXTURE["onemap_records"] if r["stcntyfips"] == "37063"}
        for row in self.durham:
            self.assertEqual(row["alternate_parcel"], pin_by_reid[row["parcel_apn"]])
            self.assertEqual(row["parcel_match_status"], "ONEMAP_MATCHED")

    def test_unit_context_is_the_units_own(self):
        # 0718075776: three units, three owners, three different assessed values.
        by_reid = {r["parcel_apn"]: r for r in self.durham}
        values = {by_reid[reid]["assessed_value"] for reid in ("211111", "211139", "230030")}
        self.assertEqual(values, {238298.0, 815800.0})
        self.assertEqual(by_reid["230030"]["assessed_value"], 815800.0)


class ParnoSpellingTests(unittest.TestCase):
    def test_source_spelling_of_the_parno_resolves_to_onemaps_spelling(self):
        rows = core.aggregate(FIXTURE["signals"], _context(FIXTURE["onemap_records"]))
        union = [r for r in rows if r["county_fips"] == "37179"]
        # Union signals name 09222012 both as "09.222.012" and "09222012": one row,
        # keyed the way OneMap (and the DeedSonar estate) spells it.
        self.assertEqual(len(union), 1)
        self.assertEqual(union[0]["parcel_apn"], "09222012")
        self.assertEqual(union[0]["alternate_parcel"], "09222012")

    def test_unmatched_source_keeps_its_own_number(self):
        signal = {"geography": "37063", "parcel_apn": "999999", "source_type": "taxdelinquent", "detail_json": {}}
        row = core.aggregate([signal], {})[0]
        self.assertEqual((row["parcel_apn"], row["alternate_parcel"]), ("999999", ""))
        self.assertEqual(row["parcel_match_status"], "SOURCE_ONLY")


class ParcelContextPrecedenceTests(unittest.TestCase):
    def test_parno_record_wins_a_cross_field_tie_in_either_order(self):
        filed = {"parno": "100072", "altparno": "X1", "ownname": "FILED UNDER 100072"}
        alias = {"parno": "0000000001", "altparno": "100072", "ownname": "ALIAS 100072"}
        for order in ([filed, alias], [alias, filed]):
            context = {}
            core.index_parcel_context(context, "37063", order)
            self.assertIs(context[("37063", "100072")], filed)

    def test_keys_several_records_claim_are_counted(self):
        units = [{"parno": "0718075776", "altparno": reid} for reid in ("211111", "211139", "230030")]
        context = {}
        self.assertEqual(core.index_parcel_context(context, "37063", units), 1)
        for reid in ("211111", "211139", "230030"):
            self.assertEqual(context[("37063", reid)]["altparno"], reid)


class PlaceholderSaleDateTests(unittest.TestCase):
    def test_roll_placeholders_are_no_sale_date(self):
        for text in ("12/30/1899 12:00:00 AM", "1/1/1900 12:00:00 AM", "1900-01-01", "12/31/1899"):
            self.assertIsNone(core.parse_years_owned(text), text)

    def test_real_sale_dates_still_count(self):
        self.assertEqual(core.parse_years_owned("01/01/2000"), 26)
        self.assertEqual(core.parse_years_owned("1921-06-01"), 105)
        self.assertEqual(core.parse_years_owned("8/31/2017 12:00:00 AM"), 9)

    def test_tracer_placeholder_leaves_tenure_unknown(self):
        rows = core.aggregate(FIXTURE["signals"], _context(FIXTURE["onemap_records"]))
        tracer = next(r for r in rows if r["parcel_apn"] == "09222012")
        self.assertEqual(tracer["last_sale_date"], "1/1/1900 12:00:00 AM")  # the roll's text, kept
        self.assertIsNone(tracer["years_owned"])
        self.assertTrue(all(r["years_owned"] is None for r in rows
                            if r["last_sale_date"].startswith("12/30/1899")))


class OwnerEvidenceTests(unittest.TestCase):
    """The heir / co-ownership proxies read the bill's owner text as well as
    OneMap's ownname (run through the strict classifiers v3 installs)."""

    def _row(self, bill_owner, onemap_owner):
        signal = {"geography": "37183", "parcel_apn": "0000000001", "owner_name": bill_owner,
                  "source_type": "taxdelinquent", "detail_json": {}}
        context = {("37183", "0000000001"): {"parno": "0000000001", "ownname": onemap_owner}}
        with mock.patch.object(core, "heir_flag", v3.heir_flag_strict), \
                mock.patch.object(core, "fragmented_flag", v3.fragmented_flag_strict):
            return core.aggregate([signal], context)[0]

    def test_onemap_dropping_the_co_owner_ampersand_keeps_co_ownership(self):
        # Wake shape: the bill "LAST, A & LAST, B", OneMap "LAST, A LAST, B".
        row = self._row("DOE, JANE & DOE, JOHN", "DOE, JANE DOE, JOHN")
        self.assertEqual(row["owner_name"], "DOE, JANE DOE, JOHN")
        self.assertTrue(row["fragmented_ownership_indicator"])

    def test_onemap_dropping_heirs_keeps_the_heir_signal(self):
        # Durham shape: the bill "JOHN Q DOE HEIRS", OneMap "DOE, JOHN Q".
        self.assertTrue(self._row("JOHN Q DOE HEIRS", "DOE, JOHN Q")["heir_indicator"])

    def test_onemap_only_evidence_still_counts(self):
        self.assertTrue(self._row("DOE JOHN", "DOE JOHN HEIRS")["heir_indicator"])

    def test_no_evidence_in_either_text_stays_false(self):
        row = self._row("DOE, JOHN", "DOE, JOHN")
        self.assertFalse(row["heir_indicator"])
        self.assertFalse(row["fragmented_ownership_indicator"])

    def test_institutional_ampersand_is_not_co_ownership(self):
        self.assertFalse(self._row("SMITH & JONES LLC", "SMITH JONES LLC")["fragmented_ownership_indicator"])


class V3RefusesRepeatedKeysTests(unittest.TestCase):
    def test_a_repeated_key_is_refused_before_the_csv_is_written(self):
        row = {"county_fips": "37063", "parcel_apn": "0718075776", "owner_name": "A",
               "parcel_match_status": "ONEMAP_MATCHED"}
        with TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"

            def fake_v2_run(signals_path, meck_path, fresh_dir):
                path = Path(fresh_dir) / "LANDOS_ENRICHED_OPPORTUNITIES.csv"
                path.parent.mkdir(parents=True, exist_ok=True)
                core.write_csv(path, [row, dict(row, owner_name="B")])
                return {"source_signals": 2, "source_errors": []}

            with mock.patch.object(v3.v2, "run", side_effect=fake_v2_run):
                with self.assertRaises(SystemExit) as refused:
                    v3.run(Path(tmp) / "signals.jsonl", out, None, Path(tmp) / "meck.jsonl")
            self.assertIn("('37063', '0718075776')", str(refused.exception))
            self.assertFalse((out / "LANDOS_ENRICHED_OPPORTUNITIES.csv").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

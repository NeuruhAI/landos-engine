from datetime import date
from scripts.deep_report import freshness_guard

AS_OF = date(2026, 6, 3)


def test_confirmed_address_allowed():
    ok, vintage, refusal = freshness_guard("11-54-0340-0151-02-5", [], AS_OF)
    assert ok and "confirmed" in vintage.lower() and refusal == ""


def test_fresh_2026_signal_allowed():
    sigs = [{"signal_date": date(2026, 1, 1), "source_id": "taxsale_berrien_2906"}]
    ok, vintage, refusal = freshness_guard("11-43-0009-0064-10-3", sigs, AS_OF)
    assert ok and "LIVE" in vintage and refusal == ""


def test_stale_2022_signal_refused():
    sigs = [{"signal_date": date(2022, 1, 1), "source_id": "taxsale_berrien_2049"}]
    ok, _vintage, refusal = freshness_guard("52-7300-0018-00-7", sigs, AS_OF)
    assert not ok and "STALE PARCEL" in refusal and "2022-01-01" in refusal


def test_no_signal_non_confirmed_refused():
    ok, _vintage, refusal = freshness_guard("99-99-9999-9999-99-9", [], AS_OF)
    assert not ok and "STALE PARCEL" in refusal

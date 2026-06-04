from app.normalizers.tax_sale_info import TaxSaleInfoNormalizer
from scripts.lead_queue import correlation_boost

SAMPLE_CSV = (
    "Lot Number,Minimum Bid,Current Taxes,Parcel Id,County,Address,Local Unit,SEV,Legal Description,Latitude,Longitude\n"
    # tax-sale.info writes longitude with a 'W' hemisphere letter (no minus sign)
    '1,"1,500.00","3,200.00",11-54-0340-0151-02-5,berrien,133 E MAIN ST,Benton Harbor,40000,LOT 1,N42.1159,W86.4573\n'
    "2,8000,900,11-03-5800-0001-00-4,berrien,1850 PIPESTONE RD,Benton Twp,30000,LOT 2,42.0848,W86.4201\n"
    ",,,,,,,,,,\n"  # blank / no-APN row must be skipped (never invent an APN)
)


def test_normalize_parses_and_skips_blank():
    cands = TaxSaleInfoNormalizer("t", 2049, "test", 2022).normalize(SAMPLE_CSV)
    assert len(cands) == 2  # the blank row is dropped
    c0 = cands[0]
    assert c0["apn"] == "11-54-0340-0151-02-5"
    assert c0["signal_type"] == "tax_delinquent"
    assert c0["lat"] == 42.1159 and c0["lng"] == -86.4573
    assert c0["raw_meta"]["sev"] == 40000.0
    assert c0["raw_meta"]["minimum_bid"] == 1500.0
    assert c0["signal_date"].year == 2022


def test_strength_deep_discount_bumps():
    csv1 = "Parcel Id,Minimum Bid,SEV,Address,Local Unit\nAAA,1000,100000,1 X ST,Y\n"  # ratio 0.01
    c = TaxSaleInfoNormalizer("t", 1, "x", 2026).normalize(csv1)[0]
    assert c["signal_strength"] == 0.92


def test_normalize_requires_apn():
    assert TaxSaleInfoNormalizer("t", 1, "x", 2026).normalize("Parcel Id,Address\n,No APN\n") == []


def test_corr_single_signal_is_neutral():
    b, n, types = correlation_boost([{"signal_type": "tax_delinquent", "signal_strength": 0.85}])
    assert b == 1.0 and n == 1 and types == ["tax_delinquent"]


def test_corr_stacks_count_and_types():
    sigs = [
        {"signal_type": "tax_delinquent", "signal_strength": 0.85},
        {"signal_type": "blight_demo", "signal_strength": 0.9},
    ]
    b, n, types = correlation_boost(sigs)
    assert n == 2 and types == ["blight_demo", "tax_delinquent"]
    assert 1.0 < b <= 1.40


def test_corr_is_capped():
    sigs = [{"signal_type": f"t{i}", "signal_strength": 1.0} for i in range(20)]
    b, _n, _t = correlation_boost(sigs)
    assert b == 1.40

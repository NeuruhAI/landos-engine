from app.normalizers.egle_part201 import EglePart201Normalizer
from scripts.lead_queue import correlation_boost

SITE = {"SiteName": "Acme Cleaners", "SiteID": "BH123", "Address": "100 Main St",
        "Latitude": 42.11, "Longitude": -86.45, "Contaminants": "PCE", "RiskCondition": "open"}


def test_resolved_site_emits_signal():
    c = EglePart201Normalizer.build_signal(SITE, "11-54-0001-0001-00-0", "high", 2026)
    assert c is not None
    assert c["signal_type"] == "environmental_part201"
    assert c["apn"] == "11-54-0001-0001-00-0"
    assert c["raw_meta"]["environmental_flag"] is True
    assert c["raw_meta"]["site_id"] == "BH123"
    assert c["signal_date"].year == 2026


def test_unresolved_site_not_emitted():
    assert EglePart201Normalizer.build_signal(SITE, None, None, 2026) is None


def test_correlation_stacks_tax_and_part201():
    sigs = [{"signal_type": "tax_delinquent", "signal_strength": 0.85},
            {"signal_type": "environmental_part201", "signal_strength": 0.6}]
    boost, n, types = correlation_boost(sigs)
    assert n == 2 and "environmental_part201" in types and boost > 1.0

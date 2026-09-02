"""B1: contact-field freshness is rules, not ML, and unknown stays unknown."""
from datetime import date
from app.freshness import (field_freshness, row_freshness_score, staleness_days,
                           FRESH, AGING, STALE, FRESHNESS_RULES)

TODAY = date(2026, 9, 2)


def test_rule_table_matches_the_published_doc():
    assert FRESHNESS_RULES["phone"] == (90, 365)
    assert FRESHNESS_RULES["mailing_address"] == (180, 365)
    assert FRESHNESS_RULES["arrears"] == (90, 365)


def test_phone_boundaries():
    assert field_freshness("phone", "2026-06-04", TODAY) == (FRESH, 90, True)    # exactly 90 d
    assert field_freshness("phone", "2026-06-03", TODAY) == (AGING, 91, True)    # 91 d
    assert field_freshness("phone", "2025-09-02", TODAY) == (AGING, 365, True)   # exactly 365 d
    assert field_freshness("phone", "2025-09-01", TODAY) == (STALE, 366, True)   # 366 d


def test_unknown_as_of_is_stale_and_flagged_unknown_never_defaulted():
    assert field_freshness("phone", None, TODAY) == (STALE, None, False)
    assert field_freshness("phone", "", TODAY) == (STALE, None, False)
    assert staleness_days("not a date", TODAY) is None


def test_future_as_of_is_stale():
    assert field_freshness("phone", "2026-09-03", TODAY)[0] == STALE


def test_tracerfy_trace_date_reads_as_aging_not_today():
    state, days, known = field_freshness("phone", "2026-07-28", TODAY)
    assert (state, days, known) == (FRESH, 36, True)
    assert field_freshness("phone", "2026-06-30", TODAY) == (FRESH, 64, True)


def test_row_score_rules():
    assert row_freshness_score({"phone": "2026-08-20", "mailing_address": "2026-08-01"}, TODAY) == FRESH
    assert row_freshness_score({"phone": "2026-05-01", "mailing_address": "2026-08-01"}, TODAY) == AGING
    assert row_freshness_score({"phone": "2026-08-20", "mailing_address": None}, TODAY) == STALE
    assert row_freshness_score({}, TODAY) == STALE

"""A1 (2026-09-02): the Tracerfy resolved-file import carries freshness stamps,
never downgrades a phone, and names every rejection."""
from scripts.import_resolved import (as_of_from_filename, merge_never_downgrade,
                                     stamp_phones, build_phones)


def test_as_of_comes_from_the_file_name_never_today():
    assert as_of_from_filename("data/incoming/tracerfy_resolved_20260728T191348Z.csv") == "2026-07-28"
    assert as_of_from_filename("tracerfy_resolved_20260630T225130Z.csv") == "2026-06-30"
    assert as_of_from_filename("berrien_RESOLVED.csv") is None


def test_stamp_phones_sets_source_as_of_observed_at_and_default_source():
    phones = build_phones({"phone_1": "(269) 555-0100", "phone_1_confidence": "0.9",
                           "phone_1_source": "", "phone_2": "2695550101", "phone_2_source": "tps"}, ["tracerfy"])
    out = stamp_phones(phones, source_stamp="tracerfy_20260805", source_as_of="2026-07-28",
                       observed_at="2026-09-02T18:00:00Z")
    assert out[0]["source"] == "tracerfy_20260805"          # blank per-phone source -> the stamp
    assert out[1]["source"] == "tps"                         # delivered source kept
    assert all(p["source_as_of"] == "2026-07-28" for p in out)
    assert all(p["observed_at"] == "2026-09-02T18:00:00Z" for p in out)


def test_stamp_phones_keeps_unknown_as_of_unknown():
    out = stamp_phones([{"number": "+12695550100", "confidence": 0.5}], source_stamp=None,
                       source_as_of=None, observed_at="2026-09-02T18:00:00Z")
    assert out[0]["source_as_of"] is None
    assert "source" not in out[0]


def test_merge_never_downgrades_and_never_drops():
    existing = [{"number": "+12695550100", "confidence": 0.9, "source": "old"},
                {"number": "+12695550199", "confidence": 0.7, "source": "old"}]
    incoming = [{"number": "+12695550100", "confidence": 0.6, "source": "new"},   # lower -> blocked
                {"number": "+12695550101", "confidence": 0.8, "source": "new"}]   # new number -> added
    merged, blocked = merge_never_downgrade(existing, incoming)
    nums = {p["number"]: p for p in merged}
    assert nums["+12695550100"]["confidence"] == 0.9 and nums["+12695550100"]["source"] == "old"
    assert nums["+12695550199"]["confidence"] == 0.7                     # untouched number preserved
    assert nums["+12695550101"]["source"] == "new"
    assert blocked == [{"number": "+12695550100", "existing_confidence": 0.9, "incoming_confidence": 0.6}]


def test_merge_equal_or_higher_confidence_replaces_with_new_stamps():
    existing = [{"number": "+12695550100", "confidence": 0.6, "source": "old"}]
    incoming = [{"number": "+12695550100", "confidence": 0.6, "source": "new", "source_as_of": "2026-07-28"}]
    merged, blocked = merge_never_downgrade(existing, incoming)
    assert merged[0]["source"] == "new" and merged[0]["source_as_of"] == "2026-07-28" and blocked == []

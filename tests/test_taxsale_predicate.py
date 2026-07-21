"""
test_taxsale_predicate.py — G1: the complete tax-sale mint predicate + reconciliation.

Pure fixtures, no DB. Covers: each predicate leg (freshness / needs_diligence / flag /
promote), reporting precedence, fail-closed defaults on partial rows, county-by-county
excluded counts, the books-balance invariant, and determinism (input order must never
move the receipt hash; the clock is injected, never read).
"""
import random
from datetime import date

from app.taxsale_predicate import (EXCLUSION_ORDER, evaluate_mint_predicate,
                                   reconcile, reconciliation_canonical,
                                   render_reconciliation)

TODAY = date(2026, 7, 21)   # injected clock — tests never read the wall clock


def _sig(ref="TS-1", fips="18141", sale="2026-09-01", promoted=True,
         needs_diligence=False, flag=None, raw=None, apn="71-01-01-100-001.000-001"):
    return {"source_ref": ref, "county_fips": fips, "parcel_apn": apn,
            "owner_name": "DOE JOHN", "signal_date": sale, "raw_payload": raw or {},
            "promoted": promoted, "needs_diligence": needs_diligence,
            "diligence_flag": flag}


# ---- the predicate, leg by leg --------------------------------------------

def test_fully_cleared_signal_is_eligible():
    ok, code, why = evaluate_mint_predicate(_sig(), TODAY)
    assert ok and code == "eligible"
    assert "upcoming" in why


def test_freshness_is_required_past_sale_never_mints():
    ok, code, _ = evaluate_mint_predicate(_sig(sale="2026-01-15"), TODAY)
    assert not ok and code == "stale"


def test_freshness_is_required_resolved_sale_never_mints():
    # future-dated but already resolved (buyer present) — the freshness gate's own rule
    ok, code, _ = evaluate_mint_predicate(
        _sig(sale="2026-09-01", raw={"buyer_name": "SOMEBODY ELSE"}), TODAY)
    assert not ok and code == "stale"


def test_needs_diligence_true_never_mints_even_promoted():
    ok, code, _ = evaluate_mint_predicate(_sig(promoted=True, needs_diligence=True), TODAY)
    assert not ok and code == "needs_diligence"


def test_flagged_never_mints_even_fresh_promoted_and_cleared():
    ok, code, why = evaluate_mint_predicate(
        _sig(promoted=True, needs_diligence=False, flag="title cloud found"), TODAY)
    assert not ok and code == "flagged"
    assert "title cloud found" in why


def test_unpromoted_never_mints():
    ok, code, _ = evaluate_mint_predicate(_sig(promoted=False), TODAY)
    assert not ok and code == "not_promoted"


def test_reporting_precedence_flag_beats_everything():
    # a row that is stale AND un-reviewed AND un-promoted AND flagged reports 'flagged'
    ok, code, _ = evaluate_mint_predicate(
        _sig(sale="2025-01-01", promoted=False, needs_diligence=True, flag="dup APN"), TODAY)
    assert not ok and code == "flagged"


def test_reporting_precedence_stale_beats_review_state():
    ok, code, _ = evaluate_mint_predicate(
        _sig(sale="2025-01-01", promoted=False, needs_diligence=True), TODAY)
    assert not ok and code == "stale"


# ---- fail-closed defaults --------------------------------------------------

def test_partial_row_fails_closed():
    # a row missing the mig-039 columns entirely reads as needs-diligence (never mints)
    row = {"source_ref": "TS-X", "county_fips": "18141",
           "signal_date": "2026-09-01", "raw_payload": {}}
    ok, code, _ = evaluate_mint_predicate(row, TODAY)
    assert not ok and code == "needs_diligence"


def test_whitespace_flag_is_not_a_flag():
    # an empty/whitespace diligence_flag is NOT a flag (never store one; but never trip on it)
    ok, code, _ = evaluate_mint_predicate(_sig(flag="   "), TODAY)
    assert ok and code == "eligible"


# ---- reconciliation --------------------------------------------------------

def _population():
    return [
        _sig(ref="A-1", fips="18141"),                                        # eligible
        _sig(ref="A-2", fips="18141", sale="2025-06-01"),                     # stale
        _sig(ref="A-3", fips="18141", needs_diligence=True),                  # needs_diligence
        _sig(ref="B-1", fips="26021", promoted=False),                        # not_promoted
        _sig(ref="B-2", fips="26021", flag="assessed twice"),                 # flagged
        _sig(ref="B-3", fips="26021"),                                        # eligible
        _sig(ref="C-1", fips="26027", promoted=False, needs_diligence=True),  # not_prom+needs -> needs
    ]


def test_reconcile_county_by_county_excluded_counts():
    r = reconcile(_population(), TODAY)
    assert r["total"] == 7 and r["eligible"] == 2
    assert r["excluded"] == {"flagged": 1, "stale": 1, "needs_diligence": 2, "not_promoted": 1}
    assert r["per_county"]["18141"]["excluded"]["stale"] == 1
    assert r["per_county"]["18141"]["excluded"]["needs_diligence"] == 1
    assert r["per_county"]["26021"]["excluded"]["flagged"] == 1
    assert r["per_county"]["26021"]["excluded"]["not_promoted"] == 1
    assert r["per_county"]["26027"]["excluded"]["needs_diligence"] == 1
    # every county block always carries every reason key (stable shape for diffing)
    for pc in r["per_county"].values():
        assert set(pc["excluded"]) == set(EXCLUSION_ORDER)


def test_reconcile_books_balance():
    r = reconcile(_population(), TODAY)
    assert r["reconciled"] is True
    assert r["total"] == r["eligible"] + sum(r["excluded"].values())
    per_county_total = sum(pc["total"] for pc in r["per_county"].values())
    assert per_county_total == r["total"]


def test_reconcile_deterministic_across_input_order():
    pop = _population()
    base = reconcile(pop, TODAY)
    for seed in (1, 7, 42):
        shuffled = pop[:]
        random.Random(seed).shuffle(shuffled)
        again = reconcile(shuffled, TODAY)
        assert again["receipt_sha256"] == base["receipt_sha256"]
        assert reconciliation_canonical(again) == reconciliation_canonical(base)


def test_reconcile_receipt_moves_when_data_moves():
    base = reconcile(_population(), TODAY)
    changed = _population()
    changed[0]["diligence_flag"] = "late-found lien"
    assert reconcile(changed, TODAY)["receipt_sha256"] != base["receipt_sha256"]


def test_reconcile_clock_is_injected():
    # the same rows a year later: the upcoming sales have been consumed
    later = reconcile(_population(), date(2027, 7, 21))
    assert later["eligible"] == 0
    assert later["excluded"]["stale"] >= 3


def test_reconcile_empty_population():
    r = reconcile([], TODAY)
    assert r["total"] == 0 and r["eligible"] == 0 and r["reconciled"] is True
    assert r["per_county"] == {} and r["eligible_refs"] == []


def test_render_reconciliation_reports_all_counties():
    out = render_reconciliation(reconcile(_population(), TODAY))
    for fips in ("18141", "26021", "26027"):
        assert f"[{fips}]" in out
    assert "reconciled=YES" in out

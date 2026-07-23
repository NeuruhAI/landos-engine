"""
test_taxsale_gate.py — regression guards for the tax-sale PROMOTE gate (mig 036).
The live proof is scripts/probe_taxsale_gate.py (a real DB probe, rolled back), pinned in the
commit receipt. These are the cheap CI guards that the gate can't be silently removed.
"""
import pytest

from scripts import mint_taxsale_leads, promote_taxsale


def test_mint_query_has_the_promote_gate():
    # gate-in-DATA: the mint's candidate query MUST filter promoted=true. If this predicate is
    # ever dropped, the ungated ToU hazard returns — fail loudly.
    sql = mint_taxsale_leads.SIGNALS.lower()
    assert "promoted = true" in sql
    assert "source_type = 'taxsale'" in sql


def test_promote_refuses_bulk():
    # contract §3: no promote-all. Zero targets must refuse, never touch the DB.
    with pytest.raises(SystemExit):
        promote_taxsale.promote("some-user-uuid", refs=[], apply=True)


def test_promote_sql_scopes_to_taxsale_and_unpromoted():
    sql = promote_taxsale.PROMOTE.lower()
    assert "source_type = 'taxsale'" in sql
    assert "promoted = false" in sql          # never re-promote / touch a non-taxsale row
    assert "promoted_by" in sql and "promoted_at" in sql


# ---- G1 (mig 040): the complete diligence predicate, gate-in-DATA ----------

def test_mint_query_has_the_full_diligence_predicate():
    # a taxsale signal mints ONLY when reviewed (needs_diligence=false) and un-flagged.
    # If either clause is ever dropped, un-reviewed or flagged records mint again — fail loudly.
    sql = mint_taxsale_leads.SIGNALS.lower()
    assert "needs_diligence = false" in sql
    assert "diligence_flag is null" in sql


def test_recon_query_is_ungated():
    # the dry-run reconciler must see EVERYTHING (else excluded counts lie by omission)
    sql = mint_taxsale_leads.RECON_SIGNALS.lower()
    assert "source_type = 'taxsale'" in sql
    assert "promoted = true" not in sql
    assert "needs_diligence = false" not in sql
    assert "diligence_flag is null" not in sql


def test_promote_sql_completes_diligence_and_refuses_flagged():
    sql = promote_taxsale.PROMOTE.lower()
    assert "needs_diligence = false" in sql    # the keystroke IS the completed review
    assert "diligence_flag is null" in sql     # a flagged row cannot be promoted


def test_flag_refuses_bulk_and_requires_reason():
    import pytest
    with pytest.raises(SystemExit):
        promote_taxsale.flag("some-user-uuid", refs=[], reason="x", apply=True)
    with pytest.raises(SystemExit):
        promote_taxsale.flag("some-user-uuid", refs=["TS-1"], reason="  ", apply=True)


def test_clear_flag_reopens_diligence_never_promotes():
    sql = promote_taxsale.CLEAR_FLAG.lower()
    assert "needs_diligence = true" in sql     # fail-closed: clearing a flag re-opens review
    assert "promoted = true" not in sql        # clearing a flag NEVER promotes

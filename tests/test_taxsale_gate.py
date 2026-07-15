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

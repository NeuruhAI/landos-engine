"""P0 TCPA projection contract: unknown never becomes callable."""
import inspect
import re

import scripts.sync_to_crm as sync
from scripts.sync_to_crm import (
    CALLABLE_OR_SUPPRESSED,
    ORPHAN_CALLABLE,
    dnc_verdict,
)


def test_dnc_verdict_is_strict_tri_state():
    assert dnc_verdict("clear") is False
    assert dnc_verdict(" CLEAR ") is False
    assert dnc_verdict("do_not_call") is True
    for unknown in (None, "", "unchecked", "pending", "dnc", "unknown"):
        assert dnc_verdict(unknown) is None


def test_main_requires_explicit_clear_and_writes_sticky_dnc():
    src = inspect.getsource(sync.main)
    assert "projected_dnc is False" in src
    assert "when dnc is true then true" in src
    assert "p1, p1t, p2, conf, projected_dnc" in src


def test_contact_bridge_never_coalesces_unknown_to_false():
    combined = re.sub(r"\s+", " ", (CALLABLE_OR_SUPPRESSED + ORPHAN_CALLABLE).lower())
    assert "coalesce(dnc, false)" not in combined
    assert "public.callable_leads" in combined


def test_python_contact_gate_uses_strict_callable_view_decision():
    src = inspect.getsource(sync.sync_seller_contacts)
    assert 'bool(L["callable"])' in src


def test_operator_smoke_fixture_is_never_auto_promoted():
    src = inspect.getsource(sync.main)
    assert sync.OPERATOR_SMOKE_TEST_LEAD_ID in src
    assert "Operator-owned smoke-test cell" in src
    assert "then case when dnc is true then true else null end" in src


def test_main_dry_run_does_not_call_contact_writer():
    src = inspect.getsource(sync.main)
    dry_branch = src.index("if dry_run:")
    writer_call = src.index("contact_stats = sync_seller_contacts(cur)")
    assert dry_branch < writer_call
    assert "NO writes" in src[dry_branch:writer_call]

"""ORANGE JOB 4 contract: the push_status marker is a same-transaction RECEIPT.

Source-contract tests in the test_sync_dnc_projection idiom — they pin the
load-bearing properties without a live DB:
  * the marker runs in the SAME transaction as the projection (before commit)
  * only actually-projected leads (UPDATE or discovered INSERT) are marked
  * empty projection => no marker statement at all
  * marker count must reconcile with projected count (assert, refuse to commit)
  * updated_at is NEVER stamped (that column is ingest freshness, not push state)
  * push_status stays OUT of the sync SELECTOR (receipt, not resumability — T7)
"""
import inspect
import re

import scripts.sync_to_crm as sync

SRC = inspect.getsource(sync.main)


def test_marker_is_in_same_transaction_before_commit():
    marker = SRC.index("push_status = 'pushed'")
    commit = SRC.index("conn.commit()", marker)
    assert marker < commit, "marker must precede the projection commit"
    # and it sits AFTER the contact bridge, inside the live (non-dry-run) path
    assert SRC.index("contact_stats = sync_seller_contacts(cur)") < marker


def test_marker_guards_the_empty_list():
    guard = SRC.index("if projected_lead_ids:")
    marker = SRC.index("push_status = 'pushed'")
    assert guard < marker


def test_marker_count_must_reconcile():
    assert "assert cur.rowcount == len(projected_lead_ids)" in SRC


def test_only_projected_leads_are_marked():
    # collected exactly twice: the curated UPDATE path and the discovered INSERT path
    assert SRC.count('projected_lead_ids.append(r["lead_id"])') == 2
    # the unmatched WARN path must NOT collect
    warn = SRC.index("WARN no acquisition_leads row")
    nxt = SRC.index("continue", warn)
    assert "projected_lead_ids.append" not in SRC[warn:nxt]


def test_marker_never_touches_updated_at():
    marker_stmt = re.search(
        r"update landos\.owner_leads\s+set (.*?)where", SRC, re.S).group(1)
    assert "updated_at" not in marker_stmt
    assert "push_status" in marker_stmt
    assert "pushed_at" in marker_stmt
    assert "push_target = 'public.acquisition_leads'" in marker_stmt


def test_push_status_stays_out_of_the_sync_selector():
    selector = SRC[: SRC.index("leads = cur.fetchall()")]
    assert "push_status" not in selector, (
        "push_status is a receipt, not a resumability mechanism (T7) — "
        "the selector must not read it")

"""
test_seller_contact.py — the A8.2 contact-bridge invariant (mirror of the CRM
src/lib/leads/seller-contact.ts). No DB: a fake cursor exercises every branch of
the upsert, plus the sync SQL invariants. Live census (47 w/phone + 39 orphans
-> 86 / 0, idempotent, human rows untouched) is pinned in the commit receipt.
"""
import re

from scripts.seller_contact import parse_name, upsert_seller_contact_phone, SOURCE
from scripts.sync_to_crm import CALLABLE_OR_SUPPRESSED, ORPHAN_CALLABLE


class FakeCur:
    """Minimal dict_row cursor: fetchone() returns the seeded seller row (or None);
    every execute is recorded so we can assert what was written."""
    def __init__(self, existing=None):
        self.existing = existing
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((re.sub(r"\s+", " ", sql).strip().lower(), params))

    def fetchone(self):
        return self.existing

    def writes(self):
        return [c for c in self.calls if c[0].startswith(("insert", "update"))]


# ---- name parsing (mirrors the .ts split) ------------------------------------------

def test_parse_name():
    assert parse_name("Vandermolen Family Trust") == ("Vandermolen Family", "Trust")
    assert parse_name("Cher") == ("Cher", None)
    assert parse_name("  ") == (None, None) and parse_name(None) == (None, None)


# ---- upsert branches ---------------------------------------------------------------

def test_creates_when_absent_and_callable():
    cur = FakeCur(existing=None)
    out = upsert_seller_contact_phone(cur, lead_id="L1", tenant_id="T", owner_name="Ann B Smith", phone="+15551234567", callable=True)
    assert out == "created"
    ins = cur.writes()[0]
    assert ins[0].startswith("insert into public.contacts")
    assert "'seller'" in ins[0] and SOURCE in ins[1]           # seller row, spine-stamped


def test_fills_a_phoneless_row():
    cur = FakeCur(existing={"contact_id": "c1", "phone": None, "source": "lead_phone_mirror"})
    out = upsert_seller_contact_phone(cur, lead_id="L1", tenant_id="T", owner_name="X", phone="+15551112222", callable=True)
    assert out == "filled"
    assert cur.writes()[0][0].startswith("update public.contacts set phone")


def test_never_clobbers_an_existing_phone():
    cur = FakeCur(existing={"contact_id": "c1", "phone": "+15559999999", "source": "spine_sync"})
    out = upsert_seller_contact_phone(cur, lead_id="L1", tenant_id="T", owner_name="X", phone="+15551112222", callable=True)
    assert out == "left" and cur.writes() == []               # first-write wins


def test_retracts_only_spine_owned_phone_when_suppressed():
    cur = FakeCur(existing={"contact_id": "c1", "phone": "+15551112222", "source": SOURCE})
    out = upsert_seller_contact_phone(cur, lead_id="L1", tenant_id="T", owner_name="X", phone=None, callable=False)
    assert out == "retracted"
    upd = cur.writes()[0]
    assert "set phone=null" in upd[0] and upd[1] == ("c1", SOURCE)


def test_never_touches_a_human_contact_when_suppressed():
    cur = FakeCur(existing={"contact_id": "c1", "phone": "+15551112222", "source": "homeowner_direct"})
    out = upsert_seller_contact_phone(cur, lead_id="L1", tenant_id="T", owner_name="X", phone=None, callable=False)
    assert out == "noop" and cur.writes() == []               # RED STOP: human untouched


def test_dnc_number_is_never_created():
    # callable=False (suppressed) with no existing row -> nothing written
    cur = FakeCur(existing=None)
    out = upsert_seller_contact_phone(cur, lead_id="L1", tenant_id="T", owner_name="X", phone="+15551112222", callable=False)
    assert out == "noop" and cur.writes() == []


# ---- sync SQL invariants -----------------------------------------------------------

def test_orphan_assertion_is_seller_scoped_and_dnc_gated():
    s = re.sub(r"\s+", " ", ORPHAN_CALLABLE.lower())
    assert "c.contact_type = 'seller'" in s
    assert "not coalesce(a.dnc, false)" in s                   # DNC leads are not "orphans"
    assert "phone_1 is not null" in s


def test_pass_targets_callable_or_suppressed_only():
    s = re.sub(r"\s+", " ", CALLABLE_OR_SUPPRESSED.lower())
    assert "phone_1 is not null" in s and "coalesce(dnc, false)" in s

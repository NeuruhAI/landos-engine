"""Bounded execution of sync_to_crm (2026-09-13): deterministic keyset pages, one commit
per page, JSONL checkpoint, resume after a kill, idempotent retry, and the SAME gates.

The DB is an in-memory fake of the two tables the writer touches (landos.owner_leads read
side; public.acquisition_leads + public.contacts write side) that answers the writer's
own SQL by shape. Nothing here connects anywhere.
"""
import json
import re
import uuid
from unittest import mock

import pytest

import scripts.sync_to_crm as sync
from scripts.sync_to_crm import CALLABLE_OR_SUPPRESSED, ORPHAN_CALLABLE

EFXO = "postgresql://postgres.efxowqudmsfqjmbeidjc:pw@aws-0-us-east-2.pooler.supabase.com:5432/postgres"
LVDI = "postgresql://postgres.lvdimujkfheeyqchjzra:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"


def _lead(i, **over):
    row = {
        "lead_id": uuid.UUID(int=i + 1), "owner_name": f"OWNER {i}", "situs_lat": None, "situs_lng": None,
        "skip_trace_status": "not_traced", "confidence_score": None, "recipe": None, "ch_name": None,
        "caf": None, "dnc_status": None, "phones": None, "scrub_receipt": None, "signal_overlap": None,
        "owner_type": "individual", "emerging_score": None, "emerging_signals": None, "provenance": "curated",
        "parcel_apn": f"APN-{i}", "county_fips": "18091", "raw_meta": None, "situs_address": None,
        "owner_mailing_address": None, "owner_mailing_city": None, "owner_mailing_state": None,
        "owner_mailing_zip": None,
    }
    row.update(over)
    return row


class FakeDB:
    """Answers the writer's SQL by shape. Pending writes land on commit, vanish on rollback."""

    def __init__(self, leads, matched=None):
        self.leads = sorted(leads, key=lambda r: r["lead_id"])
        self.acq = {}            # source_lead_id -> acquisition row
        self.contacts = {}       # acq lead_id -> contact row
        for r in self.leads:
            if matched is None or r["lead_id"] in matched:
                self.acq[r["lead_id"]] = {"lead_id": uuid.uuid4(), "phone_1": None, "dnc": None}
        self.commits = self.rollbacks = 0
        self.pending = []                 # ("update"|"insert"|"contact_*", key, payload)
        self.committed_updates = []
        self.committed_inserts = []
        self.update_params = {}
        self.max_page = 0
        self.max_lookup = 0
        self.fail_update_of = None
        self.contacts_at_commit = []

    # -- transaction -------------------------------------------------------------
    def _snapshot(self):
        return {"acq": {k: dict(v) for k, v in self.acq.items()},
                "contacts": {k: dict(v) for k, v in self.contacts.items()}}

    def begin(self):
        self._base = self._snapshot()

    def commit(self):
        self.commits += 1
        for kind, key, payload in self.pending:
            (self.committed_updates if kind == "update" else
             self.committed_inserts if kind == "insert" else []).append(key)
        self.pending = []
        self.contacts_at_commit.append({k: dict(v) for k, v in self.contacts.items()})
        self._base = self._snapshot()

    def rollback(self):
        self.rollbacks += 1
        self.pending = []
        self.acq, self.contacts = self._base["acq"], self._base["contacts"]
        self._base = self._snapshot()

    # -- bridge view emulation ---------------------------------------------------------
    def _callable_or_suppressed(self, scope):
        out = []
        for src, a in self.acq.items():
            if scope is not None and a["lead_id"] not in scope:
                continue
            c = self.contacts.get(a["lead_id"])
            if (a["phone_1"] or a["dnc"] is True or (c and c["source"] == "spine_sync")):
                out.append({"lead_id": a["lead_id"], "owner_name": f"ACQ {src}", "phone_1": a["phone_1"],
                            "dnc": a["dnc"], "callable": bool(a["phone_1"]) and a["dnc"] is False})
        return out

    def _orphans(self, scope):
        return [{"lead_id": r["lead_id"], "owner_name": r["owner_name"]}
                for r in self._callable_or_suppressed(scope)
                if r["callable"] and not (self.contacts.get(r["lead_id"]) or {}).get("phone")]


class FakeCursor:
    def __init__(self, db):
        self.db, self.rows, self.rowcount = db, [], -1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        db, s = self.db, re.sub(r"\s+", " ", sql).strip().lower()
        params = params or ()
        self.rowcount = -1
        if "as ch_name from landos.owner_leads" in s and "order by lead_id" not in s:
            flt = params[1]
            self.rows = [{"ch_name": r["ch_name"]} for r in db.leads if flt is None or r["lead_id"] in flt]
        elif "from landos.owner_leads where" in s and "order by lead_id limit" in s:
            _, flt, _, after, _, limit = params
            rows = [r for r in db.leads if flt is None or r["lead_id"] in flt]
            if after is not None:
                after = uuid.UUID(str(after))
                rows = [r for r in rows if r["lead_id"] > after]
            self.rows = [dict(r) for r in (rows if limit is None else rows[:limit])]
            db.max_page = max(db.max_page, len(self.rows))
        elif "from landos.scores" in s or "from landos.contact_relations" in s or "landos.discovery_signals ds" in s:
            ids = params[-1]
            db.max_lookup = max(db.max_lookup, len(ids))
            self.rows = []
        elif s.startswith("update public.acquisition_leads set"):
            src = params[-1]
            if src == db.fail_update_of:
                raise RuntimeError(f"injected failure on {src}")
            a = db.acq.get(src)
            if a is None:
                self.rowcount = 0
            else:
                p1, dnc = params[6], params[10]
                a["phone_1"] = p1
                a["dnc"] = True if a["dnc"] is True else dnc
                db.update_params[src] = params
                db.pending.append(("update", src, params))
                self.rowcount = 1
        elif s.startswith("insert into public.acquisition_leads"):
            src = params[0]
            assert src not in db.acq, "duplicate discovered-tier INSERT"
            assert len(params) == len(sync.DISCOVERED_INSERT_COLS)
            db.acq[src] = {"lead_id": uuid.uuid4(), "phone_1": params[21], "dnc": params[25]}
            db.pending.append(("insert", src, params))
            self.rowcount = 1
        elif s.startswith("select lead_id from public.acquisition_leads where tenant_id = %s and source_lead_id = any(%s)"):
            self.rows = [{"lead_id": db.acq[x]["lead_id"]} for x in params[1] if x in db.acq]
        elif "as callable from public.acquisition_leads a" in s:
            scope = set(params[1]) if len(params) > 1 else None
            self.rows = db._callable_or_suppressed(scope)
        elif "from public.callable_leads cl where" in s:
            scope = set(params[1]) if len(params) > 1 else None
            self.rows = db._orphans(scope)
        elif s.startswith("select contact_id, phone, source from public.contacts"):
            c = db.contacts.get(params[0])
            self.rows = [c] if c else []
        elif s.startswith("insert into public.contacts"):
            _, _, _, phone, lead_id, source = params
            assert lead_id not in db.contacts, "duplicate seller contact"
            db.contacts[lead_id] = {"contact_id": uuid.uuid4(), "phone": phone, "source": source}
            db.pending.append(("contact_insert", lead_id, phone))
        elif s.startswith("update public.contacts set phone=%s"):
            for c in db.contacts.values():
                if c["contact_id"] == params[1]:
                    c["phone"] = params[0]
        elif s.startswith("update public.contacts set phone=null"):
            for c in db.contacts.values():
                if c["contact_id"] == params[0]:
                    c["phone"] = None
        else:
            raise AssertionError(f"unexpected SQL: {s[:120]}")

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class FakeConn:
    def __init__(self, db):
        self.db = db
        db.begin()

    def __enter__(self):
        return self

    def __exit__(self, et, *a):
        if et is not None:
            self.db.rollback()
        return False

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()


@pytest.fixture()
def run(monkeypatch):
    """main(**kw) against a FakeDB on the efxo DSN; returns the db."""
    def _run(db, **kw):
        monkeypatch.setattr(sync, "DATABASE_URL", EFXO)
        monkeypatch.setattr(sync, "_git_sha", lambda: "testsha")
        with mock.patch.object(sync.psycopg, "connect", return_value=FakeConn(db)):
            sync.main(**kw)
        return db
    return _run


def _records(path):
    return [json.loads(ln) for ln in open(path) if ln.strip()]


# --- batch boundaries ------------------------------------------------------------------

def test_pages_are_contiguous_keyset_ranges_committed_one_at_a_time(run, tmp_path):
    db = FakeDB([_lead(i) for i in range(7)])
    cp = tmp_path / "cp.jsonl"
    run(db, batch_size=3, checkpoint_path=str(cp))
    recs = _records(cp)
    kinds = [r["kind"] for r in recs]
    assert kinds == ["run", "batch", "batch", "batch", "final"]
    ids = [str(r["lead_id"]) for r in db.leads]
    b = [r for r in recs if r["kind"] == "batch"]
    assert [(x["first_lead_id"], x["last_lead_id"]) for x in b] == [
        (ids[0], ids[2]), (ids[3], ids[5]), (ids[6], ids[6])]
    assert [x["rows_examined"] for x in b] == [3, 3, 1]
    assert [x["batch_no"] for x in b] == [1, 2, 3]
    assert all(x["git_sha"] == "testsha" and x["dsn_ref"] == "efxowqudmsfqjmbeidjc" for x in b)
    for key in ("rows_written", "rows_suppressed", "rows_skipped", "errors", "rss_bytes", "peak_rss_bytes", "run_id"):
        assert key in b[0]
    assert db.commits == 4                      # 3 pages + the tenant-wide bridge pass
    assert sorted(map(str, db.committed_updates)) == sorted(ids)   # each lead exactly once
    assert db.max_page == 3 and db.max_lookup == 3
    final = recs[-1]
    assert final["rows_examined"] == 7 and final["rows_written"] == 7 and final["batches"] == 3


def test_monolithic_mode_is_unchanged_one_transaction(run, tmp_path):
    db = FakeDB([_lead(i) for i in range(5)])
    run(db, checkpoint_path=str(tmp_path / "m.jsonl"))
    assert db.commits == 1 and db.max_page == 5
    recs = _records(tmp_path / "m.jsonl")
    assert [r["kind"] for r in recs] == ["run", "batch", "final"]
    assert recs[1]["batch_size"] is None and recs[1]["bridge"] is None


# --- resume after a kill / pause -----------------------------------------------------

def test_pause_then_resume_continues_after_last_committed_key(run, tmp_path):
    db = FakeDB([_lead(i) for i in range(7)])
    cp = tmp_path / "cp.jsonl"
    run(db, batch_size=3, checkpoint_path=str(cp), max_batches=1)
    recs = _records(cp)
    assert [r["kind"] for r in recs] == ["run", "batch", "pause"]
    assert db.commits == 1 and len(db.committed_updates) == 3
    first_run = recs[0]

    run(db, batch_size=3, checkpoint_path=str(cp), resume=True)
    recs = _records(cp)
    assert [r["kind"] for r in recs] == ["run", "batch", "pause", "batch", "batch", "final"]
    assert {r["run_id"] for r in recs} == {first_run["run_id"]}
    b = [r for r in recs if r["kind"] == "batch"]
    assert [x["batch_no"] for x in b] == [1, 2, 3]
    assert b[1]["first_lead_id"] == str(db.leads[3]["lead_id"])
    assert sorted(map(str, db.committed_updates)) == sorted(str(r["lead_id"]) for r in db.leads)
    assert db.commits == 4


def test_failed_page_rolls_back_only_itself_and_resume_retries_it(run, tmp_path):
    db = FakeDB([_lead(i) for i in range(7)])
    db.fail_update_of = db.leads[4]["lead_id"]         # inside page 2
    cp = tmp_path / "cp.jsonl"
    with pytest.raises(RuntimeError, match="injected failure"):
        run(db, batch_size=3, checkpoint_path=str(cp))
    recs = _records(cp)
    assert [r["kind"] for r in recs] == ["run", "batch", "error"]
    assert recs[-1]["batch_no"] == 2 and "injected failure" in recs[-1]["error"]
    assert db.commits == 1 and len(db.committed_updates) == 3      # page 1 stands; page 2 gone
    assert db.acq[db.leads[3]["lead_id"]]["phone_1"] is None         # rolled back, not half-applied

    db.fail_update_of = None
    run(db, batch_size=3, checkpoint_path=str(cp), resume=True)
    recs = _records(cp)
    assert [r["kind"] for r in recs] == ["run", "batch", "error", "batch", "batch", "final"]
    b = [r for r in recs if r["kind"] == "batch"]
    assert b[1]["batch_no"] == 2 and b[1]["first_lead_id"] == str(db.leads[3]["lead_id"])
    assert sorted(map(str, db.committed_updates)) == sorted(str(r["lead_id"]) for r in db.leads)


def test_resume_refuses_a_checkpoint_from_another_target_or_a_dry_run(run, tmp_path):
    cp = tmp_path / "cp.jsonl"
    cp.write_text(json.dumps({"kind": "run", "run_id": "x", "as_of": "t", "dsn_ref": "lvdimujkfheeyqchjzra"}) + "\n")
    with pytest.raises(SystemExit, match="targets lvdimujkfheeyqchjzra"):
        run(FakeDB([_lead(0)]), batch_size=1, checkpoint_path=str(cp), resume=True)
    cp.write_text(json.dumps({"kind": "run", "run_id": "x", "as_of": "t", "dsn_ref": "efxowqudmsfqjmbeidjc",
                              "dry_run": True}) + "\n")
    with pytest.raises(SystemExit, match="dry run"):
        run(FakeDB([_lead(0)]), batch_size=1, checkpoint_path=str(cp), resume=True)


def test_resume_of_a_finished_run_is_a_noop(run, tmp_path, capsys):
    db = FakeDB([_lead(i) for i in range(4)])
    cp = tmp_path / "cp.jsonl"
    run(db, batch_size=2, checkpoint_path=str(cp))
    n = db.commits
    run(db, batch_size=2, checkpoint_path=str(cp), resume=True)
    assert db.commits == n and "already FINAL" in capsys.readouterr().out


# --- idempotency: a retried / repeated page never duplicates ------------------------------

def test_repeating_a_completed_run_creates_no_duplicate_rows_or_contacts(run, tmp_path):
    clear = _lead(0, skip_trace_status="traced_web", confidence_score=0.9, dnc_status="clear",
                  phones=[{"number": "+15550000001", "type": "mobile", "confidence": 0.9}])
    disc = _lead(1, provenance="discovered_taxsale", raw_meta={"asset_class": "vacant_land"})
    plain = [_lead(i) for i in range(2, 6)]
    db = FakeDB([clear, disc] + plain, matched={clear["lead_id"]} | {r["lead_id"] for r in plain})
    run(db, batch_size=2, checkpoint_path=str(tmp_path / "a.jsonl"))
    assert db.committed_inserts == [disc["lead_id"]]                 # discovered tier inserted once
    assert len(db.acq) == 6 and len(db.contacts) == 1
    seller = db.contacts[db.acq[clear["lead_id"]]["lead_id"]]
    assert seller["phone"] == "+15550000001" and seller["source"] == "spine_sync"
    contact_id = seller["contact_id"]

    run(db, batch_size=2, checkpoint_path=str(tmp_path / "b.jsonl"))   # a second, independent run
    assert db.committed_inserts == [disc["lead_id"]]                 # no second INSERT
    assert len(db.acq) == 6 and len(db.contacts) == 1
    assert db.contacts[db.acq[clear["lead_id"]]["lead_id"]]["contact_id"] == contact_id
    recs = _records(tmp_path / "b.jsonl")
    assert recs[-1]["rows_inserted"] == 0 and recs[-1]["rows_updated"] == 6


def test_bridge_closes_inside_every_committed_page(run, tmp_path):
    callable_ = _lead(0, skip_trace_status="traced_paid", confidence_score=0.8, dnc_status="clear",
                      phones=[{"number": "+15550000002", "type": "landline", "confidence": 0.8}])
    db = FakeDB([callable_] + [_lead(i) for i in range(1, 5)])
    run(db, batch_size=2, checkpoint_path=str(tmp_path / "cp.jsonl"))
    acq_id = db.acq[callable_["lead_id"]]["lead_id"]
    # at the FIRST commit the callable lead already had its seller send-target row
    assert db.contacts_at_commit[0][acq_id]["phone"] == "+15550000002"
    recs = _records(tmp_path / "cp.jsonl")
    assert recs[1]["bridge"] == {"created": 1}
    assert recs[2]["bridge"] == {}                # page 2 had no bridge candidates
    assert recs[-1]["bridge_final"] == {"left": 1}   # tenant-wide pass keeps, never clobbers


# --- gates are the same gates in batched mode ----------------------------------------------

def test_batched_mode_projects_exactly_the_canonical_gates(run, tmp_path):
    L = {
        "callable": _lead(0, skip_trace_status="traced_web", confidence_score=0.9, dnc_status="clear",
                          phones=[{"number": "+15550000010", "type": "mobile", "confidence": 0.9},
                                  {"number": "+15550000011", "type": "landline", "confidence": 0.5}]),
        "null_conf": _lead(1, skip_trace_status="traced_web", confidence_score=None, dnc_status="clear",
                           phones=[{"number": "+15550000012", "type": "mobile", "confidence": 0.9}]),
        "low_conf": _lead(2, skip_trace_status="traced_paid", confidence_score=0.59, dnc_status="clear",
                          phones=[{"number": "+15550000013", "type": "mobile", "confidence": 0.9}]),
        "dnc_unknown": _lead(3, skip_trace_status="traced_paid", confidence_score=0.95, dnc_status="unchecked",
                             phones=[{"number": "+15550000014", "type": "mobile", "confidence": 0.9}]),
        "dnc_true": _lead(4, skip_trace_status="traced_paid", confidence_score=0.95, dnc_status="do_not_call",
                          phones=[{"number": "+15550000015", "type": "mobile", "confidence": 0.9}]),
        "not_traced": _lead(5, skip_trace_status="not_traced", confidence_score=0.99, dnc_status="clear",
                            phones=[{"number": "+15550000016", "type": "mobile", "confidence": 0.9}]),
        "receipt_unclean": _lead(6, skip_trace_status="traced_web", confidence_score=0.9, dnc_status="clear",
                                 scrub_receipt={"x": 1},
                                 phones=[{"number": "+15550000017", "type": "mobile", "confidence": 0.9,
                                          "dnc_status": "clear"},
                                         {"number": "+15550000018", "type": "mobile", "confidence": 0.95}]),
    }
    db = FakeDB(list(L.values()))
    run(db, batch_size=3, checkpoint_path=str(tmp_path / "cp.jsonl"))

    def proj(name):
        p = db.update_params[L[name]["lead_id"]]
        return {"p1": p[6], "p1t": p[7], "p2": p[8], "conf": p[9], "dnc": p[10]}

    assert proj("callable") == {"p1": "+15550000010", "p1t": "mobile", "p2": "+15550000011", "conf": 0.9, "dnc": False}
    assert proj("null_conf") == {"p1": None, "p1t": None, "p2": None, "conf": None, "dnc": False}   # NULL never HIGH
    assert proj("low_conf")["p1"] is None
    assert proj("dnc_unknown") == {"p1": None, "p1t": None, "p2": None, "conf": None, "dnc": None}  # unknown stays unknown
    assert proj("dnc_true") == {"p1": None, "p1t": None, "p2": None, "conf": None, "dnc": True}
    assert proj("not_traced")["p1"] is None                                  # PHONE_PRESENT != CALLABLE
    assert proj("receipt_unclean")["p1"] == "+15550000017" and proj("receipt_unclean")["p2"] is None
    final = _records(tmp_path / "cp.jsonl")[-1]
    assert final["rows_suppressed"] == 1 and final["phones_projected"] == 2
    assert len(db.contacts) == 2                                              # one seller row per callable lead


def test_gate_source_is_the_same_text_in_batched_main():
    text = __import__("inspect").getsource(sync.main)
    assert "projected_dnc is False" in text
    assert 'r["skip_trace_status"] in ("traced_web", "traced_paid")' in text
    assert 'float(r["confidence_score"]) >= THRESHOLD' in text
    assert "when dnc is true then true" in text
    assert text.count("update public.acquisition_leads set") == 1     # one projection statement, one writer


# --- memory: no whole-dataset accumulation ----------------------------------------------

def test_pages_bound_fetch_lookup_and_preview_sizes(run, tmp_path, capsys):
    db = FakeDB([_lead(i, ch_name=f"Owner {i % 4} Smith") for i in range(50)])
    run(db, batch_size=7, checkpoint_path=str(tmp_path / "cp.jsonl"), dry_run=True)
    assert db.max_page == 7 and db.max_lookup == 7
    out = capsys.readouterr().out
    assert "50 composed" in out and out.count("headline:") == 5      # preview = 5, never 50
    assert db.commits == 0                                             # dry run writes nothing
    recs = _records(tmp_path / "cp.jsonl")
    assert recs[-1]["kind"] == "dry_run_totals" and recs[-1]["briefs_composed"] == 50
    assert db.update_params == {}                                      # dry run: no UPDATE issued


def test_hub_labels_are_whole_set_even_when_a_hub_spans_pages(run, tmp_path):
    # 2 parcels for the same controlling human, in different pages -> both stamped "hub: 2 parcels"
    a = _lead(0, ch_name="PETER S COLOVOS")
    fill = [_lead(i) for i in range(1, 4)]
    b = _lead(4, ch_name="Peter Colovos")
    db = FakeDB([a] + fill + [b])
    run(db, batch_size=2, checkpoint_path=str(tmp_path / "cp.jsonl"))
    assert db.update_params[a["lead_id"]][3] == "Peter S Colovos — hub: 2 parcels"
    assert db.update_params[b["lead_id"]][3] == "Peter S Colovos — hub: 2 parcels"


def test_rss_over_envelope_halves_the_next_page(run, tmp_path, monkeypatch):
    db = FakeDB([_lead(i) for i in range(20)])
    monkeypatch.setattr(sync, "_rss_bytes", lambda: 10 * 1024 ** 3)
    monkeypatch.setattr(sync, "MIN_BATCH_SIZE", 2)
    run(db, batch_size=8, checkpoint_path=str(tmp_path / "cp.jsonl"), rss_envelope_bytes=1024)
    sizes = [r["batch_size"] for r in _records(tmp_path / "cp.jsonl") if r["kind"] == "batch"]
    assert sizes[:3] == [8, 4, 2]                     # adapts down, never below the floor
    assert sum(r["rows_examined"] for r in _records(tmp_path / "cp.jsonl") if r["kind"] == "batch") == 20


# --- the write guard holds in batched mode ---------------------------------------------------

def test_batched_write_on_lvdi_is_refused_before_connecting(monkeypatch):
    monkeypatch.setattr(sync, "DATABASE_URL", LVDI)
    with mock.patch.object(sync.psycopg, "connect") as connect:
        with pytest.raises(sync.LegacyCrmWriteRefused):
            sync.main(batch_size=10, checkpoint_path="/dev/null")
        connect.assert_not_called()


def test_scoped_bridge_queries_wrap_the_unchanged_constants():
    assert sync._scoped(CALLABLE_OR_SUPPRESSED, None) == (CALLABLE_OR_SUPPRESSED, (sync.TENANT,))
    sql, params = sync._scoped(ORPHAN_CALLABLE, ["a", "b"])
    assert ORPHAN_CALLABLE in sql and sql.strip().startswith("select * from (") and params == (sync.TENANT, ["a", "b"])

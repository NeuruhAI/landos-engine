"""
test_recipe.py — RECIPE-2 engine composer: the deterministic parser, the
match/pending/out counting semantics, the compose bodies, and the decide
lifecycle guards. DB-backed cases skip (never false-green) when the spine is
unreachable and roll back every write.
"""
import pytest

from app import recipe_core as rc


LIVE_COUNTIES = {"26021": "Berrien County", "26077": "Kalamazoo County",
                 "48491": "Williamson County"}
LIVE_TAGS = ["homeowner_direct"]


# ---- parser (pure, deterministic, never guesses) ---------------------------

def test_parse_extracts_full_filter():
    f, q, matched = rc.parse_goal(
        "Berrien discovered parcels score 60+ with $5k spread, need 50",
        live_counties=LIVE_COUNTIES, live_tags=LIVE_TAGS)
    assert f["counties"] == ["26021"]
    assert f["statuses"] == ["discovered"]
    assert f["min_score"] == 60.0
    assert f["min_spread"] == 5000.0
    assert f["target"] == 50
    assert q == []


def test_parse_county_by_state_and_fips():
    f, _, _ = rc.parse_goal("Williamson County, TX taxsale and 26077",
                            live_counties=LIVE_COUNTIES, live_tags=LIVE_TAGS)
    assert "48491" in f["counties"] and "26077" in f["counties"]


def test_parse_only_known_status_and_live_tags():
    f, _, _ = rc.parse_goal("frobnicated homeowner_direct leads",
                            live_counties=LIVE_COUNTIES, live_tags=LIVE_TAGS)
    assert f["statuses"] == []                    # 'frobnicated' is not a status
    assert f["signal_tags"] == ["homeowner_direct"]


def test_parse_vague_goal_asks_not_guesses():
    f, q, _ = rc.parse_goal("make me rich", live_counties=LIVE_COUNTIES, live_tags=LIVE_TAGS)
    assert not any([f["counties"], f["statuses"], f["signal_tags"],
                    f["min_score"], f["min_spread"]])
    assert len(q) == 1 and "couldn't extract a filter" in q[0]


def test_parse_money_forms():
    assert rc._parse_money("$5k") == 5000
    assert rc._parse_money("5,000") == 5000
    assert rc._parse_money("2.5k") == 2500
    assert rc._parse_money("nope") is None


# ---- DB-backed: counting, compose, decide lifecycle ------------------------

@pytest.fixture()
def conn():
    psycopg = pytest.importorskip("psycopg")
    from psycopg.rows import dict_row
    from app.config import DATABASE_URL
    try:
        c = psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"database unreachable: {e}")
    yield c
    c.rollback()   # nothing a test does is ever committed
    c.close()


def test_count_acquire_match_pending_semantics(conn):
    # a status-only filter: every discovered row is a clean match (status is
    # HARD/known), none pending (no soft criterion to be un-enriched).
    with conn.cursor() as cur:
        counts = rc.count_acquire(cur, {"counties": [], "statuses": ["discovered"],
                                        "signal_tags": [], "min_score": None,
                                        "min_spread": None, "target": None})
    assert counts["available_now"] >= 0 and counts["pending"] == 0
    assert counts["hard_pass"] == counts["available_now"]


def test_min_spread_makes_rows_pending_not_missed(conn):
    # spread inputs are universally NULL on the discovered tier today, so a
    # min_spread filter makes matched-status rows PENDING, never a fake miss.
    with conn.cursor() as cur:
        counts = rc.count_acquire(cur, {"counties": [], "statuses": ["discovered"],
                                        "signal_tags": [], "min_score": None,
                                        "min_spread": 5000.0, "target": None})
    assert counts["available_now"] == 0
    assert counts["pending"] == counts["hard_pass"] >= 1


def test_compose_acquire_grounded_and_synced(conn):
    with conn.cursor() as cur:
        pid, body = rc.compose_and_sync(cur, "acquire", "Berrien discovered parcels")
        assert body["status"] == "proposed"
        assert body["gate"]["verdict"] in ("pass", "warn")
        assert body["expected_yield"]["available_now"] is not None
        assert body["rationale_hash"] and body["engine_receipt_hash"]
        # the row is really in the projection (is_fixture false)
        cur.execute("select is_fixture, composed_by from public.recipe_proposals_view where proposal_id=%s", (pid,))
        row = cur.fetchone()
        assert row["is_fixture"] is False and "composer" in row["composed_by"]
    conn.rollback()


def test_compose_vague_syncs_needs_clarification(conn):
    with conn.cursor() as cur:
        pid, body = rc.compose_and_sync(cur, "acquire", "just do something good")
        assert body["status"] == "needs_clarification"
        assert body["needs_clarification"]
        cur.execute("select status from public.recipe_proposals_view where proposal_id=%s", (pid,))
        assert cur.fetchone()["status"] == "needs_clarification"
    conn.rollback()


def test_approve_mints_recipe_and_run_single_writer(conn):
    with conn.cursor() as cur:
        pid, _ = rc.compose_and_sync(cur, "acquire", "Berrien discovered parcels")
        res = rc.decide(cur, pid, "approve", decided_by=None)
        assert res["status"] == "approved" and res["recipe_id"] and res["run_id"]
        cur.execute("select source, goal_type, proposal_id, rationale_hash from public.hunt_recipes where id=%s",
                    (res["recipe_id"],))
        r = cur.fetchone()
        assert r["source"] == "composed" and str(r["proposal_id"]) == pid and r["rationale_hash"]
        cur.execute("select status, stages from public.recipe_runs_view where run_id=%s", (res["run_id"],))
        run = cur.fetchone()
        assert run["status"] == "complete" and len(run["stages"]) == 4
        assert run["stages"][0]["in_count"] >= run["stages"][-1]["out_count"]  # funnel narrows
    conn.rollback()


def test_reject_flips_status_no_recipe(conn):
    with conn.cursor() as cur:
        pid, _ = rc.compose_and_sync(cur, "acquire", "Berrien discovered parcels")
        res = rc.decide(cur, pid, "reject", decided_by=None)
        assert res["status"] == "rejected" and "recipe_id" not in res
        cur.execute("select status, decided_at from public.recipe_proposals_view where proposal_id=%s", (pid,))
        row = cur.fetchone()
        assert row["status"] == "rejected" and row["decided_at"] is not None
    conn.rollback()


def test_decide_twice_is_rejected(conn):
    with conn.cursor() as cur:
        pid, _ = rc.compose_and_sync(cur, "acquire", "Berrien discovered parcels")
        rc.decide(cur, pid, "approve", decided_by=None)
        with pytest.raises(ValueError, match="already"):
            rc.decide(cur, pid, "reject", decided_by=None)
    conn.rollback()


def test_dispose_empty_inventory_blocked(conn):
    with conn.cursor() as cur:
        _, body = rc.compose_and_sync(cur, "dispose", "sell nothing",
                                      {"inventory": [], "params": {}})
        assert body["status"] == "needs_clarification"
        assert body["gate"]["verdict"] == "blocked"
    conn.rollback()


def test_dispose_valid_inventory_warns_no_buyers(conn):
    with conn.cursor() as cur:
        cur.execute("select lead_id, county_fips from public.acquisition_leads where tenant_id=%s limit 2",
                    (rc.TENANT,))
        picks = [{"lead_id": str(r["lead_id"]), "disposition_id": "d",
                  "county_fips": r["county_fips"]} for r in cur.fetchall()]
        if not picks:
            pytest.skip("no queue leads to dispose")
        _, body = rc.compose_and_sync(cur, "dispose", "sell these",
                                      {"inventory": picks, "params": {"target_price_usd": 50000}})
        assert body["status"] == "proposed"
        assert body["gate"]["verdict"] == "warn"   # no buyer book yet
        assert body["expected_yield"]["available_now"] == len(picks)
    conn.rollback()

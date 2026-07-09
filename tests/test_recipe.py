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


# ---- DISTRESS recipe (Path C): tax-delinquent + high-equity, honest --------

def test_distress_spec_defaults_and_overrides():
    s = rc.distress_spec()
    assert s["tax_delinquent"] is True and s["equity_pct_min"] == 70.0
    assert s["equity_override_pct"] == 60.0
    assert s["rank_by"] == rc.DISTRESS_DIMENSIONS
    # override only replaces named keys; None is ignored (never invents)
    s2 = rc.distress_spec(equity_pct_min=55, rank_by=None)
    assert s2["equity_pct_min"] == 55 and s2["rank_by"] == rc.DISTRESS_DIMENSIONS
    # dropping the override clause via 0 (falsy) does not fall back to default
    s3 = rc.distress_spec(equity_override_pct=0)
    assert s3["equity_override_pct"] == 0


def test_distress_count_equity_computed_from_provenance(conn):
    # Phase B b1: equity computes live from tax-sale provenance (SEV×2 − min_bid),
    # so real ≥floor targets exist and the F1 set partitions cleanly.
    with conn.cursor() as cur:
        c = rc.count_distress(cur, rc.distress_spec())
    assert c["clean_match"] >= 1                        # real ≥70% targets exist now
    assert c["pending"] >= 0                            # only rows with no SEV / uncalibrated state
    # every tax-delinquent row lands in exactly one bucket (no double count, no leak)
    assert (c["clean_match"] + c["pending"] + c["override_band"] + c["below_floor"]
            == c["tax_delinquent_n"])
    assert c["scoped"] >= c["tax_delinquent_n"]


def test_distress_dropping_equity_makes_f1_clean(conn):
    # with NO equity floor, tax-delinquent rows are clean matches (F1 alone).
    with conn.cursor() as cur:
        c = rc.count_distress(cur, rc.distress_spec(equity_pct_min=0))
    # equity_min 0 is falsy in the spec factory -> treated as "no floor"
    assert c["clean_match"] == c["tax_delinquent_n"] >= 1
    assert c["pending"] == 0


def test_distress_coverage_is_measured_not_assumed(conn):
    with conn.cursor() as cur:
        cov = rc.distress_coverage(cur, rc.DISTRESS_DIMENSIONS)
    dims = {c["dimension"]: c for c in cov}
    assert set(dims) == set(rc.DISTRESS_DIMENSIONS)
    # every entry carries a real measured count and a present flag
    for c in cov:
        assert c["rows"] >= 0 and c["present"] == (c["rows"] > 0)


def test_absentee_lights_from_parcel_meta(conn):
    # b2 quick-win: absentee is carried on parcel_meta.owner_out_of_state (~450 MI
    # leads) — $0, in-spine. distress_coverage must count it as present.
    with conn.cursor() as cur:
        cov = {c["dimension"]: c for c in rc.distress_coverage(cur, ["absentee", "probate"])}
    assert cov["absentee"]["rows"] >= 1 and cov["absentee"]["present"] is True


def test_compose_distress_honest_body_and_punch_list(conn):
    with conn.cursor() as cur:
        body = rc.compose_distress(cur, rank_limit=5)
    assert body["goal_type"] == "acquire"          # renders in the RECIPE-2 card
    assert body["status"] in ("proposed", "needs_clarification")
    assert body["gate"]["verdict"] in ("pass", "warn")
    # F1 is real; equity surfaced as pending in the yield gap_note
    assert body["expected_yield"]["available_now"] >= 0
    assert "pending" in body["expected_yield"]["gap_note"]
    # the punch_list names the equity gap AND the approve-persistence gap
    clauses = " ".join(p["clause"] for p in body["punch_list"]).lower()
    assert "equity" in clauses and "persistence" in clauses
    assert len(body["rank_preview"]) <= 5
    # rank axis is auto-selected: the first candidate that actually ORDERS the
    # set (>= 2 distinct). score_distress is flat today, so it must not be chosen.
    ax = body["rank_axis"]
    assert ax["column"] is None or ax["distinct"] >= 2
    # PRIMARY rank is distress-stack depth (non-increasing), then the numeric axis
    stacks = [r["stack_score"] for r in body["rank_preview"]]
    assert stacks == sorted(stacks, reverse=True)
    # every previewed lead is a real target (clean equity ≥ floor) and reports its stack
    for r in body["rank_preview"]:
        assert r["stack_score"] == len(r["stack_dims"])
        assert r["equity_pct"] is None or r["equity_pct"] >= body["filter"]["equity_pct_min"]


def test_compose_distress_sync_writes_real_proposal(conn):
    with conn.cursor() as cur:
        body = rc.compose_distress(cur)
        pid, body = rc.sync_proposal(cur, body)
        assert body["rationale_hash"] and body["engine_receipt_hash"]
        cur.execute("select is_fixture, composed_by, goal_type from public.recipe_proposals_view "
                    "where proposal_id=%s", (pid,))
        row = cur.fetchone()
        assert row["is_fixture"] is False and "composer" in row["composed_by"]
        assert row["goal_type"] == "acquire"
    conn.rollback()   # never commit


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

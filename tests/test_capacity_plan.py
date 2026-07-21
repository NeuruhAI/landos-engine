"""
test_capacity_plan.py — P-27: the 30k dry-run capacity planner (pure, fixture-only).

The structural claims are the important ones: the planner has no execution power
(no network imports, constant-unapproved gate), its economics mirror the live
tracer's constants (drift-pinned), and the plan is deterministic.
"""
import pytest

from app.capacity_plan import (DEFAULT_ASSUMPTIONS, TRACE_COST_PER_CREDIT,
                               plan_canonical, plan_capacity, render_plan)


def test_thirty_k_plan_numbers():
    plan = plan_capacity(41_000, target=30_000, batch_size=2_000)
    assert plan["planned"] == 30_000 and plan["shortfall"] == 0
    assert plan["batches"]["count"] == 15
    assert plan["batches"]["sizes"] == [2_000] * 15
    assert plan["expected_hits"] == {"min": 18_000, "max": 25_500}   # 60%–85%
    assert plan["phone_volume"] == {"min": 18_000, "max": 76_500}    # ×1.0 / ×3.0
    s = plan["spend_usd"]
    assert s["trace"] == {"min": 360.0, "max": 600.0}                # misses free floor
    assert s["dnc_scrub"]["min"] == 360.0 and s["dnc_scrub"]["max"] == 1_530.0
    assert s["total"] == {"min": 720.0, "max": 2_130.0}


def test_pool_smaller_than_target_reports_shortfall_honestly():
    plan = plan_capacity(7_500, target=30_000, batch_size=2_000)
    assert plan["planned"] == 7_500 and plan["shortfall"] == 22_500
    assert plan["batches"]["sizes"] == [2_000, 2_000, 2_000, 1_500]


def test_advanced_trace_doubles_credits():
    n, a = (plan_capacity(1_000, trace_type=t) for t in ("normal", "advanced"))
    assert a["credits_per_record"] == 2 * n["credits_per_record"]
    assert a["spend_usd"]["trace"]["max"] == 2 * n["spend_usd"]["trace"]["max"]


def test_no_dnc_zeroes_that_lane_only():
    plan = plan_capacity(1_000, dnc=False)
    assert plan["spend_usd"]["dnc_scrub"]["min"] == 0.0
    assert plan["spend_usd"]["dnc_scrub"]["max"] == 0.0
    assert plan["spend_usd"]["total"] == plan["spend_usd"]["trace"]


def test_assumptions_are_configurable_and_echoed():
    plan = plan_capacity(10_000, assumptions={"hit_rate_range": (0.5, 0.7)})
    assert plan["expected_hits"] == {"min": 5_000, "max": 7_000}
    assert plan["assumptions"]["hit_rate_range"] == [0.5, 0.7]
    assert plan["assumptions"]["misses_free"] is True


def test_bad_inputs_fail_loud():
    with pytest.raises(ValueError):
        plan_capacity(-1)
    with pytest.raises(ValueError):
        plan_capacity(100, target=0)
    with pytest.raises(ValueError):
        plan_capacity(100, batch_size=0)
    with pytest.raises(ValueError):
        plan_capacity(100, trace_type="premium")
    with pytest.raises(ValueError):
        plan_capacity(100, assumptions={"hit_rate_range": (0.9, 0.2)})


# ---- structural mint/spend safety ------------------------------------------

def test_approval_gate_is_constant_and_names_the_keystroke():
    plan = plan_capacity(30_000)
    gate = plan["approval_gate"]
    assert gate["approved"] is False
    assert "skip_trace_tracerfy" in gate["required_keystroke"]
    assert "--confirm" in gate["required_keystroke"]


def test_planner_module_has_no_execution_power():
    # AST-level guard: the pure planner may import nothing that can reach a network,
    # a database, or a subprocess — no matter what its prose says.
    import ast

    import app.capacity_plan as cp
    tree = ast.parse(open(cp.__file__).read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            imported.add((node.module or "").split(".")[0])
    forbidden = {"urllib", "requests", "httpx", "socket", "psycopg", "psycopg2",
                 "subprocess", "os", "http"}
    assert not (imported & forbidden), f"planner imports execution power: {imported & forbidden}"
    assert '"approved": False' in open(cp.__file__).read()  # the gate is a literal


def test_cost_constant_pinned_to_the_live_tracer():
    # drift guard: if Tracerfy pricing changes in the tracer, the planner must move too
    from scripts.skip_trace_tracerfy import COST_PER_CREDIT
    assert TRACE_COST_PER_CREDIT == COST_PER_CREDIT


def test_cli_pool_count_predicate_mirrors_the_tracer():
    # the plan's pool must be the tracer's eligibility, not a broader (flattering) count
    from scripts.plan_skiptrace_capacity import POOL_COUNT
    from scripts.skip_trace_tracerfy import TARGET_QUERY
    for term in ("owner_type = 'individual'",
                 "coalesce(skip_trace_status,'not_traced') = 'not_traced'",
                 "not coalesce(discovery_seed, false)",
                 "owner_name not ilike all",
                 "(owner_mailing_address is not null or situs_address is not null)"):
        assert term in POOL_COUNT and term in TARGET_QUERY
    low = POOL_COUNT.lower()
    assert "update" not in low and "insert" not in low and "delete" not in low


def test_plan_is_deterministic():
    a = plan_capacity(12_345, per_county={"18141": 8_000, "26021": 4_345})
    b = plan_capacity(12_345, per_county={"26021": 4_345, "18141": 8_000})
    assert plan_canonical(a) == plan_canonical(b)
    assert a["receipt_sha256"] == b["receipt_sha256"]
    assert plan_capacity(12_346)["receipt_sha256"] != a["receipt_sha256"]


def test_render_names_the_gate_and_the_ranges():
    out = render_plan(plan_capacity(41_000, target=30_000,
                                    per_county={"18141": 41_000}))
    assert "OPERATOR APPROVAL GATE" in out
    assert "skip_trace_tracerfy --batch --confirm" in out
    assert "18,000 – 25,500" in out
    assert "$720.00 – $2,130.00" in out
    assert "[18141]=41,000" in out


def test_default_assumptions_are_the_documented_ranges():
    assert DEFAULT_ASSUMPTIONS["hit_rate_range"] == (0.60, 0.85)
    assert DEFAULT_ASSUMPTIONS["phones_per_hit_range"] == (1.0, 3.0)
    assert TRACE_COST_PER_CREDIT == 0.02

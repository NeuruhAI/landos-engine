"""
service.py — orchestrate one insight: ground -> cache -> route -> meter -> model -> cache.

Contract to the endpoint:
  * returns a dict {brief, angle, do_not_say, grounding, model, generated_at, cached}
  * returns None when the lead does not resolve (endpoint -> 404)
  * raises InsightUnavailable(reason, status) when the meter/kill-switch/model say so
    (endpoint -> honest-offline 503) — NEVER a fabricated brief.

Honesty invariants: do_not_say is ALWAYS the deterministic rails (never the model). An empty
grounding yields an empty brief with no model spend. A cached brief is reused until a grounded
input changes (hash), so the model is never paid to reproduce the same answer.
"""
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from .. import config
from . import grounding as grounding_mod
from . import compose as compose_mod
from . import meter as meter_mod
from . import model as model_mod


class InsightUnavailable(Exception):
    def __init__(self, reason, status=503):
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _cache_get(cur, acq_lead_id):
    cur.execute("""select grounding_hash, brief, model, generated_at
                     from landos.insight_cache where lead_id = %s""", (acq_lead_id,))
    return cur.fetchone()


def _cache_put(cur, acq_lead_id, ghash, result):
    cur.execute(
        """insert into landos.insight_cache (lead_id, grounding_hash, brief, model, cost_usd)
           values (%s, %s, %s, %s, %s)
           on conflict (lead_id) do update set
             grounding_hash = excluded.grounding_hash, brief = excluded.brief,
             model = excluded.model, cost_usd = excluded.cost_usd, generated_at = now()
           returning generated_at""",
        (acq_lead_id, ghash,
         Jsonb({k: result[k] for k in ("brief", "angle", "do_not_say", "grounding")}),
         result["model"], result["cost_usd"]))
    return cur.fetchone()["generated_at"]


def generate_insight(cur, lead_id, *, force=False, allow_paid=True):
    g = grounding_mod.build(cur, lead_id)
    if g is None:
        return None
    acq_id = g["acq_lead_id"]
    deterministic = compose_mod.compose(g)
    det_result = {
        "brief": deterministic["brief"], "angle": deterministic["angle"],
        "do_not_say": deterministic["do_not_say"], "grounding": g["facts"],
    }

    # empty (thin record) — a correct output. No model, no spend.
    if compose_mod.is_empty(deterministic):
        gen_at = _cache_put(cur, acq_id, g["hash"], {**det_result, "model": "deterministic", "cost_usd": 0})
        return {**det_result, "model": "deterministic", "generated_at": gen_at.isoformat(),
                "cached": False, "empty": True}

    # cache hit — grounded inputs unchanged.
    cached = _cache_get(cur, acq_id)
    if cached and cached["grounding_hash"] == g["hash"] and not force:
        b = cached["brief"]
        return {"brief": b.get("brief", ""), "angle": b.get("angle", ""),
                "do_not_say": b.get("do_not_say", []), "grounding": b.get("grounding", {}),
                "model": cached["model"], "generated_at": cached["generated_at"].isoformat(),
                "cached": True}

    tier = model_mod.route(g)
    if not allow_paid:
        tier = "ollama"
    system, user = model_mod.prompt_mod.build(g, deterministic)
    est = model_mod.estimate_cost(system, user, tier)

    try:
        meter_mod.check(cur, est)                       # kill switch + per-run + monthly caps
    except meter_mod.BudgetExceeded as e:
        raise InsightUnavailable(e.reason, status=503)

    try:
        parts, model_name, cost, tin, tout = model_mod.generate(g, deterministic, tier)
    except model_mod.ModelOffline as e:
        if config.INSIGHT_FALLBACK_DETERMINISTIC:
            # Serve the honest grounded floor, but do NOT cache or charge — a real model
            # gets its chance on the next call once it is reachable (no poisoned cache).
            return {**det_result, "model": "deterministic",
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "cached": False, "degraded": True}
        raise InsightUnavailable(f"insight model offline: {e}", status=503)

    # do_not_say is ALWAYS the deterministic rails; the model only shapes what_we_know/angle.
    result = {
        "brief": parts["what_we_know"] or deterministic["brief"],
        "angle": parts["angle"] or deterministic["angle"],
        "do_not_say": deterministic["do_not_say"],
        "grounding": g["facts"],
        "model": model_name, "cost_usd": cost,
    }
    meter_mod.charge(cur, lead_id=acq_id, model=model_name, cost_usd=cost,
                     tokens_in=tin, tokens_out=tout)
    gen_at = _cache_put(cur, acq_id, g["hash"], result)
    return {"brief": result["brief"], "angle": result["angle"],
            "do_not_say": result["do_not_say"], "grounding": result["grounding"],
            "model": model_name, "generated_at": gen_at.isoformat(), "cached": False}

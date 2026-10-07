"""
meter.py — the spend meter. The paid model never runs without passing this gate, and every
generation (paid or $0) is recorded, so the accounting is complete and visible.

Gates (any trip => BudgetExceeded, surfaced by the endpoint as an honest 503):
  * kill switch  (INSIGHT_KILL_SWITCH)         — hard off.
  * per-run cap  (INSIGHT_PER_RUN_CAP_USD)     — no single call may exceed it.
  * monthly cap  (INSIGHT_MONTHLY_CAP_USD)     — sum(cost_usd) this calendar month + est.
"""
from .. import config


class BudgetExceeded(Exception):
    """Raised when a gate trips. `.reason` is the honest-offline message for the client."""
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def month_spend(cur):
    """Total $ spent this calendar month (UTC), from the ledger."""
    cur.execute("""select coalesce(sum(cost_usd), 0) as s from landos.insight_spend
                   where spent_at >= date_trunc('month', now())""")
    return float(cur.fetchone()["s"])


def check(cur, est_cost):
    """Raise BudgetExceeded if this estimated-cost generation may not proceed. est_cost=0
    (local model) still respects the kill switch but never trips the $ caps."""
    if config.INSIGHT_KILL_SWITCH:
        raise BudgetExceeded("insight generation disabled (kill switch)")
    if est_cost <= 0:
        return
    if est_cost > config.INSIGHT_PER_RUN_CAP_USD:
        raise BudgetExceeded(
            f"insight per-run cap reached (est ${est_cost:.4f} > ${config.INSIGHT_PER_RUN_CAP_USD:.2f})")
    if month_spend(cur) + est_cost > config.INSIGHT_MONTHLY_CAP_USD:
        raise BudgetExceeded("insight budget reached")


def charge(cur, *, lead_id, model, cost_usd, tokens_in=None, tokens_out=None):
    """Append the ledger row. Called after a successful generation (paid OR $0)."""
    cur.execute(
        """insert into landos.insight_spend (lead_id, model, cost_usd, tokens_in, tokens_out)
           values (%s, %s, %s, %s, %s)""",
        (lead_id, model, cost_usd, tokens_in, tokens_out))


def summary(cur):
    """The visible meter — for the receipt and any /health-style surface."""
    spent = month_spend(cur)
    cur.execute("select count(*) n from landos.insight_spend where spent_at >= date_trunc('month', now())")
    n = cur.fetchone()["n"]
    return {
        "month_spend_usd": round(spent, 4),
        "monthly_cap_usd": config.INSIGHT_MONTHLY_CAP_USD,
        "remaining_usd": round(max(0.0, config.INSIGHT_MONTHLY_CAP_USD - spent), 4),
        "per_run_cap_usd": config.INSIGHT_PER_RUN_CAP_USD,
        "kill_switch": config.INSIGHT_KILL_SWITCH,
        "generations_this_month": n,
    }

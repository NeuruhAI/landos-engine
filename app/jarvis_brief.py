"""
jarvis_brief.py — the per-lead "Jarvis brief": the structured story behind the score.

PURE composition (no LLM, no DB, no network) — assembled from data already in landos
and projected to the CRM for display (engine composes; CRM only shows it). The
on-demand LLM "play on the play" narrative is a LATER, separately-metered step
(STEP C) — here `narrative` is always null.

HONESTY: MISSING-as-NULL throughout. `final_lead_score` (the absentee/reachability
rank) is NOT in the brief — the brief carries the COMPOSITE axes (owner/distress/
signals/arbitrage) separately and labels them as the composite; the two are never
conflated. arbitrage stays null when comps=0 — never a priced 0.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

JARVIS_VERSION = "jarvis_v1"


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt(x: Optional[float]) -> str:
    if x is None:
        return "—"                      # honest MISSING, never 0
    return str(int(x)) if float(x).is_integer() else str(round(x, 1))


def compose_brief(facts: Dict[str, Any], as_of: str) -> Dict[str, Any]:
    """
    facts (flat, MISSING -> None):
      controlling_human, portfolio_hub, recipe, owner_type, owner_name, parcel_apn,
      score_owner, score_distress, score_signals (dict|None), score_arbitrage (None
      when comps=0), alt_use_fitness (num|None), requires_bea (bool),
      contamination_kind ('part201'|'brownfield'|None), kin (list).
    """
    o, d = _num(facts.get("score_owner")), _num(facts.get("score_distress"))
    arb = _num(facts.get("score_arbitrage"))            # None when comps=0 — never 0
    auf = _num(facts.get("alt_use_fitness"))
    bea = bool(facts.get("requires_bea"))
    kind = facts.get("contamination_kind")
    ch, ot = facts.get("controlling_human"), facts.get("owner_type")
    kin = facts.get("kin") or []

    owner_label = ch or facts.get("owner_name") or ot or "owner"
    bea_tag = f" · BEA:{kind}" if bea else ""
    hub_tag = " · portfolio-hub" if facts.get("portfolio_hub") else ""
    kin_tag = f" · {len(kin)} kin" if kin else ""
    headline = (f"{owner_label} ({ot or '?'}) · {facts.get('recipe') or 'no-recipe'} · "
                f"composite O{_fmt(o)}/D{_fmt(d)}/A{_fmt(arb)} · alt-use {_fmt(auf)}"
                f"{bea_tag}{hub_tag}{kin_tag}")

    return {
        "version": JARVIS_VERSION,
        "composed_at": as_of,
        "controlling_human": ch,
        "portfolio_hub": facts.get("portfolio_hub"),
        "recipe": facts.get("recipe"),
        "owner_type": ot,
        "scores": {                                     # the COMPOSITE axes (NOT final_lead_score)
            "owner": o,
            "distress": d,
            "signals": facts.get("score_signals"),
            "arbitrage": arb,                           # null when comps=0
            "alt_use_fitness": auf,
        },
        "contamination": {"requires_bea": bea, "kind": kind},
        "kin": kin,
        "headline": headline,
        "narrative": None,                              # LLM fills on-demand later (STEP C)
    }

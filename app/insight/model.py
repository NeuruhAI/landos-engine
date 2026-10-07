"""
model.py — model routing. Ollama (Contabo, $0) for bulk / low-score; Claude API for
high-score / active-pipeline. stdlib HTTP only (no SDK dependency). A model that cannot be
reached raises ModelOffline -> the endpoint answers honest-offline, never a fabricated brief.
NEURUH IP: the routing + the model config never leave the engine.
"""
import json
import re
import urllib.error
import urllib.request

from .. import config
from . import prompt as prompt_mod


class ModelOffline(Exception):
    """The chosen model could not be reached (or returned unusable output)."""


def route(grounding):
    """'claude' for a high-score lead when the paid tier is configured; else 'ollama'."""
    if grounding.get("high_score") and config.ANTHROPIC_API_KEY:
        return "claude"
    return "ollama"


def estimate_cost(system, user, tier):
    """Pre-call $ estimate for the meter gate. Local = $0; Claude ~ chars/4 tokens in +
    a capped output budget out."""
    if tier != "claude":
        return 0.0
    est_in = (len(system) + len(user)) / 4.0
    est_out = 320.0
    return (est_in / 1e6) * config.INSIGHT_PRICE_IN_PER_MTOK + (est_out / 1e6) * config.INSIGHT_PRICE_OUT_PER_MTOK


def _parse_parts(text):
    """Pull {what_we_know, angle} out of a model response, tolerating stray prose."""
    if not text:
        raise ModelOffline("empty model response")
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ModelOffline("model returned no JSON")
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            raise ModelOffline("model returned unparseable JSON")
    return {"what_we_know": (obj.get("what_we_know") or "").strip(),
            "angle": (obj.get("angle") or "").strip()}


def _ollama(system, user):
    body = json.dumps({
        "model": config.OLLAMA_MODEL, "system": system, "prompt": user,
        "format": "json", "stream": False, "options": {"temperature": 0.2},
    }).encode()
    req = urllib.request.Request(config.OLLAMA_URL.rstrip("/") + "/api/generate",
                                 data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise ModelOffline(f"ollama unreachable: {e}")
    parts = _parse_parts(data.get("response", ""))
    return parts, f"ollama:{config.OLLAMA_MODEL}", 0.0, None, None


def _claude(system, user):
    body = json.dumps({
        "model": config.ANTHROPIC_MODEL, "max_tokens": 400, "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": config.ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise ModelOffline(f"anthropic unreachable: {e}")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage", {}) or {}
    tin, tout = usage.get("input_tokens"), usage.get("output_tokens")
    cost = ((tin or 0) / 1e6) * config.INSIGHT_PRICE_IN_PER_MTOK + \
           ((tout or 0) / 1e6) * config.INSIGHT_PRICE_OUT_PER_MTOK
    return _parse_parts(text), f"anthropic:{config.ANTHROPIC_MODEL}", round(cost, 6), tin, tout


def generate(grounding, deterministic, tier):
    """Run the chosen tier. Returns (parts, model_name, cost_usd, tokens_in, tokens_out).
    Raises ModelOffline if the model is unreachable or unusable."""
    system, user = prompt_mod.build(grounding, deterministic)
    if tier == "claude":
        return _claude(system, user)
    return _ollama(system, user)

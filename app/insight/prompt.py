"""
prompt.py — the LLM prompt, built ENTIRELY from the grounding. NEURUH IP.

The model is a rephraser, not a researcher: it is handed the real facts (and the
deterministic draft as a floor) and told to write natural prose using ONLY those facts.
It never sees the lead_id, the raw DB, or anything outside the grounding — there is
nothing to hallucinate FROM. do_not_say is not in the model's remit (compose.py owns it).
"""
import json

SYSTEM = (
    "You are a land-acquisition analyst writing a one-screen call brief for a VA. "
    "You will be given a fixed set of FACTS about a single parcel, each tagged with its "
    "source field, plus a plain-English draft. Rewrite the draft into tight, natural prose. "
    "HARD RULES: (1) Use ONLY the given facts. Introduce no number, place, motivation, or "
    "claim that is not in the facts. (2) If the facts are thin, the brief is short — that is "
    "correct; never pad. (3) No urgency, no pressure language, no invented backstory. "
    "(4) Return STRICT JSON: {\"what_we_know\": \"...\", \"angle\": \"...\"}. Nothing else. "
    "The angle is the opening a caller should take, derived only from these facts."
)


def _facts_block(facts):
    lines = []
    for k, fv in facts.items():
        lines.append(f"- {k} = {json.dumps(fv['value'], default=str)}  (from {fv['field']})")
    return "\n".join(lines)


def build(grounding, deterministic):
    """-> (system, user). user carries the facts + the deterministic draft as the floor."""
    facts = grounding.get("facts", {})
    user = (
        "FACTS (the only things you may state):\n"
        + _facts_block(facts)
        + "\n\nDRAFT (a correct, grounded floor — tighten it, do not exceed it):\n"
        + f"what_we_know: {deterministic.get('brief','')}\n"
        + f"angle: {deterministic.get('angle','')}\n\n"
        + "Return the strict JSON now."
    )
    return SYSTEM, user

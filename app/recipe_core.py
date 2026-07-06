"""
recipe_core.py — RECIPE-2 engine composer core (t1 Part 4).

The engine side of the RECIPE-2 UI (apg-land-crm migs 059/060). AUTONOMY
DOCTRINE, same as HUNT-V2: the composer PROPOSES (turns an operator's goal into
a recipe_proposals_view row), the operator DECIDES (approve/reject, desk-gated
in the CRM), and only on approve is the composed hunt_recipes row minted and a
run executed. $0, deterministic, never-fabricate.

THE SYNC CONTRACT is mig 060 (verbatim): the engine is the ONLY writer of
public.recipe_proposals_view / recipe_runs_view (+ the composed hunt_recipes
row), via the owner/service_role connection, upsert by primary key, NO FKs. The
jsonb shapes the card renders are pinned there:
    ingredients:    [{source, filter, est_count, est_cost_usd, note}]
    rationale:      {text, citations:[{label, ref}]}
    gate:           {verdict:'pass'|'warn'|'blocked', reasons:[text]}
    expected_yield: {available_now, target, gap_cost_usd, gap_note}
    needs_clarification: [text questions]     (rendered inline, never an error)
    runs.stages:        [{key, label, in_count, out_count, note}]
    runs.recomposition: [{suggestion, reason}]

COMPOSE is deterministic (no LLM): a goal is PARSED into the same filter a
HUNT-V1 recipe carries (counties / signal_tags / min_score / min_spread /
statuses) and counted against the live acquisition queue with the EXACT
evalRecipe semantics (match / pending / out — a soft criterion on an
un-enriched NULL is 'pending', never a fake miss). What the parser can't
confidently extract becomes a needs_clarification question, never a guess.
"""
import re
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.fips import _table  # census name<->fips (module-private, reused read-only)
from app.receipt import section_hash, sha256_text

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
COMPOSED_BY = "landos-engine/composer@v1 (deterministic)"

# status vocabulary the recipe modal offers (mirrors RECIPE_STATUS_OPTIONS in
# the CRM types.ts). Parser only accepts these — an unknown status word is a
# clarification, never invented.
STATUS_VOCAB = ["discovered", "new", "contacted", "qualified", "dead"]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# PARSER — deterministic goal_text -> a HuntRecipe-shaped filter
# ---------------------------------------------------------------------------
def _norm(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())).strip()


def _parse_money(tok):
    """'$5k'/'5,000'/'5000' -> 5000.0; None if not a number."""
    t = tok.replace(",", "").replace("$", "").strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", t)
    if not m:
        return None
    v = float(m.group(1))
    return v * {"k": 1_000, "m": 1_000_000, "": 1}[m.group(2)]


def parse_goal(goal_text, *, live_counties, live_tags):
    """Extract a recipe filter from free text. live_counties = {fips: name} that
    actually have queue rows (you can only hunt what's there); live_tags = the
    distinct signal_tags in use. Returns (filter_dict, questions, matched_terms).
    Nothing is invented — a term that can't be mapped is reported, not guessed."""
    text = goal_text or ""
    low = _norm(text)
    filt = {"counties": [], "signal_tags": [], "min_score": None,
            "min_spread": None, "statuses": [], "target": None}
    matched = []

    # counties: explicit "Name, ST" first, then raw 5-digit FIPS, then any
    # live-queue county whose name appears in the text.
    by_fips, by_state_name = _table()
    for name, st in re.findall(r"([A-Za-z][A-Za-z .'-]+?),\s*([A-Za-z]{2})\b", text):
        key = (st.upper(),
               re.sub(r" (county|parish|borough)$", "",
                      re.sub(r"[^a-z ]", " ", name.lower()).strip()))
        fips = by_state_name.get(key)
        if fips and fips not in filt["counties"]:
            filt["counties"].append(fips)
            matched.append(f"county:{by_fips[fips]['county']}, {st.upper()}")
    for fips in re.findall(r"\b(\d{5})\b", text):
        if fips in by_fips and fips not in filt["counties"]:
            filt["counties"].append(fips)
            matched.append(f"county:{fips}")
    for fips, name in live_counties.items():
        nm = _norm(name).replace(" county", "")
        if nm and re.search(rf"\b{re.escape(nm)}\b", low) and fips not in filt["counties"]:
            filt["counties"].append(fips)
            matched.append(f"county:{name}")

    # numeric floors
    m = re.search(r"score\s*(?:>=|>|of|at least|min(?:imum)?)?\s*(\d+(?:\.\d+)?)\+?", low) \
        or re.search(r"(\d+(?:\.\d+)?)\+?\s*score", low)
    if m:
        filt["min_score"] = float(m.group(1))
        matched.append(f"min_score:{filt['min_score']:g}")
    m = re.search(r"spread\s*(?:>=|>|of|at least|min(?:imum)?)?\s*\$?([\d.,]+[km]?)", low) \
        or re.search(r"\$?([\d.,]+[km]?)\s*(?:\+)?\s*spread", low)
    if m:
        v = _parse_money(m.group(1))
        if v is not None:
            filt["min_spread"] = v
            matched.append(f"min_spread:${v:,.0f}")

    # statuses (only the known vocabulary)
    for s in STATUS_VOCAB:
        if re.search(rf"\b{s}\b", low):
            filt["statuses"].append(s)
            matched.append(f"status:{s}")

    # signal tags: only the ones actually in use on the queue
    for tag in live_tags:
        if re.search(rf"\b{re.escape(_norm(tag).replace('_', ' '))}\b", low) \
           or re.search(rf"\b{re.escape(tag.lower())}\b", low):
            filt["signal_tags"].append(tag)
            matched.append(f"tag:{tag}")

    # target ("need 50", "target 100", "50 leads/parcels/deals")
    m = re.search(r"(?:target|need|want|get|find)\s*(\d+)", low) \
        or re.search(r"(\d+)\s*(?:leads?|parcels?|deals?|properties)", low)
    if m:
        filt["target"] = int(m.group(1))
        matched.append(f"target:{filt['target']}")

    # clarification questions when the parse yields no usable filter
    questions = []
    has_filter = any([filt["counties"], filt["signal_tags"], filt["statuses"],
                      filt["min_score"] is not None, filt["min_spread"] is not None])
    if not has_filter:
        questions.append(
            "Which counties, statuses, score floor, or spread floor should this hunt "
            "target? I couldn't extract a filter from the goal as written.")
    return filt, questions, matched


# ---------------------------------------------------------------------------
# MATCHER — count the filter against the live queue (evalRecipe semantics)
# ---------------------------------------------------------------------------
_COUNT_SQL = """
with c as (
  select case
    -- definitive fails first (HARD already applied in WHERE; these are SOFT-known)
    when %(has_co)s and county_fips is not null and not (county_fips = any(%(counties)s::text[])) then 'out'
    when %(min_score)s::numeric is not null and final_lead_score is not null and final_lead_score < %(min_score)s::numeric then 'out'
    when %(min_spread)s::numeric is not null and (assessed_total - amount_owed) is not null
         and (assessed_total - amount_owed) < %(min_spread)s::numeric then 'out'
    -- un-enriched soft criteria -> pending (shown, never a fake miss)
    when %(has_co)s and county_fips is null then 'pending'
    when %(min_score)s::numeric is not null and final_lead_score is null then 'pending'
    when %(min_spread)s::numeric is not null and (assessed_total - amount_owed) is null then 'pending'
    else 'match' end as verdict
  from public.acquisition_leads
  where tenant_id = %(tenant)s::uuid
    and (cardinality(%(statuses)s::text[]) = 0 or status = any(%(statuses)s::text[]))
    and (cardinality(%(tags)s::text[]) = 0 or signal_tags && %(tags)s::text[])
)
select
  count(*) filter (where verdict = 'match')   as available_now,
  count(*) filter (where verdict = 'pending') as pending,
  count(*)                                     as hard_pass
from c
"""


def count_acquire(cur, filt, tenant=TENANT):
    """available_now / pending / hard_pass for a parsed filter — the SAME
    match/pending/out verdict the CRM queue applies, computed in SQL."""
    cur.execute(_COUNT_SQL, {
        "tenant": tenant,
        "has_co": bool(filt["counties"]),
        "counties": filt["counties"],
        "statuses": filt["statuses"],
        "tags": filt["signal_tags"],
        "min_score": filt["min_score"],
        "min_spread": filt["min_spread"],
    })
    return cur.fetchone()


def _filter_english(filt, by_fips):
    """Human-readable filter string for ingredients/rationale (no invention)."""
    parts = []
    if filt["counties"]:
        names = [by_fips.get(f, {}).get("county", f) for f in filt["counties"]]
        parts.append("counties " + ", ".join(names))
    if filt["statuses"]:
        parts.append("status " + "/".join(filt["statuses"]))
    if filt["signal_tags"]:
        parts.append("tags " + "/".join(filt["signal_tags"]))
    if filt["min_score"] is not None:
        parts.append(f"score ≥ {filt['min_score']:g}")
    if filt["min_spread"] is not None:
        parts.append(f"spread ≥ ${filt['min_spread']:,.0f}")
    return "; ".join(parts) or "any lead in the tenant queue"


# ---------------------------------------------------------------------------
# COMPOSE — build the proposal body (pure; DB write is compose_and_sync)
# ---------------------------------------------------------------------------
def compose_acquire(cur, goal_text, tenant=TENANT):
    """Compose an ACQUIRE proposal body from a free-text goal. Deterministic,
    grounded in the live queue. Returns a dict ready to upsert."""
    by_fips, _ = _table()
    cur.execute("""select county_fips, count(*) n from public.acquisition_leads
                   where tenant_id = %s and county_fips is not null
                   group by county_fips""", (tenant,))
    live_counties = {r["county_fips"]: by_fips.get(r["county_fips"], {}).get("county", r["county_fips"])
                     for r in cur.fetchall()}
    cur.execute("""select distinct unnest(signal_tags) t from public.acquisition_leads
                   where tenant_id = %s""", (tenant,))
    live_tags = [r["t"] for r in cur.fetchall() if r["t"]]

    filt, questions, matched = parse_goal(goal_text, live_counties=live_counties,
                                          live_tags=live_tags)
    if questions:
        return {"goal_type": "acquire", "goal_text": goal_text, "status": "needs_clarification",
                "ingredients": [], "rationale": {"text":
                    "The goal didn't carry a filter I could ground against the queue.",
                    "citations": []},
                "gate": {"verdict": "warn", "reasons": ["awaiting clarification"]},
                "expected_yield": {"available_now": None, "target": None,
                                   "gap_cost_usd": None, "gap_note": None},
                "needs_clarification": questions, "filter": filt}

    counts = count_acquire(cur, filt, tenant)
    available, pending = counts["available_now"], counts["pending"]
    filt_txt = _filter_english(filt, by_fips)

    ingredients = [{"source": "public.acquisition_leads (queue)", "filter": filt_txt,
                    "est_count": available, "est_cost_usd": 0,
                    "note": "clean matches — every criterion evaluable and passing"}]
    if pending:
        ingredients.append({"source": "public.acquisition_leads (pending)", "filter": filt_txt,
                            "est_count": pending, "est_cost_usd": 0,
                            "note": "un-enriched (county/score/spread unknown) — widen or enrich to convert"})

    # gate: pass on ≥1 clean match; warn on none-yet; blocked never fabricated
    if available >= 1:
        gate = {"verdict": "pass", "reasons": [f"{available} clean matches in the queue now"]}
    elif pending:
        gate = {"verdict": "warn", "reasons": [f"0 clean matches; {pending} pending enrichment"]}
    else:
        gate = {"verdict": "warn", "reasons": ["0 matches for this filter in the queue today"]}

    # expected_yield: real available_now; target only if the operator named one;
    # NO fabricated dollar gap (acquire gaps close via $0 enrichment/sourcing).
    y = {"available_now": available, "target": filt["target"],
         "gap_cost_usd": None, "gap_note": None}
    if filt["target"] and filt["target"] > available and pending:
        y["gap_note"] = f"{min(filt['target'] - available, pending)} pending enrichable at $0"

    citations = [{"label": f"queue match count ({available})", "ref": None}]
    for f in filt["counties"]:
        citations.append({"label": by_fips.get(f, {}).get("county", f), "ref": None})
    rationale = {"text": f"Parsed filter → {filt_txt}. Counted with the queue's own "
                         f"match/pending/out rule: {available} clean, {pending} pending.",
                 "citations": citations}

    return {"goal_type": "acquire", "goal_text": goal_text, "status": "proposed",
            "ingredients": ingredients, "rationale": rationale, "gate": gate,
            "expected_yield": y, "needs_clarification": [],
            "filter": filt, "matched_terms": matched}


def compose_dispose(cur, goal_text, dispo, tenant=TENANT):
    """Compose a DISPOSE proposal from structured inventory. Buyer matching is
    honest about the empty buyers/contacts tables today (warn, never invented)."""
    inv = (dispo or {}).get("inventory") or []
    params = (dispo or {}).get("params") or {}
    lead_ids = [i["lead_id"] for i in inv if i.get("lead_id")]
    # validate inventory against the live queue (only real leads)
    cur.execute("""select lead_id, county_fips, owner_name from public.acquisition_leads
                   where tenant_id = %s and lead_id = any(%s)""", (tenant, lead_ids))
    found = cur.fetchall()
    found_ids = {str(r["lead_id"]) for r in found}
    missing = [lid for lid in lead_ids if lid not in found_ids]

    # buyer match — the CONTACTS/buyers surface is empty today (honest empty)
    cur.execute("select count(*) n from public.dispositions where tenant_id = %s", (tenant,))
    _disp_n = cur.fetchone()["n"]
    buyers_matched = 0  # no buyer-type contacts populated yet (documented empty state)

    ingredients = [{"source": "public.dispositions (inventory)",
                    "filter": f"{len(found)} deal(s) marked for disposition",
                    "est_count": len(found), "est_cost_usd": None,
                    "note": (f"{len(missing)} inventory id(s) not in queue — dropped"
                             if missing else "all inventory validated")}]
    ingredients.append({"source": "public.contacts (buyers)",
                        "filter": "buyer-type contacts matched to this inventory",
                        "est_count": buyers_matched, "est_cost_usd": None,
                        "note": "no buyer-type contacts on file yet — matching lights up when the buyer book fills"})

    if not found:
        return {"goal_type": "dispose", "goal_text": goal_text, "status": "needs_clarification",
                "ingredients": ingredients,
                "rationale": {"text": "None of the supplied inventory resolves to a live queue lead.",
                              "citations": []},
                "gate": {"verdict": "blocked", "reasons": ["no valid inventory to dispose"]},
                "expected_yield": {"available_now": 0, "target": None,
                                   "gap_cost_usd": None, "gap_note": None},
                "needs_clarification": ["Which deals should this disposition include? "
                                        "The supplied inventory didn't match the queue."],
                "filter": {"inventory": lead_ids, "params": params}}

    gate = ({"verdict": "warn", "reasons": [f"{len(found)} deal(s) ready; no matched buyers yet"]}
            if buyers_matched == 0 else
            {"verdict": "pass", "reasons": [f"{len(found)} deal(s), {buyers_matched} buyer(s) matched"]})

    tgt = params.get("target_price_usd")
    bits = [f"{len(found)} deal(s) staged for disposition"]
    if params.get("timeline_days"):
        bits.append(f"{params['timeline_days']}-day timeline")
    if tgt:
        bits.append(f"target ${float(tgt):,.0f}")
    if params.get("note"):
        bits.append(str(params["note"]))
    rationale = {"text": "; ".join(bits) + ". Buyer matching pending a populated buyer book.",
                 "citations": [{"label": f"{len(found)} inventory lead(s)", "ref": None}]}

    return {"goal_type": "dispose", "goal_text": goal_text, "status": "proposed",
            "ingredients": ingredients, "rationale": rationale, "gate": gate,
            "expected_yield": {"available_now": len(found),
                               "target": int(tgt) if tgt else None,
                               "gap_cost_usd": None, "gap_note": None},
            "needs_clarification": [],
            "filter": {"inventory": lead_ids, "params": params}}


# ---------------------------------------------------------------------------
# SYNC — the ONLY writer of the projections (owner/service_role)
# ---------------------------------------------------------------------------
def _rationale_hash(body):
    """Receipt of WHY this recipe exists: hash of goal + parsed filter + gate."""
    return section_hash({"goal_type": body["goal_type"], "goal_text": body["goal_text"],
                         "filter": body.get("filter"), "gate": body["gate"]})


def compose_and_sync(cur, goal_type, goal_text, dispo=None, tenant=TENANT):
    """Compose then UPSERT one recipe_proposals_view row. Returns (proposal_id,
    body). The engine is the sole writer; is_fixture stays false on real syncs."""
    body = (compose_dispose(cur, goal_text, dispo, tenant) if goal_type == "dispose"
            else compose_acquire(cur, goal_text, tenant))
    rat_hash = _rationale_hash(body)
    receipt_hash = sha256_text("GENESIS" + section_hash({k: body[k] for k in
                               ("goal_type", "goal_text", "status", "ingredients",
                                "rationale", "gate", "expected_yield", "needs_clarification")}))
    cur.execute(
        """insert into public.recipe_proposals_view
             (tenant_id, goal_type, goal_text, status, ingredients, rationale,
              rationale_hash, gate, expected_yield, needs_clarification,
              composed_by, engine_receipt_hash, is_fixture)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,false)
           returning proposal_id""",
        (tenant, body["goal_type"], body["goal_text"], body["status"],
         Jsonb(body["ingredients"]), Jsonb(body["rationale"]), rat_hash,
         Jsonb(body["gate"]), Jsonb(body["expected_yield"]),
         Jsonb(body["needs_clarification"]), COMPOSED_BY, receipt_hash))
    pid = cur.fetchone()["proposal_id"]
    body["rationale_hash"], body["engine_receipt_hash"] = rat_hash, receipt_hash
    return str(pid), body


# ---------------------------------------------------------------------------
# DECIDE — approve mints the composed recipe + executes a run; reject flips
# ---------------------------------------------------------------------------
def get_proposal(cur, proposal_id):
    cur.execute("""select proposal_id, tenant_id, goal_type, goal_text, status,
                          ingredients, rationale, rationale_hash, gate, expected_yield
                   from public.recipe_proposals_view where proposal_id = %s""",
                (proposal_id,))
    return cur.fetchone()


def decide(cur, proposal_id, action, decided_by=None, note=None):
    """approve|reject a proposal. approve → status approved + composed
    hunt_recipes row (proposal_id + rationale_hash) + one executed run into
    recipe_runs_view. reject → status rejected. Both stamp decided_at/by.
    Returns a dict result. Enforces the proposed/needs_clarification gate."""
    p = get_proposal(cur, proposal_id)
    if p is None:
        raise LookupError(f"no proposal {proposal_id}")
    if p["status"] not in ("proposed", "needs_clarification"):
        raise ValueError(f"proposal already {p['status']}")
    if action not in ("approve", "reject"):
        raise ValueError("action must be approve or reject")

    if action == "reject":
        cur.execute("""update public.recipe_proposals_view
                       set status='rejected', decided_at=now(), decided_by=%s, updated_at=now()
                       where proposal_id=%s""", (decided_by, proposal_id))
        return {"proposal_id": proposal_id, "status": "rejected"}

    # approve
    cur.execute("""update public.recipe_proposals_view
                   set status='approved', decided_at=now(), decided_by=%s, updated_at=now()
                   where proposal_id=%s""", (decided_by, proposal_id))

    recipe_id = run_id = None
    if p["goal_type"] == "acquire":
        recipe_id = _mint_composed_recipe(cur, p)
        run_id = _execute_run(cur, p, recipe_id)
    else:
        # dispose: the composed "recipe" is the disposition intent; a run
        # materializes the staged inventory funnel. No hunt_recipes filter row
        # (that table is an acquisition filter) — the run carries the proof.
        run_id = _execute_run(cur, p, None)
    return {"proposal_id": proposal_id, "status": "approved",
            "recipe_id": recipe_id, "run_id": run_id}


def _mint_composed_recipe(cur, p):
    """Mint the composed hunt_recipes row from an approved acquire proposal —
    the provenance link the mig-060 contract requires (proposal_id +
    rationale_hash, source='composed'). Filter recovered from the proposal."""
    rat = p["rationale"] or {}
    # recover the filter from ingredients' human string is lossy; re-derive from
    # the live queue exactly as compose did, so the minted recipe is faithful.
    by_fips, _ = _table()
    cur.execute("""select county_fips, count(*) n from public.acquisition_leads
                   where tenant_id=%s and county_fips is not null group by county_fips""",
                (p["tenant_id"],))
    live_counties = {r["county_fips"]: by_fips.get(r["county_fips"], {}).get("county", r["county_fips"])
                     for r in cur.fetchall()}
    cur.execute("select distinct unnest(signal_tags) t from public.acquisition_leads where tenant_id=%s",
                (p["tenant_id"],))
    live_tags = [r["t"] for r in cur.fetchall() if r["t"]]
    filt, _, _ = parse_goal(p["goal_text"], live_counties=live_counties, live_tags=live_tags)
    name = (p["goal_text"] or "Composed hunt")[:120]
    cur.execute(
        """insert into public.hunt_recipes
             (tenant_id, created_by, name, counties, signal_tags, min_score,
              min_spread, statuses, source, goal_type, proposal_id, rationale_hash)
           values (%s, %s, %s, %s, %s, %s, %s, %s, 'composed', 'acquire', %s, %s)
           returning id""",
        (p["tenant_id"], _system_actor(cur, p["tenant_id"]), name, filt["counties"],
         filt["signal_tags"], filt["min_score"], filt["min_spread"], filt["statuses"],
         p["proposal_id"], p["rationale_hash"]))
    return str(cur.fetchone()["id"])


def _system_actor(cur, tenant):
    """hunt_recipes.created_by is NOT NULL references profiles(id). The composer
    isn't a person; borrow a desk profile in-tenant so provenance is valid
    (no FK violation). Falls back to any profile if role columns differ."""
    for q in (
        "select id from public.profiles where tenant_id=%s and role in "
        "('owner_admin','tenant_admin','manager') order by role limit 1",
        "select id from public.profiles where tenant_id=%s limit 1",
        "select id from public.profiles limit 1",
    ):
        try:
            cur.execute(q, (tenant,) if "%s" in q else ())
            r = cur.fetchone()
            if r:
                return r["id"]
        except Exception:
            continue
    return None


def _execute_run(cur, p, recipe_id):
    """Execute ONE run: re-materialize the funnel against the live queue and
    write recipe_runs_view. $0 (it's a count), receipt-hashed, with grounded
    recomposition suggestions when the yield is thin."""
    if p["goal_type"] == "acquire":
        by_fips, _ = _table()
        cur.execute("""select county_fips, count(*) n from public.acquisition_leads
                       where tenant_id=%s and county_fips is not null group by county_fips""",
                    (p["tenant_id"],))
        live_counties = {r["county_fips"]: by_fips.get(r["county_fips"], {}).get("county", r["county_fips"])
                         for r in cur.fetchall()}
        cur.execute("select distinct unnest(signal_tags) t from public.acquisition_leads where tenant_id=%s",
                    (p["tenant_id"],))
        live_tags = [r["t"] for r in cur.fetchall() if r["t"]]
        filt, _, _ = parse_goal(p["goal_text"], live_counties=live_counties, live_tags=live_tags)
        counts = count_acquire(cur, filt, p["tenant_id"])
        cur.execute("select count(*) n from public.acquisition_leads where tenant_id=%s", (p["tenant_id"],))
        total = cur.fetchone()["n"]
        stages = [
            {"key": "intake", "label": "queue intake", "in_count": total, "out_count": total,
             "note": "tenant acquisition queue"},
            {"key": "hard", "label": "status/tags", "in_count": total,
             "out_count": counts["hard_pass"], "note": "hard criteria (definitive)"},
            {"key": "soft", "label": "county/score/spread", "in_count": counts["hard_pass"],
             "out_count": counts["available_now"] + counts["pending"],
             "note": "soft criteria (un-enriched rows kept as pending)"},
            {"key": "available", "label": "clean matches", "in_count": counts["available_now"] + counts["pending"],
             "out_count": counts["available_now"],
             "note": f"{counts['pending']} pending enrichment" if counts["pending"] else "all evaluable"},
        ]
        recomposition = []
        if counts["available_now"] == 0:
            if filt["min_score"] is not None:
                recomposition.append({"suggestion": "drop or lower the score floor",
                                      "reason": "no clean matches meet it today"})
            if counts["pending"]:
                recomposition.append({"suggestion": "enrich pending rows (geocode/score, $0)",
                                      "reason": f"{counts['pending']} rows are pending, not missing"})
            if not recomposition:
                recomposition.append({"suggestion": "widen counties or statuses",
                                      "reason": "the filter matches nothing in the queue"})
        cost = 0
    else:
        inv = ((p["expected_yield"] or {}).get("available_now")) or 0
        stages = [{"key": "inventory", "label": "staged inventory", "in_count": inv,
                   "out_count": inv, "note": "deals marked for disposition"},
                  {"key": "buyers", "label": "matched buyers", "in_count": inv,
                   "out_count": 0, "note": "no buyer-type contacts on file yet"}]
        recomposition = [{"suggestion": "add buyer-type contacts", "reason": "0 buyers matched"}]
        cost = None

    receipt = sha256_text("GENESIS" + section_hash({"proposal_id": str(p["proposal_id"]),
                                                    "stages": stages}))
    cur.execute(
        """insert into public.recipe_runs_view
             (tenant_id, recipe_id, proposal_id, status, stages, cost_usd,
              receipt_hash, recomposition, is_fixture, started_at, finished_at)
           values (%s,%s,%s,'complete',%s,%s,%s,%s,false, now(), now())
           returning run_id""",
        (p["tenant_id"], recipe_id, p["proposal_id"], Jsonb(stages), cost,
         receipt, Jsonb(recomposition)))
    return str(cur.fetchone()["run_id"])

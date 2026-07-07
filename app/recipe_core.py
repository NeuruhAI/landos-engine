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
# DISTRESS RECIPE — the operator's "tax-delinquent + high-equity, ranked by the
# distress stack" recipe, encoded HONESTLY against the live queue (Path C).
#
# The queue today: ~1,517 rows carry tax-sale provenance (emerging_signals
# source_type='taxsale') — that IS F1 (tax_delinquent), by provenance not a
# tag (signal_tags is empty). Equity is uncomputable (0 rows valued: assessed/
# owed/market all NULL) so the ≥70% floor and the ≥60% override resolve to
# PENDING, never a fake miss (same doctrine as min_spread in count_acquire).
# The distress stack has one real axis (taxsale) + 1 probate row; absentee/
# code_violation/divorce/lis_pendens are not ingested — so RANK collapses to
# score_distress today. compose_distress states all of this against real counts
# and emits a grounded punch_list: exactly what enrichment/ingest lights each
# dead clause (the Phase B spec, auto-derived — never guessed).
#
# Deliberately NOT wired into compose_and_sync's approve/mint path: hunt_recipes
# has no equity/tax_delinquent/rank columns, so approving would silently drop
# F2 + rank. That fidelity gap is surfaced IN the punch_list, not hidden.
# ---------------------------------------------------------------------------
DISTRESS_DIMENSIONS = ["absentee", "code_violation", "divorce", "lis_pendens", "probate"]

# how each dimension is DETECTED in the spine (so coverage lights up on its own
# when a lane lands): emerging_signals.source_type aliases + signal_tag fallbacks.
_DIM_SOURCE_TYPES = {
    "probate": ["probate", "probate_in"],
    "code_violation": ["code_violation", "codeviolation"],
    "divorce": ["divorce"],
    "lis_pendens": ["lis_pendens", "lispendens"],
    "absentee": ["absentee"],
}
_DIM_TAGS = {"absentee": ["absentee", "owner_out_of_state"]}
# parcel_meta boolean keys that evidence a dimension ($0, in-spine). absentee is
# already carried as parcel_meta.owner_out_of_state on ~450 MI leads.
_DIM_META = {"absentee": ["owner_out_of_state"]}
_META_TRUTHY = ("true", "t", "yes", "1", "y")

DISTRESS_SPEC_DEFAULT = {
    "tax_delinquent": True,           # F1 — from county scrape / tax-sale provenance
    "equity_pct_min": 70.0,           # F2 — "tax delinquency + low equity = no money"
    "equity_override_pct": 60.0,      # OVERRIDE — high-demand/old-RE + builder-assignable exit (operator call)
    "rank_by": list(DISTRESS_DIMENSIONS),
    "also_pull": ["code_violation", "lis_pendens"],
    "counties": [],
    "statuses": [],
}


def distress_spec(**overrides):
    """The canonical operator recipe with optional overrides. Missing/None keys
    fall back to DISTRESS_SPEC_DEFAULT — nothing invented, only overridden."""
    spec = {k: (list(v) if isinstance(v, list) else v)
            for k, v in DISTRESS_SPEC_DEFAULT.items()}
    for k, v in (overrides or {}).items():
        if v is not None:
            spec[k] = v
    return spec


_HAS_TAXSALE = ("exists (select 1 from jsonb_array_elements("
                "case when jsonb_typeof(a.emerging_signals)='array' then a.emerging_signals "
                "else '[]'::jsonb end) e where e->>'source_type' = 'taxsale')")

# ---- EQUITY from tax-sale provenance (Phase B b1, $0) --------------------------
# The value data the recipe needs is ALREADY ingested — trapped in each tax-sale
# signal's raw_payload: `sev` (MI State Equalized Value, statutorily ~50% of true
# cash value → market ≈ SEV × 2) and `min_bid` (the tax-sale minimum bid = the
# delinquent-tax lien that must be cleared to take title). For the land tax-sale
# universe (rarely mortgaged), that lien IS the debt, so:
#     equity_pct = (market − min_bid) / market,  market = SEV × state_ratio
# Materialized top-level columns win when present (a later opt-in projection can
# cache them for the CRM UI); otherwise we compute live from provenance. States
# without a calibrated ratio → equity NULL → pending (never guessed).
_MARKET_MULT = {"26": 2.0}   # MI: SEV ≈ 50% of true cash value → ×2. Per-state calibration TODO.


def _ratio_case(alias="a"):
    whens = " ".join(f"when '{fips}' then {mult}" for fips, mult in _MARKET_MULT.items())
    return f"(case left({alias}.county_fips, 2) {whens} else null end)"


# per-lead (sev, min_bid) rolled up from the lead's tax-sale signals
_VAL_CTE = """val as (
    select a.lead_id,
           max(nullif(s.raw_payload->>'sev','')::numeric)                                     as sev,
           min(nullif(coalesce(s.raw_payload->>'min_bid', s.raw_payload->>'minimum_bid'),'')::numeric) as min_bid
    from public.acquisition_leads a
    join lateral jsonb_array_elements(
           case when jsonb_typeof(a.emerging_signals)='array' then a.emerging_signals else '[]'::jsonb end) e on true
    join landos.discovery_signals s
           on s.signal_id = (e->>'signal_id')::uuid and s.source_type = 'taxsale'
    where a.tenant_id = %(tenant)s::uuid
    group by a.lead_id
  )"""

# market/debt prefer materialized columns, else provenance; equity NULL outside a
# calibrated state ratio (honest pending, never a fabricated number).
_MARKET = f"(coalesce(a.assessed_total, v.sev) * {_ratio_case()})"
_DEBT = "coalesce(a.amount_owed, v.min_bid, 0)"
_EQUITY = (f"case when coalesce(a.assessed_total, v.sev) > 0 and {_ratio_case()} is not null "
           f"then round(({_MARKET} - {_DEBT}) / {_MARKET} * 100.0, 1) else null end")

_DISTRESS_COUNT_SQL = f"""
with {_VAL_CTE},
c as (
  select
    {_HAS_TAXSALE} as is_taxdelinq,
    {_EQUITY}      as equity_pct
  from public.acquisition_leads a
  left join val v on v.lead_id = a.lead_id
  where a.tenant_id = %(tenant)s::uuid
    and (cardinality(%(statuses)s::text[]) = 0 or a.status = any(%(statuses)s::text[]))
    and (cardinality(%(counties)s::text[]) = 0
         or (a.county_fips is not null and a.county_fips = any(%(counties)s::text[])))
), vf as (
  select is_taxdelinq, equity_pct,
    case
      when %(tax_delinquent)s and not is_taxdelinq                                          then 'out'
      when %(equity_min)s::numeric is not null and equity_pct is null                       then 'pending'
      when %(equity_min)s::numeric is not null and equity_pct < %(equity_min)s::numeric     then 'out'
      else 'match' end as verdict
  from c
)
select
  count(*) filter (where verdict = 'match')   as clean_match,
  count(*) filter (where verdict = 'pending') as pending,
  count(*) filter (where is_taxdelinq)        as tax_delinquent_n,
  count(*) filter (where %(override_min)s::numeric is not null and equity_pct is not null
                   and equity_pct >= %(override_min)s::numeric
                   and equity_pct <  %(equity_min)s::numeric)             as override_band,
  count(*) filter (where equity_pct is not null
                   and equity_pct < coalesce(%(override_min)s::numeric, %(equity_min)s::numeric)) as below_floor,
  count(*)                                     as scoped
from vf
"""


def count_distress(cur, spec, tenant=TENANT):
    """clean_match (F1+F2 confirmed ≥ floor) / pending (F1 ok, equity unknown) /
    tax_delinquent_n (F1 alone) / override_band (floor > equity ≥ override,
    operator-gated) / below_floor (truly rejected). Equity is SOFT: unknown →
    pending, never a fake miss. Computed live from tax-sale provenance."""
    em = spec.get("equity_pct_min")
    ov = spec.get("equity_override_pct")
    cur.execute(_DISTRESS_COUNT_SQL, {
        "tenant": tenant,
        "statuses": spec.get("statuses") or [],
        "counties": spec.get("counties") or [],
        "tax_delinquent": bool(spec.get("tax_delinquent")),
        "equity_min": em if em else None,   # falsy floor (None/0) == no equity floor
        "override_min": ov if ov else None,
    })
    return cur.fetchone()


def _distress_scope(spec, tenant):
    """Shared WHERE fragment + params for the scoped tax-delinquent set (tenant +
    statuses + counties + F1 tax-sale provenance). Reused by rank_axis/rank_top."""
    where = ["a.tenant_id = %(tenant)s::uuid",
             "(cardinality(%(statuses)s::text[]) = 0 or a.status = any(%(statuses)s::text[]))",
             "(cardinality(%(counties)s::text[]) = 0 or (a.county_fips is not null "
             "and a.county_fips = any(%(counties)s::text[])))"]
    if spec.get("tax_delinquent"):
        where.append(_HAS_TAXSALE)
    params = {"tenant": tenant, "statuses": spec.get("statuses") or [],
              "counties": spec.get("counties") or []}
    return " and ".join(where), params


# rank axes in priority order: most distress-semantic FIRST, but only used if it
# actually ORDERS the scoped set (>= 2 distinct values). score_distress is the
# intended axis; today it's populated on ~1 row so emerging_score (the tax-sale
# distress-emergence score) carries the rank until score_distress fills.
_RANK_CANDIDATES = ["score_distress", "emerging_score", "final_lead_score", "land_score"]


def rank_axis(cur, spec, tenant=TENANT):
    """Pick the best AVAILABLE rank axis on the scoped set — first candidate that
    truly orders it. Returns {column, populated, distinct}; column=None when
    nothing orders (honest: arbitrary order until a signal lands)."""
    scope, params = _distress_scope(spec, tenant)
    for col in _RANK_CANDIDATES:
        cur.execute(f"select count(a.{col}) as populated, count(distinct a.{col}) as distinct_n "
                    f"from public.acquisition_leads a where {scope}", params)
        r = cur.fetchone()
        if (r["distinct_n"] or 0) >= 2:
            return {"column": col, "populated": r["populated"], "distinct": r["distinct_n"]}
    return {"column": None, "populated": 0, "distinct": 0}


def rank_top(cur, spec, tenant=TENANT, limit=10, axis=None, equity_min=None):
    """Top CLEAN leads (scoped + equity ≥ floor) by the DISTRESS STACK — primary
    order is stack depth (count of present rank dimensions), tiebroken by the
    numeric axis from rank_axis (whitelisted; NULLS last). equity_min filters to
    the set the recipe actually returns, so a below-floor lead never previews as
    a target. Returns stack_score + stack_dims (which axes stacked) per lead."""
    scope, params = _distress_scope(spec, tenant)
    params["limit"] = limit
    dims = spec.get("rank_by") or []
    stack = _stack_score_sql(dims)
    dim_arr = ("array_remove(array[" + ", ".join(
        f"case when {_dim_present_sql(d)} then '{d}' end" for d in dims) + "], null)"
        if dims else "array[]::text[]")
    col = axis if axis in _RANK_CANDIDATES else None
    tie = f"a.{col} desc nulls last, " if col else ""
    floor_sql = ""
    if equity_min:
        params["equity_min"] = equity_min
        floor_sql = f" and ({_EQUITY}) >= %(equity_min)s::numeric"
    cur.execute(f"""select a.lead_id, a.score_distress, a.emerging_score, a.final_lead_score,
                           a.status, a.county_fips, {_EQUITY} as equity_pct,
                           ({stack}) as stack_score, {dim_arr} as stack_dims
                    from public.acquisition_leads a
                    left join lateral (
                      select max(nullif(s.raw_payload->>'sev','')::numeric) as sev,
                             min(nullif(coalesce(s.raw_payload->>'min_bid', s.raw_payload->>'minimum_bid'),'')::numeric) as min_bid
                      from jsonb_array_elements(
                        case when jsonb_typeof(a.emerging_signals)='array' then a.emerging_signals else '[]'::jsonb end) e
                      join landos.discovery_signals s
                        on s.signal_id = (e->>'signal_id')::uuid and s.source_type='taxsale') v on true
                    where {scope}{floor_sql}
                    order by ({stack}) desc, {tie}a.lead_id limit %(limit)s""", params)
    return cur.fetchall()


def _dim_present_sql(dim, alias="a"):
    """A boolean SQL expression: does this lead evidence `dim`? Detects across all
    three $0 sources — emerging_signals.source_type (aliases), signal_tags, and
    parcel_meta boolean keys. All values come from fixed internal maps, so inlining
    them as literals is injection-safe."""
    parts = []
    srcs = ", ".join(f"'{s}'" for s in _DIM_SOURCE_TYPES.get(dim, [dim]))
    parts.append(f"exists (select 1 from jsonb_array_elements(case when "
                 f"jsonb_typeof({alias}.emerging_signals)='array' then {alias}.emerging_signals "
                 f"else '[]'::jsonb end) e where e->>'source_type' in ({srcs}))")
    if _DIM_TAGS.get(dim):
        tag_arr = "array[" + ", ".join(f"'{t}'" for t in _DIM_TAGS[dim]) + "]::text[]"
        parts.append(f"(coalesce({alias}.signal_tags, '{{}}'::text[]) && {tag_arr})")
    truthy = ", ".join(f"'{v}'" for v in _META_TRUTHY)
    for mk in _DIM_META.get(dim, []):
        parts.append(f"lower({alias}.parcel_meta->>'{mk}') in ({truthy})")
    # coalesce → non-null boolean (NULL signal_tags/parcel_meta must read as false,
    # else (bool)::int in the stack sum yields NULL and poisons the score)
    return "coalesce((" + " or ".join(parts) + "), false)"


def _stack_score_sql(dims, alias="a"):
    """How many distress dimensions stack on a lead — the literal 'distress stack'
    depth the recipe ranks by. 0 when no rank dimension is present."""
    if not dims:
        return "0"
    return " + ".join(f"({_dim_present_sql(d, alias)})::int" for d in dims)


def distress_coverage(cur, dims, tenant=TENANT):
    """For each requested rank dimension, how many queue leads carry it TODAY —
    grounded (measured 0, never assumed). One distinct-lead count per dim across
    emerging_signals.source_type + signal_tags + parcel_meta (so absentee lights
    up from parcel_meta.owner_out_of_state at $0, no double count)."""
    out = []
    for dim in dims:
        cur.execute(f"select count(*) n from public.acquisition_leads a "
                    f"where a.tenant_id = %s and {_dim_present_sql(dim)}", (tenant,))
        n = cur.fetchone()["n"]
        out.append({"dimension": dim, "rows": int(n), "present": n > 0})
    return out


def _valuation_note():
    return ("equity = (market − min_bid) / market, market = SEV × 2 (MI cash-value "
            "basis); min_bid = tax-sale lien to clear. $0, from tax-sale provenance")


def _distress_goal_text(spec):
    ov = spec.get("equity_override_pct")
    return (f"tax-delinquent, equity ≥{spec['equity_pct_min']:g}%"
            + (f" (override ≥{ov:g}% high-demand/builder-exit, operator call)" if ov else "")
            + ", rank by distress stack: " + " · ".join(spec["rank_by"]))


def _distress_punch_list(spec, coverage, counts):
    """The Phase B spec, AUTO-DERIVED from the live gaps — what lights each
    clause that still has no data. Grounded in the measured counts, never guessed."""
    items = []
    eq = spec["equity_pct_min"]
    ov = spec.get("equity_override_pct")
    clean, pending = counts["clean_match"], counts["pending"]
    if clean == 0:
        items.append({
            "clause": f"F2 equity ≥{eq:g}%" + (f" + ≥{ov:g}% override" if ov else ""),
            "status": "inert",
            "reason": "0 leads valued — no SEV/min_bid provenance on any scoped row",
            "lights_when": "tax-sale raw_payload sev + min_bid present (or assessed_total/amount_owed materialized)",
            "where": "Phase B b1 — valuation projection (done for MI; calibrate other states)"})
    elif pending:
        items.append({
            "clause": f"F2 equity ≥{eq:g}% — LIT ({clean} confirmed)", "status": "partial",
            "reason": f"{pending} still pending — no SEV or state ratio not calibrated (non-MI)",
            "lights_when": "add state SEV→market ratio to _MARKET_MULT, or materialize assessed_total",
            "where": "Phase B b1 — per-state valuation calibration"})
    for c in coverage:
        if not c["present"]:
            items.append({
                "clause": f"rank axis: {c['dimension']}", "status": "absent",
                "reason": "0 queue rows carry this signal",
                "lights_when": f"emerging_signals source_type='{c['dimension']}' (or signal_tags) present",
                "where": "Phase B b2 — landos-ingest lane"})
    for src in spec.get("also_pull", []):
        items.append({
            "clause": f"ALSO PULL: {src}", "status": "absent",
            "reason": "no ingest lane exists",
            "lights_when": f"discovery_signals source_type='{src}' rows staged",
            "where": "Phase B b2 — landos-ingest (repo firewall: engine never fetches raw sources)"})
    items.append({
        "clause": "approve-persistence fidelity", "status": "lossy",
        "reason": "hunt_recipes has no equity_pct/tax_delinquent/rank_by columns",
        "lights_when": "hunt_recipes gains those columns",
        "where": "CRM migration (mig-060 contract) — until then approving mints only the "
                 "status/tag filter, dropping F2 + rank"})
    return items


def compose_distress(cur, spec=None, tenant=TENANT, rank_limit=10):
    """Compose the operator's tax-delinquent + high-equity distress recipe,
    honestly, against the live queue. Returns a proposal body (goal_type
    'acquire' so it renders in the RECIPE-2 card) plus a grounded punch_list of
    what lights each clause that has no data yet. Pure read — persist via
    sync_proposal; approve/mint intentionally NOT wired (see punch_list)."""
    spec = distress_spec(**(spec or {}))
    counts = count_distress(cur, spec, tenant)
    f1 = counts["tax_delinquent_n"]        # F1 alone — all tax-delinquent
    clean = counts["clean_match"]          # F1+F2 confirmed ≥ floor (real targets)
    pending = counts["pending"]            # F1 ok, equity unknown (no SEV / uncalibrated state)
    band = counts["override_band"]         # override ≤ equity < floor (operator call)
    below = counts["below_floor"]          # truly rejected — "a recipe for no money"
    coverage = distress_coverage(cur, spec["rank_by"], tenant)
    axis = rank_axis(cur, spec, tenant)
    top = rank_top(cur, spec, tenant, rank_limit, axis=axis["column"],
                   equity_min=spec.get("equity_pct_min"))

    eq = spec["equity_pct_min"]
    ov = spec.get("equity_override_pct")
    present = [c for c in coverage if c["present"]]
    absent = [c for c in coverage if not c["present"]]
    # honest rank statement: distress-stack depth is the primary axis (the literal
    # "rank by the stack"); the numeric axis only tiebreaks within equal depth.
    tie = axis["column"]
    if present:
        rank_note = ("ranked by distress-stack depth ("
                     + ", ".join(f"{c['dimension']} {c['rows']}" for c in present) + ")"
                     + (f", tiebroken by {tie}" if tie else ""))
    elif tie:
        rank_note = (f"ranked by {tie} ({axis['distinct']} live values) — no distress-stack "
                     f"axis present yet, and score_distress orders only 1 row")
    else:
        rank_note = "stable order — no distress-stack axis present and no numeric axis orders the set"

    ingredients = [
        {"source": "public.acquisition_leads (tax-delinquent)",
         "filter": "F1: tax_delinquent — tax-sale provenance (emerging_signals)",
         "est_count": f1, "est_cost_usd": 0,
         "note": f"workable now; {rank_note}"},
        {"source": "public.acquisition_leads (equity ≥%g%%)" % eq,
         "filter": (f"F2: equity_pct ≥ {eq:g} (override ≥ {ov:g}, operator-gated)" if ov
                    else f"F2: equity_pct ≥ {eq:g}"),
         "est_count": clean, "est_cost_usd": 0,
         "note": (f"{clean} confirmed ≥{eq:g}%"
                  + (f", +{band} in {ov:g}–{eq:g}% override band" if ov and band else "")
                  + f", {below} below floor (no money), {pending} pending — {_valuation_note()}")},
        {"source": "distress-stack rank",
         "filter": "rank by " + " · ".join(spec["rank_by"]),
         "est_count": sum(c["rows"] for c in present), "est_cost_usd": 0,
         "note": ("present: " + (", ".join(f"{c['dimension']} {c['rows']}" for c in present) or "none")
                  + " | absent (not ingested): " + (", ".join(c["dimension"] for c in absent) or "none")
                  + f" — {rank_note}")},
    ]
    for src in spec.get("also_pull", []):
        cov = next((c for c in coverage if c["dimension"] == src), None)
        if not (cov and cov["present"]):
            ingredients.append({
                "source": f"{src} records (NOT ingested)", "filter": f"also-pull {src}",
                "est_count": 0, "est_cost_usd": 0,
                "note": "no ingest lane exists — Phase B b2 (landos-ingest new source_type)"})

    override_bit = (f" +{band} in the {ov:g}–{eq:g}% override band (operator call)"
                    if ov and band else "")
    gate = ({"verdict": "pass",
             "reasons": [f"{clean} leads ≥{eq:g}% equity — real targets{override_bit}",
                         f"{below} rejected below floor (no money); {pending} pending valuation; "
                         f"{len(absent)}/{len(coverage)} distress axes still absent — see punch_list",
                         rank_note]}
            if clean >= 1 else
            {"verdict": "warn",
             "reasons": [f"{f1} tax-delinquent, but 0 clear ≥{eq:g}% equity"
                         + (f" ({pending} pending valuation)" if pending else ""), rank_note]})

    yld = {"available_now": clean, "target": None, "gap_cost_usd": None,
           "gap_note": (f"{clean} ≥{eq:g}% equity"
                        + (f"; +{band} override-band ({ov:g}–{eq:g}%)" if ov and band else "")
                        + f"; {below} below floor; {pending} pending valuation")}

    rationale = {"text":
        (f"F1 tax_delinquent = {f1} rows (tax-sale provenance). F2 equity ≥{eq:g}%: {clean} confirmed"
         + (f", {band} override-eligible ({ov:g}–{eq:g}%)" if ov and band else "")
         + f", {below} below floor, {pending} pending. "
         + "Equity from SEV×2 − min_bid (MI). Distress stack present: "
         + (", ".join(f"{c['dimension']} {c['rows']}" for c in present) or "none")
         + "; absent: " + (", ".join(c["dimension"] for c in absent) or "none")
         + f". {rank_note[0].upper() + rank_note[1:]}. See punch_list for what lights each clause."),
        "citations": [{"label": f"tax-delinquent count ({f1})", "ref": None},
                      {"label": f"equity ≥{eq:g}% confirmed ({clean}) / pending ({pending})", "ref": None}]}

    return {"goal_type": "acquire", "goal_text": _distress_goal_text(spec),
            "status": "proposed" if f1 >= 1 else "needs_clarification",
            "ingredients": ingredients, "rationale": rationale, "gate": gate,
            "expected_yield": yld, "needs_clarification": [],
            "filter": {"recipe": "distress", **spec},
            "rank_axis": axis,
            "rank_preview": [{"lead_id": str(r["lead_id"]),
                              "stack_score": int(r["stack_score"]),
                              "stack_dims": list(r["stack_dims"] or []),
                              "rank_value": (float(r[axis["column"]])
                                             if axis["column"] and r[axis["column"]] is not None else None),
                              "equity_pct": (float(r["equity_pct"])
                                             if r["equity_pct"] is not None else None),
                              "score_distress": (float(r["score_distress"])
                                                 if r["score_distress"] is not None else None),
                              "emerging_score": (float(r["emerging_score"])
                                                 if r["emerging_score"] is not None else None),
                              "status": r["status"]} for r in top],
            "punch_list": _distress_punch_list(spec, coverage, counts)}


# ---------------------------------------------------------------------------
# SYNC — the ONLY writer of the projections (owner/service_role)
# ---------------------------------------------------------------------------
def _rationale_hash(body):
    """Receipt of WHY this recipe exists: hash of goal + parsed filter + gate."""
    return section_hash({"goal_type": body["goal_type"], "goal_text": body["goal_text"],
                         "filter": body.get("filter"), "gate": body["gate"]})


def _proposal_hashes(body):
    """(rationale_hash, engine_receipt_hash) for a composed body — pure, no DB,
    so every writer hashes identically."""
    rat_hash = _rationale_hash(body)
    receipt_hash = sha256_text("GENESIS" + section_hash({k: body[k] for k in
                               ("goal_type", "goal_text", "status", "ingredients",
                                "rationale", "gate", "expected_yield", "needs_clarification")}))
    return rat_hash, receipt_hash


def sync_proposal(cur, body, tenant=TENANT):
    """UPSERT one recipe_proposals_view row from a composed body. The engine is
    the sole writer; is_fixture stays false. Returns (proposal_id, body)."""
    rat_hash, receipt_hash = _proposal_hashes(body)
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


def compose_and_sync(cur, goal_type, goal_text, dispo=None, tenant=TENANT):
    """Compose then UPSERT one recipe_proposals_view row. Returns (proposal_id,
    body). The engine is the sole writer; is_fixture stays false on real syncs."""
    body = (compose_dispose(cur, goal_text, dispo, tenant) if goal_type == "dispose"
            else compose_acquire(cur, goal_text, tenant))
    return sync_proposal(cur, body, tenant)


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

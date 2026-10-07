"""
grounding.py — pull ONLY real fields for a lead, each tagged with the column it came
from. This is the source of truth for the whole feature: the brief may say nothing the
grounding does not contain, and the grounding is returned verbatim so every claim is
auditable. A null field is simply absent — it yields no fact, and therefore no sentence.
"""
import hashlib
import json
import re
import uuid

from ..score_absentee import classify_owner_type

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

# Board score above which a lead is "high-value" — routed to the paid model, and never
# pitched with a distress script if it is a portfolio holder (see the do_not_say rails).
DEFAULT_HIGH_SCORE = 70.0


def _is_uuid(s):
    try:
        uuid.UUID(str(s))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _jget(blob, *path):
    """Safe nested jsonb get: parcel_meta/score_signals arrive as dicts (or None)."""
    cur = blob
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _hub_count(portfolio_hub):
    """'Peter S Colovos — hub: 13 parcels' -> 13. None/other -> None (honest absent)."""
    if not portfolio_hub:
        return None
    m = re.search(r"hub:\s*(\d+)\s*parcel", str(portfolio_hub))
    return int(m.group(1)) if m else None


def fetch(cur, lead_id):
    """Resolve lead_id (board acquisition_leads.lead_id OR engine source_lead_id) to the
    board row + parcel_owner vintage_flag + recent call dispositions. None if unresolved."""
    if not _is_uuid(lead_id):
        return None
    cur.execute(
        """select a.lead_id, a.source_lead_id, a.owner_name, a.parcel_apn, a.county_fips,
                  a.signal_tags, a.score_distress, a.score_owner, a.score_arbitrage,
                  a.score_signals, a.parcel_meta, a.amount_owed, a.assessed_total,
                  a.card_state, a.stage, a.portfolio_hub, a.controlling_human,
                  a.contamination_kind, a.requires_bea, a.commercial_agent_flag,
                  a.situs_address, a.dnc, a.phone_1, po.vintage_flag
             from public.acquisition_leads a
             left join landos.parcel_owner po
               on po.county_fips = a.county_fips and po.parcel_apn = a.parcel_apn
            where a.tenant_id = %(t)s
              and (a.lead_id = %(lid)s::uuid or a.source_lead_id = %(lid)s::uuid)
            limit 1""",
        {"t": TENANT, "lid": str(lead_id)})
    row = cur.fetchone()
    if not row:
        return None
    cur.execute(
        """select disposition, noted_at from landos.lead_outcomes
            where lead_id = %s order by noted_at desc nulls last limit 5""",
        (row["source_lead_id"],))
    row["_dispositions"] = [d["disposition"] for d in cur.fetchall() if d.get("disposition")]
    return row


def build(cur, lead_id):
    """lead_id -> a grounding dict. Returns None if the lead does not resolve.

    Shape: {acq_lead_id, engine_lead_id, high_score, facts, hash}. `facts` is an ordered
    map key -> {value, field}; ONLY non-null facts appear. `hash` covers the fact values
    so a cached brief regenerates exactly when the grounded inputs change."""
    row = fetch(cur, lead_id)
    if row is None:
        return None

    facts = {}

    def add(key, value, field):
        # honest absence: '', [], None, and whitespace-only strings yield no fact.
        if value is None:
            return
        if isinstance(value, str) and not value.strip():
            return
        if isinstance(value, (list, dict)) and len(value) == 0:
            return
        facts[key] = {"value": value, "field": field}

    pm = row["parcel_meta"] if isinstance(row["parcel_meta"], dict) else {}
    ss = row["score_signals"] if isinstance(row["score_signals"], dict) else {}

    add("owner_name", (row["owner_name"] or "").strip() or None, "acquisition_leads.owner_name")
    add("parcel_apn", row["parcel_apn"], "acquisition_leads.parcel_apn")
    add("county_fips", row["county_fips"], "acquisition_leads.county_fips")
    add("situs_address", row["situs_address"], "acquisition_leads.situs_address")
    add("signal_tags", row["signal_tags"], "acquisition_leads.signal_tags")
    add("score_distress", _num(row["score_distress"]), "acquisition_leads.score_distress")
    add("score_owner", _num(row["score_owner"]), "acquisition_leads.score_owner")

    # absentee — the out-of-state mailing flag the projection already computed.
    oos = _jget(pm, "owner_out_of_state")
    if oos is not None:
        add("owner_out_of_state", bool(oos), "acquisition_leads.parcel_meta.owner_out_of_state")
    mail = _jget(pm, "owner_mailing")
    if isinstance(mail, dict) and (mail.get("city") or mail.get("state")):
        add("owner_mailing", {k: mail.get(k) for k in ("city", "state") if mail.get(k)},
            "acquisition_leads.parcel_meta.owner_mailing")

    # recipe/senior signals (senior_lakefront_assemblage etc.)
    add("senior_score", _num(ss.get("senior_score")), "acquisition_leads.score_signals.senior_score")
    add("tenure_years", _num(ss.get("tenure_years")), "acquisition_leads.score_signals.tenure_years")
    add("dist_to_lake_m", _num(ss.get("dist_to_lake_m")), "acquisition_leads.score_signals.dist_to_lake_m")

    # cluster / FAMILY flag (assemblage)
    cluster = _jget(pm, "cluster")
    if isinstance(cluster, dict):
        add("cluster_size", cluster.get("cluster_size"), "acquisition_leads.parcel_meta.cluster.cluster_size")
        if cluster.get("same_surname_family"):
            add("same_surname_family", True, "acquisition_leads.parcel_meta.cluster.same_surname_family")
    recipe_id = _jget(pm, "recipe", "recipe_id")
    add("recipe_id", recipe_id, "acquisition_leads.parcel_meta.recipe.recipe_id")

    add("amount_owed", _num(row["amount_owed"]), "acquisition_leads.amount_owed")
    add("assessed_total", _num(row["assessed_total"]), "acquisition_leads.assessed_total")
    add("acreage", _num(_jget(pm, "acreage")), "acquisition_leads.parcel_meta.acreage")
    add("card_state", row["card_state"], "acquisition_leads.card_state")
    add("stage", row["stage"], "acquisition_leads.stage")

    # portfolio hub / multi-parcel count — the "this is a HOLDER not a seller" rail.
    hub_n = _hub_count(row["portfolio_hub"])
    add("hub_parcel_count", hub_n, "acquisition_leads.portfolio_hub")
    add("controlling_human", row["controlling_human"], "acquisition_leads.controlling_human")

    # environmental
    if row["requires_bea"]:
        add("requires_bea", True, "acquisition_leads.requires_bea")
    add("contamination_kind", row["contamination_kind"], "acquisition_leads.contamination_kind")
    if row["commercial_agent_flag"]:
        add("commercial_agent_flag", True, "acquisition_leads.commercial_agent_flag")

    # the hard compliance / vintage rails (drive do_not_say). Fire on the STORED vintage
    # flag OR the owner-name pattern — parcel_owner only covers the loaded counties, so a
    # 'COUNTY OF … TREASURER' on an unloaded county would otherwise slip the govt rail and
    # a VA could pitch a treasurer as a seller (the exact landmine).
    if (row["vintage_flag"] or "") == "govt":
        add("vintage_flag_govt", True, "parcel_owner.vintage_flag")
    elif classify_owner_type(row["owner_name"] or "") == "govt":
        add("vintage_flag_govt", True, "acquisition_leads.owner_name (govt name pattern)")
    if row["dnc"]:
        add("dnc", True, "acquisition_leads.dnc")
    add("has_phone", bool((row["phone_1"] or "").strip()) or None, "acquisition_leads.phone_1")
    add("call_dispositions", row["_dispositions"] or None, "landos.lead_outcomes.disposition")

    return {
        "acq_lead_id": str(row["lead_id"]),
        "engine_lead_id": str(row["source_lead_id"]) if row["source_lead_id"] else None,
        "high_score": _num(row["score_distress"]) is not None and float(row["score_distress"]) >= DEFAULT_HIGH_SCORE,
        "facts": facts,
        "hash": hash_facts(facts),
    }


def hash_facts(facts):
    """Stable sha256 over the fact VALUES (not the prose) — the cache regenerates exactly
    when a grounded input changes, never merely because the model was re-invoked."""
    payload = {k: v["value"] for k, v in sorted(facts.items())}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _num(v):
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None

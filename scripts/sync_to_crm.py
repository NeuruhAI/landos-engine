"""
sync_to_crm.py — P4 of the Contact Resolver directive (D1 v2 §6): the sync
projection. THE SOLE WRITER of contact/pierce data into public.acquisition_leads.

    python -m scripts.sync_to_crm

Reads landos.owner_leads (the engine's source of truth — READ-ONLY here) and
projects onto public.acquisition_leads, matched on
acquisition_leads.source_lead_id = owner_leads.lead_id. UPDATE-only for the curated set
(never deletes), so the CRM's row set and its RLS/assignment workflow stay untouched. The
ONE exception is the 'discovered' intake tier: leads minted from county tax-sale signals
(provenance='discovered_taxsale') that have no acquisition_leads row yet are INSERTED here
with status='discovered' — so the single-writer rule holds (this is the only writer of
acquisition_leads). Every other unmatched owner_lead is still left to the CRM's own intake.
Idempotent: re-running converges to the same state.

Two layers, different gates:

  ENRICHMENT (any row that has it):
    outreach_recipe, controlling_human (skip_trace_meta->controlling_human.name),
    commercial_agent_flag, situs_lat/situs_lng (map pins),
    portfolio_hub — computed here by normalizing controlling-human names
    (first+last token, middle initials dropped) across the qualified set; a
    human spanning 2+ parcels gets "<Display Name> — hub: N parcels" on every
    one of their rows. A caller opening BLUE STAR 12 sees the Colovos
    portfolio, not just the LLC.

  CONTACT (callable rows ONLY — the Q2/compliance gate):
    phone_1/phone_1_type/phone_2/contact_confidence, written only when
    dnc_status is exactly clear AND
    skip_trace_status in (traced_web, traced_paid) AND
    confidence_score >= THRESHOLD. All other rows have their contact columns
    actively NULLed, so a row that falls below callable loses its projected
    phones on the next sync (the projection never serves stale dialables).
"""
import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL
from app.jarvis_brief import compose_brief
from scripts.seller_contact import upsert_seller_contact_phone

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
OPERATOR_SMOKE_TEST_LEAD_ID = "90391cd1-e792-4fa1-bc18-78818d55cde2"
THRESHOLD = 0.6  # {{CONFIDENCE_THRESHOLD}} — same cutoff the importer validates
# Phase B b1: assessed/SEV -> market ratio by state FIPS. MI SEV is ~50% of true
# cash value (statute) -> x2. Unmapped state -> equity stays NULL (pending), never guessed.
MARKET_MULT = {"26": 2.0}


def dnc_verdict(raw):
    """Project the scrub verdict without inventing one.

    FALSE means an explicit fresh scrub passed. TRUE means an explicit DNC hit.
    Anything else is unknown and must stay NULL/fail-closed in the CRM.
    """
    status = str(raw or "").strip().lower()
    if status == "clear":
        return False
    if status == "do_not_call":
        return True
    return None

# ENRICH-1 P1 (2026-07-05): owner_name projection uses the asset_class idiom —
# EXISTING-FIRST coalesce. Enrichment fills blanks (NULL or '') and NEVER overwrites
# a name already on the board (manually entered or previously projected). Deliberate
# tradeoff: an engine-side name CORRECTION no longer propagates onto a board row that
# already carries a name; blanks-only is the doctrine. Single source for the fragment
# so the unit test exercises the exact SQL the sync runs.
OWNER_NAME_PROJECTION_SQL = "owner_name = coalesce(nullif(owner_name, ''), %s)"

# --- asset_class normalizer (moat boundary: fold any adapter's value to the canonical six) ---
CANONICAL_ASSET_CLASSES = {"waterfront", "acreage", "vacant_land", "residential", "commercial", "unknown"}
ASSET_CLASS_ALIASES = {
    # MI tax-sale.info already emits the canonical six -> no entries needed.
    # Add ONE line per future-adapter variant when the WARN below surfaces it.
    # "land": "vacant_land", "vacant": "vacant_land", "house": "residential",
}


def normalize_asset_class(raw, *, source=None, parcel=None, logger=None):
    """Fold any adapter's asset_class into the canonical six. NULL preserved
    (legacy/unclassified). 'unknown' is a valid distinct value. Unmapped non-null
    -> 'unknown' + WARN (surfaces the string for a one-line alias add; never halts).
    Source-agnostic: when asset_class is promoted to an owner_leads column, only the
    CALL SITE's source expression changes, not this logic."""
    if raw is None:
        return None
    v = str(raw).strip().lower().replace(" ", "_").replace("-", "_")
    if v in CANONICAL_ASSET_CLASSES:
        return v
    if v in ASSET_CLASS_ALIASES:
        return ASSET_CLASS_ALIASES[v]
    msg = (f"[asset_class] unmapped {raw!r} (norm {v!r}) source={source} "
           f"parcel={parcel} -> 'unknown'; add an alias line")
    (logger.warning if logger else print)(msg)
    return "unknown"


# Williamson TX (PDF-sourced) has no land-use code — it carries raw_meta->>'property_flag'
# instead of ->>'asset_class'. Map it to the canonical six (operator-confirmed 2026-07-04).
PROPERTY_FLAG_ASSET_CLASS = {
    "land": "vacant_land", "land_improved": "vacant_land",
    "mh_only": "residential", "mh_on_land": "residential",
}


def disc_asset_class_from_raw(raw_meta, parcel=None):
    """Canonical asset_class for a discovered-tier lead. Prefers the native raw_meta->>'asset_class'
    (MI tax-sale mint); falls back to raw_meta->>'property_flag' (Williamson TX). None if neither."""
    rm = raw_meta or {}
    native = rm.get("asset_class")
    if native:
        return normalize_asset_class(native, source="taxsale", parcel=parcel)
    pf = str(rm.get("property_flag") or "").strip().lower()
    mapped = PROPERTY_FLAG_ASSET_CLASS.get(pf)
    if mapped:
        return normalize_asset_class(mapped, source="taxsale_property_flag", parcel=parcel)
    return None


def hub_key(name: str) -> str:
    """'PETER S. COLOVOS' / 'Peter Colovos' -> 'PETER COLOVOS' (first+last,
    middle initials dropped) so registry spelling variants merge."""
    toks = [t for t in re.sub(r"[^A-Za-z ]", " ", name or "").upper().split() if len(t) > 1]
    return f"{toks[0]} {toks[-1]}" if len(toks) >= 2 else " ".join(toks)


def display_name(variants: Counter) -> str:
    """Most common raw variant, title-cased: 'PETER S COLOVOS' -> 'Peter S Colovos'."""
    raw = variants.most_common(1)[0][0]
    return " ".join(w.capitalize() for w in raw.split())


def parcel_meta_from_raw(rm):
    """Curated county deal-fields for the /map card (the 'discovered' tier) — NOT the whole raw
    blob. Keys confirmed against owner_leads.raw_meta (county cert-sale payload): normalized
    top-level land_av/improvement_av/amount_due/prop_type/legal_desc/township/status/acreage/
    tax_info_url; the cert/pay/lookup URLs under raw_meta->'raw' as PARCPRC/PAYTAXURL/PROVALWBLK."""
    if not rm:
        return None
    raw = rm.get("raw") or {}

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    land, improv = num(rm.get("land_av")), num(rm.get("improvement_av"))
    total = (land or 0) + (improv or 0) if (land is not None or improv is not None) else None
    return {
        "assessed_land_value": land,
        "assessed_improvement_value": improv,
        "assessed_total": total,
        "amount_owed": num(rm.get("amount_due")),
        "prop_type": rm.get("prop_type"),
        "legal_desc": rm.get("legal_desc"),
        "township": rm.get("township"),
        "county_status": rm.get("status"),
        "acreage": num(rm.get("acreage")),
        "cert_pdf_url": raw.get("PARCPRC"),
        "pay_tax_url": raw.get("PAYTAXURL"),
        "tax_info_url": rm.get("tax_info_url"),
        "parcel_lookup_url": raw.get("PROVALWBLK"),
    }


# --- recipe intake (spec §9.2 — senior_lakefront_assemblage_v1) ---------------------
# Rows minted by scripts/mint_recipe_leads.py carry provenance='discovered_recipe' and a
# raw_meta the minter stowed (recipe_id/run_id + per-parcel recipe measures). They ride
# the SAME discovered-tier path as the tax-sale mint — single-writer rule intact — only
# the jsonb composition differs: cluster stats -> parcel_meta, recipe measures ->
# score_signals, tags -> signal_tags (text[]), source stamped 'recipe:<recipe_id>'.

def is_recipe_meta(rm):
    """True when raw_meta was stowed by the recipe minter (recipe_id is its marker)."""
    return bool((rm or {}).get("recipe_id"))


def parcel_meta_from_recipe(rm):
    """§9.2: cluster stats -> parcel_meta jsonb. NO tax-sale keys are fabricated —
    downstream reads of amount_owed/assessed_total must .get() (honest absent)."""
    return {
        "recipe": {"recipe_id": rm.get("recipe_id"), "run_id": rm.get("run_id")},
        "cluster": {
            "cluster_id": rm.get("cluster_id"),
            "cluster_size": rm.get("cluster_size"),
            "same_surname_family": rm.get("same_surname_family"),
        },
        "tenure_floor": rm.get("tenure_floor"),
        "owner_occupied_proxy": rm.get("owner_occupied_proxy"),
    }


def score_signals_from_recipe(rm):
    """§9.2: senior_score / tenure_years / dist_to_lake_m -> score_signals jsonb.
    tenure_years None = SDF-window-dark (floor-scored), never a fabricated 0."""
    return {
        "senior_score": rm.get("senior_score"),
        "tenure_years": rm.get("tenure_years"),
        "dist_to_lake_m": rm.get("dist_to_lake_m"),
        "recipe_id": rm.get("recipe_id"),
        "run_id": rm.get("run_id"),
    }


def recipe_signal_tags(rm):
    """§9.2: signal_tags text[] — e.g. ['senior_tenure','lake_proximity','assemblage']."""
    tags = rm.get("signal_tags") or []
    return [t for t in tags if t] or None


def discovered_signal_tags(raw_meta, is_disc):
    """Discovery-minted rows (mint_discovery: ground_lease / mine_exempt / …) stow their
    lens + signal tags in raw_meta.signal_tags. Read them so the discovered-tier INSERT —
    signal_tags is NOT NULL on the board — never lands a null. Tax-sale discovered rows
    carry no such key -> None; default_signal_tags supplies their thesis tag at the bind."""
    if not is_disc:
        return None
    tags = (raw_meta or {}).get("signal_tags") or []
    return [t for t in tags if t] or None


def default_signal_tags(provenance):
    """acquisition_leads.signal_tags is NOT NULL. When a discovered row carries no explicit
    tags — an unmatched TAX-SALE parcel (the Vanderburgh-502 landmine) — default to the tag
    that names its thesis so the INSERT never trips NOT NULL and the board never shows an
    empty motivation. Mirrors the source->'engine_sync' mapping: a correct default at the
    bind, not a crash six hundred rows deep. Any other tag-less discovered provenance falls
    back to ['discovered'] (honest, non-null)."""
    p = (provenance or "").lower()
    if "taxsale" in p or "tax_sale" in p or "tax_delinquent" in p:
        return ["tax_delinquent"]
    return ["discovered"]


def recipe_source(rm):
    """acquisition_leads.source for a recipe-minted lead.

    §9.2 originally specified source='recipe:<recipe_id>', but public.acquisition_leads
    carries a controlled-vocabulary CHECK (acquisition_leads_source_check): source ∈
    {engine_sync, manual, public_found, inbound_form, cold_caller}. Every engine-synced
    lead (1,516+ tax-sale discovered rows) uses 'engine_sync'; recipe leads are engine-
    synced too, so they take the same value. Recipe provenance is not lost — it lives in
    landos.owner_leads.source ('recipe:<recipe_id>', reachable via source_lead_id), in
    signal_tags, and in parcel_meta's recipe block.
    """
    return "engine_sync"


# public.acquisition_leads.source is a controlled vocabulary — CHECK
# acquisition_leads_source_check, verified live 2026-07-14 — and it is NOT NULL with
# DEFAULT 'engine_sync'. This is the ONE place the allowed set is named; both the
# pre-insert guard and the --check-constraints probe read it. If the DB CHECK ever
# widens, change it HERE in the same commit that migrates the constraint.
ALLOWED_SOURCES = ("engine_sync", "manual", "public_found", "inbound_form", "cold_caller")


def assert_source_allowed(source):
    """JOB 2 — guard acquisition_leads_source_check BEFORE a row reaches the DB. The
    lesson of the recipe CheckViolation: fail readably HERE (naming the bad value + the
    allowed set), not with a raw psycopg 23514 six hundred rows into a sync. None is
    legal: on the UPDATE path it means 'no change' (coalesce keeps the existing value),
    and on a bare INSERT the column DEFAULT ('engine_sync') applies. A stray non-null
    value — e.g. the old 'recipe:<id>' — raises."""
    if source is None or source in ALLOWED_SOURCES:
        return source
    raise ValueError(
        f"acquisition_leads.source={source!r} violates acquisition_leads_source_check "
        f"(allowed: {list(ALLOWED_SOURCES)}, or NULL). Recipe provenance rides in "
        f"signal_tags / score_signals.recipe / parcel_meta.recipe / landos.owner_leads.source "
        f"— never the CRM source enum; map it to 'engine_sync'.")


# The discovered-tier INSERT (single-writer rule: this is the ONLY path that creates an
# acquisition_leads row). Named once so the --check-constraints probe (JOB 3) exercises
# the EXACT SQL the sync runs — a dry-run that never touches the DB cannot validate DB
# acceptance, so the probe must round-trip THIS statement. status='discovered' is a
# literal; 26 placeholders follow. Contact fields + dnc are included so a newly
# discovered row cannot transiently claim dnc=false without the clean phone that
# earned that verdict.
DISCOVERED_INSERT_SQL = """
    insert into public.acquisition_leads
      (source_lead_id, tenant_id, status, owner_name, parcel_apn, county_fips,
       situs_lat, situs_lng, situs_address, emerging_score, emerging_signals,
       parcel_meta, amount_owed, assessed_total, jarvis_brief, asset_class,
       market_value_est, equity_pct, taxes_annual,
       score_signals, signal_tags, source,
       phone_1, phone_1_type, phone_2, contact_confidence, dnc)
    values (%s, %s, 'discovered', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

# The columns DISCOVERED_INSERT_SQL binds, in VALUES order ('status' is a literal, so it
# is NOT here). Zipped with the value tuple to guard the bind against the live schema.
DISCOVERED_INSERT_COLS = (
    "source_lead_id", "tenant_id", "owner_name", "parcel_apn", "county_fips",
    "situs_lat", "situs_lng", "situs_address", "emerging_score", "emerging_signals",
    "parcel_meta", "amount_owed", "assessed_total", "jarvis_brief", "asset_class",
    "market_value_est", "equity_pct", "taxes_annual",
    "score_signals", "signal_tags", "source",
    "phone_1", "phone_1_type", "phone_2", "contact_confidence", "dnc")

# The live NOT NULL set on public.acquisition_leads (verified 2026-07-14). Of these, only
# the columns this INSERT actually binds (signal_tags, source) can trip a 23502 — the rest
# (lead_id/requires_bea/stage/funded/card_state) are omitted from the INSERT so their
# DEFAULT applies. dnc is deliberately nullable with no default after migration 080.
# Named here so the guard validates the bind against the live schema.
ACQ_NOT_NULL_COLS = frozenset({
    "lead_id", "requires_bea", "signal_tags", "stage", "source", "funded", "card_state"})


def assert_insert_row_valid(values):
    """JOB 1 (defuse) — sibling of assert_source_allowed: validate the discovered-tier INSERT
    bind against the live NOT NULL schema BEFORE the row reaches the DB. A None bound to a
    NOT NULL column — signal_tags for an unmatched tax-sale row was the Vanderburgh-502
    landmine — raises a readable error naming the column, never a raw psycopg 23502 mid-mint.
    Also re-checks source against the CHECK vocabulary. Returns the row dict."""
    row = dict(zip(DISCOVERED_INSERT_COLS, values))
    nulls = [c for c, v in row.items() if c in ACQ_NOT_NULL_COLS and v is None]
    if nulls:
        raise ValueError(
            f"discovered-tier INSERT would violate NOT NULL on {nulls} "
            f"(source_lead_id={row.get('source_lead_id')!r}). Every NOT NULL bind must be "
            f"non-null: signal_tags defaults to a thesis tag (['tax_delinquent'] for tax-sale, "
            f"['discovered'] otherwise), source to 'engine_sync'.")
    assert_source_allowed(row["source"])
    return row


def main(dry_run: bool = False, lead_ids=None) -> None:
    lead_filter = list(lead_ids) if lead_ids else None
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """select lead_id, owner_name, situs_lat, situs_lng, skip_trace_status,
                      confidence_score,
                      skip_trace_meta->>'outreach_recipe'                  as recipe,
                      skip_trace_meta->'controlling_human'->>'name'        as ch_name,
                      (skip_trace_meta->>'commercial_agent_flag')::boolean as caf,
                      skip_trace_meta->>'dnc_status'                       as dnc_status,
                      skip_trace_meta->'phones'                            as phones,
                      signal_overlap, owner_type, emerging_score, emerging_signals,
                      provenance, parcel_apn, county_fips, raw_meta, situs_address,
                      owner_mailing_address, owner_mailing_city, owner_mailing_state, owner_mailing_zip
               from landos.owner_leads
               where tenant_id = %s and not discovery_seed
                 and (%s::uuid[] is null or lead_id = any(%s))""", (TENANT, lead_filter, lead_filter))
        leads = cur.fetchall()

        # latest composite score per lead — for the score_* projection (one-way,
        # landos -> CRM). NULL components stay NULL (arbitrage = "no comps yet", not 0).
        cur.execute("""
            with latest as (
              select distinct on (identity_node_id) identity_node_id, score,
                     owner_component, distress_component, arbitrage_component,
                     top_factors, weights_version, inputs_snapshot, computed_at
              from landos.scores where tenant_id = %s
              order by identity_node_id, computed_at desc)
            select ol.lead_id, l.score, l.owner_component, l.distress_component,
                   l.arbitrage_component, l.top_factors, l.weights_version,
                   l.inputs_snapshot, l.computed_at
            from landos.owner_leads ol
            join landos.identity_nodes n on n.tenant_id = %s and n.parcel_apn = ol.parcel_apn
            join latest l on l.identity_node_id = n.id
            where ol.tenant_id = %s and not ol.discovery_seed""",
            (TENANT, TENANT, TENANT))
        scores = {row["lead_id"]: row for row in cur.fetchall()}

        # kin / heirs (contact_relations) for the Jarvis brief — sparse (MISSING -> []).
        cur.execute("""
            select cr.owner_lead_id, cr.relative_name, cr.relationship, cr.is_heir
            from landos.contact_relations cr
            join landos.owner_leads ol on ol.lead_id = cr.owner_lead_id
            where ol.tenant_id = %s and not ol.discovery_seed""", (TENANT,))
        kin_map = defaultdict(list)
        for kr in cur.fetchall():
            kin_map[kr["owner_lead_id"]].append(
                {"name": kr["relative_name"], "relationship": kr["relationship"],
                 "is_heir": kr["is_heir"]})

        # Phase B b1: tax-sale valuation provenance per lead (sev, min_bid,
        # current_tax) from its matched taxsale signals — the source of the equity
        # the CRM displays. Keyed by owner_leads.lead_id (= acquisition_leads.source_lead_id).
        # guarded cast: raw_payload amounts vary — "TBA" (pending TX sales) and
        # dollar/comma-formatted ("$23.02"). Strip $ , and spaces, then regex-match a
        # number or yield NULL — never crash, never coerce garbage.
        _num = lambda expr: (f"(case when regexp_replace({expr}, '[$, ]', '', 'g') ~ '^-?[0-9]+(\\.[0-9]+)?$' "
                             f"then regexp_replace({expr}, '[$, ]', '', 'g')::numeric end)")
        _minbid = "coalesce(ds.raw_payload->>'min_bid', ds.raw_payload->>'minimum_bid')"
        cur.execute(f"""
            select ol.lead_id, left(ol.county_fips, 2) as st,
                   max({_num("ds.raw_payload->>'sev'")})          as sev,
                   min({_num(_minbid)})                            as min_bid,
                   max({_num("ds.raw_payload->>'current_tax'")})  as current_tax
            from landos.owner_leads ol
            join landos.discovery_signals ds
              on ds.matched_lead_id = ol.lead_id and ds.source_type = 'taxsale'
            where ol.tenant_id = %s and not ol.discovery_seed
            group by ol.lead_id, left(ol.county_fips, 2)""", (TENANT,))
        valuation = {row["lead_id"]: row for row in cur.fetchall()}

        as_of = datetime.now(timezone.utc).isoformat()
        jarvis_briefs = []

        # ---- portfolio hubs across the qualified set -------------------------
        groups = defaultdict(lambda: {"count": 0, "variants": Counter()})
        for r in leads:
            if r["ch_name"]:
                k = hub_key(r["ch_name"])
                if k:
                    groups[k]["count"] += 1
                    groups[k]["variants"][r["ch_name"].strip()] += 1
        hub_label = {k: f"{display_name(g['variants'])} — hub: {g['count']} parcels"
                     for k, g in groups.items() if g["count"] > 1}

        enriched = phones_set = phones_cleared = hubs_stamped = unmatched = 0
        dnc_suppressed = bea_flagged = score_projected = jarvis_composed = 0
        discovered_inserted = recipe_stamped = 0
        for r in leads:
            # DNC GATE (compliance, hard): only an explicit `clear` verdict can
            # become callable. unchecked/unknown/NULL are not clean; do_not_call
            # is suppressed. Zero information never becomes permission.
            projected_dnc = dnc_verdict(r["dnc_status"])
            is_dnc = projected_dnc is True
            callable_ = (projected_dnc is False
                         and r["skip_trace_status"] in ("traced_web", "traced_paid")
                         and r["confidence_score"] is not None
                         and float(r["confidence_score"]) >= THRESHOLD)
            if is_dnc:
                dnc_suppressed += 1
            p1 = p1t = p2 = conf = None
            if callable_:
                ph = sorted(r["phones"] or [],
                            key=lambda p: -(p.get("confidence") or 0))
                if ph:
                    p1, p1t = ph[0].get("number"), ph[0].get("type")
                    p2 = ph[1].get("number") if len(ph) > 1 else None
                    conf = float(r["confidence_score"])
            hub = hub_label.get(hub_key(r["ch_name"])) if r["ch_name"] else None

            # Part-201 / brownfield BEA badge (HARD SCOPE: only these two columns).
            so = r["signal_overlap"]
            if isinstance(so, str):
                try:
                    so = json.loads(so)
                except ValueError:
                    so = {}
            stack = (so.get("distress_stack") if isinstance(so, dict) else None) or []
            requires_bea = any(t in ("environmental_part201", "brownfield") for t in stack)
            contamination_kind = ("part201" if "environmental_part201" in stack
                                  else "brownfield" if "brownfield" in stack else None)

            # Composite component projection (ONE-WAY landos -> CRM). NULL stays NULL
            # (arbitrage NULL = "no comps yet", never a priced 0). The blended composite
            # lives inside score_signals.composite_score; final_lead_score (absentee rank)
            # is NEVER touched. score_signals (jsonb) carries the per-lead basis.
            sc = scores.get(r["lead_id"])
            if sc is not None:
                snap = sc["inputs_snapshot"] or {}
                s_owner, s_distress = sc["owner_component"], sc["distress_component"]
                s_arb = sc["arbitrage_component"]            # NULL = no comps yet, never 0
                signals_dict = {
                    "composite_score": float(sc["score"]) if sc["score"] is not None else None,
                    "weights_version": sc["weights_version"],
                    "confidence": snap.get("confidence"),
                    "coverage": snap.get("coverage"),
                    "top_factors": sc["top_factors"],
                    "requires_bea": snap.get("requires_bea"),
                    "contamination_kind": snap.get("contamination_kind"),
                    # JOB 8: post-floor call-disposition modifier baked into composite_score
                    # above (null when no disposition) — surfaced so the board can explain
                    # why a lead moved (e.g. "raised: owner interested"). Audit copy lives
                    # in landos.scores.inputs_snapshot.outcome_modifier.
                    "outcome_modifier": snap.get("outcome_modifier"),
                    "computed_at": sc["computed_at"].isoformat() if sc["computed_at"] else None,
                }
                s_signals = Jsonb(signals_dict)
                alt_use = (((snap.get("strategy_fit") or {}).get("alt_use_fitness") or {}).get("score"))
            else:
                s_owner = s_distress = s_arb = s_signals = signals_dict = alt_use = None

            # recipe intake (§9.2): recipe-minted discovered rows have no landos.scores
            # row — score_signals carries the recipe measures instead (synthesized every
            # run: idempotent, convergent; a later real composite in landos.scores wins).
            is_disc = (r.get("provenance") or "").startswith("discovered")
            recipe_rm = r["raw_meta"] if (is_disc and is_recipe_meta(r["raw_meta"])) else None
            if recipe_rm and s_signals is None:
                signals_dict = score_signals_from_recipe(recipe_rm)
                s_signals = Jsonb(signals_dict)

            # ---- Jarvis brief (engine-composed; projected, NOT an LLM call here) ----
            brief = compose_brief({
                "controlling_human": r["ch_name"], "portfolio_hub": hub,
                "recipe": r["recipe"], "owner_type": r["owner_type"], "owner_name": r["owner_name"],
                "score_owner": s_owner, "score_distress": s_distress, "score_signals": signals_dict,
                "score_arbitrage": s_arb, "alt_use_fitness": alt_use,
                "requires_bea": requires_bea, "contamination_kind": contamination_kind,
                "kin": kin_map.get(r["lead_id"], []),
            }, as_of)
            jarvis_briefs.append((r, brief))

            # discovered-tier county data (the 87 tax-sale leads only; curated 136 stay NULL).
            # coalesce(%s, col) in the UPDATE => passing None for the 136 keeps their value.
            # recipe intake (§9.2): recipe-minted rows get recipe-shaped jsonb, not tax keys.
            sig_tags = (recipe_signal_tags(recipe_rm) if recipe_rm
                        else discovered_signal_tags(r["raw_meta"], is_disc))
            lead_source = recipe_source(recipe_rm) if recipe_rm else None
            # UPDATE keeps source unchanged when lead_source is None (coalesce below); an
            # INSERT cannot — source is NOT NULL — so the discovered tier defaults to
            # 'engine_sync' (it IS an engine sync). Guard BOTH values against the live CHECK
            # before either statement runs (JOB 2): a stray source dies here, readably.
            insert_source = lead_source or "engine_sync"
            assert_source_allowed(lead_source)     # UPDATE path (None = no change)
            assert_source_allowed(insert_source)   # INSERT path (never NULL)
            # signal_tags is NOT NULL on the board. UPDATE coalesces (None keeps existing);
            # the INSERT cannot, so a tag-less discovered row (unmatched tax-sale) defaults
            # to its thesis tag. This defuses the Vanderburgh-502 landmine at the bind.
            insert_sig_tags = sig_tags or default_signal_tags(r.get("provenance"))
            if recipe_rm:
                pm_dict = parcel_meta_from_recipe(recipe_rm)
            else:
                pm_dict = parcel_meta_from_raw(r["raw_meta"]) if (is_disc and r["raw_meta"]) else None
            if pm_dict and r.get("owner_mailing_address"):
                # owner mailing (step 4d) -> on the card; out-of-state mailing = absentee signal
                st = (r.get("owner_mailing_state") or "").strip().upper()
                pm_dict["owner_mailing"] = {"address": r["owner_mailing_address"],
                                            "city": r.get("owner_mailing_city"),
                                            "state": r.get("owner_mailing_state"),
                                            "zip": r.get("owner_mailing_zip")}
                pm_dict["owner_out_of_state"] = bool(st) and st != "IN"
            pm = Jsonb(pm_dict) if pm_dict else None
            disc_addr = r["situs_address"] if is_disc else None
            # FIX-1 (2026-07-01): discovered rows minted name-less get their enriched
            # owner_name projected on UPDATE too (INSERT-only projection left 174 blank).
            # ENRICH-1 P1: stripped-to-None so a whitespace-only engine value can never
            # land on the board as a "name".
            disc_owner = ((r["owner_name"] or "").strip() or None) if is_disc else None
            # typed, sortable: amount_owed + assessed_total also as numeric cols (kept in pm too)
            # .get: recipe-shaped parcel_meta carries NO tax keys (honest absent, never 0)
            owed = pm_dict.get("amount_owed") if pm_dict else None
            assessed = pm_dict.get("assessed_total") if pm_dict else None
            # asset_class: carried NATIVELY on the discovered tier, normalized to the canonical six.
            # Source today is raw_meta->>'asset_class' (the mint stows the true six there); when it
            # graduates to an owner_leads column, only this expression changes. Curated UPDATE path
            # is left untouched (those 136 have no tax-sale raw_meta -> would normalize to NULL anyway).
            disc_asset_class = (disc_asset_class_from_raw(r["raw_meta"], r["parcel_apn"])
                                if is_disc else None)

            # ---- Phase B b1 valuation projection (SEV x2 - min_bid) --------------
            # market = SEV x state ratio (MI only); equity = (market - lien)/market.
            # Unknown lien (min_bid null) => equity NULL (pending), never a false 100%.
            # Negatives (lien > market) are written TRUE (underwater), operator-confirmed.
            v = valuation.get(r["lead_id"])
            market_value_est = equity_pct = taxes_annual = None
            if v:
                ratio = MARKET_MULT.get(v["st"])
                sev, min_bid = v["sev"], v["min_bid"]
                taxes_annual = float(v["current_tax"]) if v["current_tax"] is not None else None
                if ratio and sev and float(sev) > 0:
                    market_value_est = round(float(sev) * ratio, 2)
                    if min_bid is not None:
                        equity_pct = round((market_value_est - float(min_bid)) / market_value_est * 100, 1)

            if dry_run:
                continue   # compose-only preview; no write

            cur.execute(
                f"""update public.acquisition_leads set
                     outreach_recipe = %s, controlling_human = %s,
                     commercial_agent_flag = %s, portfolio_hub = %s,
                     situs_lat = %s, situs_lng = %s,
                     phone_1 = %s, phone_1_type = %s, phone_2 = %s,
                     contact_confidence = %s,
                     dnc = case
                       /* Operator-owned smoke-test cell, not a seller. Never let
                          an automated registry receipt promote it to FALSE.
                          Preserve TRUE if it is ever hard-suppressed. */
                       when lead_id = '90391cd1-e792-4fa1-bc18-78818d55cde2'::uuid
                         then case when dnc is true then true else null end
                       when dnc is true then true
                       else %s
                     end,
                     requires_bea = %s, contamination_kind = %s,
                     score_owner = %s, score_distress = %s,
                     score_arbitrage = %s, score_signals = %s,
                     emerging_score = %s, emerging_signals = %s,
                     {OWNER_NAME_PROJECTION_SQL},
                     situs_address = coalesce(%s, situs_address),
                     parcel_meta = coalesce(%s, parcel_meta),
                     amount_owed = coalesce(%s, amount_owed),
                     assessed_total = coalesce(%s, assessed_total),
                     asset_class = coalesce(asset_class, %s),
                     market_value_est = coalesce(market_value_est, %s),
                     equity_pct = coalesce(equity_pct, %s),
                     taxes_annual = coalesce(taxes_annual, %s),
                     signal_tags = coalesce(%s, signal_tags),
                     source = coalesce(%s, source),
                     jarvis_brief = %s
                   where source_lead_id = %s""",
                (r["recipe"], r["ch_name"], r["caf"], hub,
                 r["situs_lat"], r["situs_lng"], p1, p1t, p2, conf, projected_dnc,
                 requires_bea, contamination_kind,
                 s_owner, s_distress, s_arb, s_signals,
                 r["emerging_score"],
                 Jsonb(r["emerging_signals"]) if r["emerging_signals"] is not None else None,
                 disc_owner, disc_addr, pm, owed, assessed,
                 disc_asset_class,
                 market_value_est, equity_pct, taxes_annual,
                 sig_tags, lead_source,
                 Jsonb(brief), r["lead_id"]))
            if cur.rowcount == 0:
                # The curated set stays UPDATE-only. The ONE exception: the 'discovered' intake
                # tier — leads minted from county tax-sale signals (provenance='discovered_taxsale')
                # are INSERTED here (single-writer rule). status='discovered' keeps them visibly
                # separate from the curated set; every other unmatched owner_lead is still left to
                # the CRM's own intake (warn + skip).
                if (r.get("provenance") or "").startswith("discovered"):
                    insert_values = (
                        r["lead_id"], TENANT, r["owner_name"], r["parcel_apn"], r["county_fips"],
                        r["situs_lat"], r["situs_lng"], disc_addr, r["emerging_score"],
                        Jsonb(r["emerging_signals"]) if r["emerging_signals"] is not None else None,
                        pm, owed, assessed, Jsonb(brief), disc_asset_class,
                        market_value_est, equity_pct, taxes_annual,
                        s_signals if recipe_rm else None, insert_sig_tags, insert_source,
                        p1, p1t, p2, conf, projected_dnc)
                    assert_insert_row_valid(insert_values)   # NOT NULL + CHECK guard, pre-DB
                    cur.execute(DISCOVERED_INSERT_SQL, insert_values)
                    discovered_inserted += 1
                    jarvis_composed += 1
                    if recipe_rm:
                        recipe_stamped += 1
                    continue
                unmatched += 1
                print(f"  WARN no acquisition_leads row for {r['owner_name']!r} "
                      f"({r['lead_id']}) — not inserted (CRM owns its row set)")
                continue
            enriched += 1
            jarvis_composed += 1
            if recipe_rm:
                recipe_stamped += 1
            if requires_bea:
                bea_flagged += 1
            if sc is not None and s_owner is not None:
                score_projected += 1
            if p1:
                phones_set += 1
            elif r["skip_trace_status"] != "not_traced":
                phones_cleared += 1
            if hub:
                hubs_stamped += 1

        # === A8.2 CONTACT BRIDGE (RULING 2026-07-14) — sole writer of spine seller
        # contacts. Every callable lead gets its seller send-target row; a
        # suppressed lead's spine-owned phone is retracted (consent rail). Then the
        # leak assertion fails LOUDLY if any callable lead is still contactless. ===
        if dry_run:
            # A dry-run is read-only in fact, not merely a transaction that writes
            # and later rolls back. Report the current bridge surface without
            # calling either writer.
            cur.execute(CALLABLE_OR_SUPPRESSED, (TENANT,))
            contact_candidates = len(cur.fetchall())
            cur.execute(ORPHAN_CALLABLE, (TENANT,))
            contact_orphans = len(cur.fetchall())
            conn.rollback()
            print(f"\n=== CONTACT BRIDGE DRY-RUN — candidates={contact_candidates} "
                  f"orphans={contact_orphans}, NO writes ===")
            print(f"\n=== JARVIS BRIEF DRY-RUN — {len(jarvis_briefs)} composed, NO writes ===")
            for r, brief in jarvis_briefs[:5]:
                print(f"\nlead {r['lead_id']}  ({r['owner_name']})")
                print(f"  headline: {brief['headline']}")
                print(f"  scores:   {brief['scores']}")
                print(f"  contamination: {brief['contamination']}   kin: {len(brief['kin'])}")
            print("\nHARD STOP — dry-run only (nothing written). Re-run without --dry-run to project.")
            return

        contact_stats = sync_seller_contacts(cur)
        # VISIBILITY: heal pipeline_slug + map coordinates for the whole book, then assert
        # every projected lead is visible before commit (fail loud, like the orphan check).
        pipe_n, coord_n = backfill_pipeline_and_coords(cur)
        assert_no_orphan_callable_leads(cur)
        assert_leads_visible(cur)
        conn.commit()
        print(f"\ncontact bridge: {dict(contact_stats)} "
              f"(seller send-target rows; source='spine_sync', single writer)")
        print(f"visibility: pipeline_slug='land' set on {pipe_n}, coords backfilled on {coord_n}")

    print(f"\nsync summary: enriched={enriched} discovered_inserted={discovered_inserted} "
          f"recipe_stamped={recipe_stamped} "
          f"phones_projected={phones_set} "
          f"non-callable_kept_phoneless={phones_cleared} dnc_suppressed={dnc_suppressed} "
          f"hub_labeled={hubs_stamped} bea_flagged={bea_flagged} "
          f"score_projected={score_projected} jarvis_composed={jarvis_composed} unmatched={unmatched}")
    print(f"hubs in effect ({len(hub_label)}):")
    for k in sorted(hub_label, key=lambda k: -groups[k]["count"]):
        print(f"  {hub_label[k]}")


CALLABLE_OR_SUPPRESSED = """
    select a.lead_id, a.owner_name, a.phone_1, a.dnc,
           exists (
             select 1
               from public.callable_leads cl
              where cl.lead_id = a.lead_id
           ) as callable
    from public.acquisition_leads a
    where a.tenant_id = %s
      and (
        (a.phone_1 is not null and a.phone_1 <> '')
        or a.dnc is true
        or exists (
          select 1 from public.contacts c
          where c.related_lead_id = a.lead_id
            and c.contact_type = 'seller'
            and c.source = 'spine_sync'
        )
      )
"""

# JOB 4 — the leak assertion. A callable lead (phone on the board, not DNC) with
# no seller contact carrying a phone is UNTEXTABLE — the exact class of leak the
# ruling closes. It must never be silent again.
ORPHAN_CALLABLE = """
    select cl.lead_id, cl.owner_name
    from public.callable_leads cl
    where cl.tenant_id = %s
      and not exists (
        select 1 from public.contacts c
        where c.related_lead_id = cl.lead_id
          and c.contact_type = 'seller'
          and coalesce(c.phone, '') <> '')
"""


def sync_seller_contacts(cur):
    """The A8.2 bridge pass — sole writer of spine seller contacts. Idempotent:
    callable leads get (or keep) a seller send-target row; suppressed leads have
    their spine-owned phone retracted. Human contacts are never touched."""
    cur.execute(CALLABLE_OR_SUPPRESSED, (TENANT,))
    leads = cur.fetchall()
    stats = Counter()
    for L in leads:
        # The strict view owns the complete decision: explicit scrub-clean,
        # nonblank phone, no call/all opt-out, and manual-lead consent.
        callable_ = bool(L["callable"])
        outcome = upsert_seller_contact_phone(
            cur, lead_id=L["lead_id"], tenant_id=TENANT,
            owner_name=L["owner_name"], phone=L["phone_1"], callable=callable_)
        stats[outcome] += 1
    return stats


def assert_no_orphan_callable_leads(cur):
    """Errors-visible doctrine: raise LOUDLY if any callable lead lacks a seller
    contact. Runs inside the sync txn, so a violation aborts (rolls back) the run."""
    cur.execute(ORPHAN_CALLABLE, (TENANT,))
    orphans = cur.fetchall()
    if orphans:
        sample = ", ".join(f"{o['lead_id']} ({o['owner_name']!r})" for o in orphans[:5])
        raise AssertionError(
            f"CONTACT LEAK — {len(orphans)} callable lead(s) have NO seller contact "
            f"row (untextable): {sample}{' …' if len(orphans) > 5 else ''}. "
            f"The A8.2 bridge failed to close; aborting sync.")


# === VISIBILITY (MAKE THE LEADS VISIBLE) — a lead the operator cannot SEE is not a
# lead. Two failure modes, both fixed here (the sole writer), never a parallel update:
#   pipeline_slug NULL -> the CRM queue filters by pipeline, so the lead is an orphan;
#   situs_lat/lng NULL -> it cannot render on the map.
# Every engine-minted lead (source='engine_sync') is an APG LAND play (lakefront
# parcels, churches on acreage, dirt under towers) -> pipeline_slug='land' (an EXISTING
# slug; the lens stays distinguishable via signal_tags, which the queue filters on).
# Coordinates come from landos.parcel_geom.centroid (ST_PointOnSurface, guaranteed inside)
# joined on (county_fips, parcel_apn) — the same zero-fuzz key the geometry load uses.
PIPELINE_BACKFILL_SQL = """
    update public.acquisition_leads
       set pipeline_slug = 'land'
     where tenant_id = %s and pipeline_slug is null and source = 'engine_sync'
"""

COORDS_BACKFILL_SQL = """
    update public.acquisition_leads a
       set situs_lat = ST_Y(g.centroid), situs_lng = ST_X(g.centroid)
      from landos.parcel_geom g
     where a.tenant_id = %s
       and a.county_fips = g.county_fips
       and a.parcel_apn = g.parcel_apn
       and (a.situs_lat is null or a.situs_lng is null)
       and g.centroid is not null
"""

# The invariant's negative: an engine lead with no pipeline_slug, OR any lead missing
# coordinates WHILE a centroid exists for its parcel (an honest geometry-dark county is
# excluded — no centroid, nothing to backfill, not a bug).
LEADS_INVISIBLE = """
    select a.lead_id, a.owner_name,
           (a.pipeline_slug is null and a.source = 'engine_sync') as no_pipeline,
           ((a.situs_lat is null or a.situs_lng is null)
            and exists (select 1 from landos.parcel_geom g
                         where g.county_fips = a.county_fips and g.parcel_apn = a.parcel_apn
                           and g.centroid is not null)) as no_coords_but_geom
      from public.acquisition_leads a
     where a.tenant_id = %s
       and (
         (a.pipeline_slug is null and a.source = 'engine_sync')
         or ((a.situs_lat is null or a.situs_lng is null)
             and exists (select 1 from landos.parcel_geom g
                          where g.county_fips = a.county_fips and g.parcel_apn = a.parcel_apn
                            and g.centroid is not null))
       )
"""


def backfill_pipeline_and_coords(cur):
    """Set pipeline_slug='land' on the engine's slug-less leads and backfill coordinates
    from the parcel centroid. Set-based (all tenant leads, not just this run's) — the
    map/queue heal for the whole book. Returns (pipeline_set, coords_set)."""
    cur.execute(PIPELINE_BACKFILL_SQL, (TENANT,))
    pipe = cur.rowcount
    cur.execute(COORDS_BACKFILL_SQL, (TENANT,))
    coords = cur.rowcount
    return pipe, coords


def assert_leads_visible(cur):
    """Errors-visible doctrine (JOB 3): raise LOUDLY before commit if any lead is invisible
    — an engine lead with no pipeline_slug, or a lead lacking coordinates though its parcel
    HAS a centroid. Runs inside the sync txn, so a violation aborts (rolls back) the run.
    Honest geometry-dark counties (no centroid) are not violations — nothing to render from."""
    cur.execute(LEADS_INVISIBLE, (TENANT,))
    bad = cur.fetchall()
    if bad:
        sample = ", ".join(f"{b['lead_id']} ({b['owner_name']!r})" for b in bad[:5])
        raise AssertionError(
            f"INVISIBLE LEADS — {len(bad)} lead(s) the operator cannot see "
            f"(no pipeline_slug and/or no map coordinates where geometry exists): "
            f"{sample}{' …' if len(bad) > 5 else ''}. The backfill failed to heal them; "
            f"aborting sync.")


def backfill_visibility(dry_run: bool = False):
    """Standalone one-shot (mirrors --backfill-contacts): make the existing book visible
    without re-running the full projection. Same writer, same invariant gate."""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        def census():
            cur.execute("""select
                count(*) filter (where pipeline_slug is null) no_pipeline,
                count(*) filter (where situs_lat is null or situs_lng is null) no_coords,
                count(*) filter (where situs_lat is not null and situs_lng is not null
                                   and pipeline_slug is not null) renderable
                from public.acquisition_leads where tenant_id=%s""", (TENANT,))
            return dict(cur.fetchone())
        before = census()
        print(f"[census] before: {before}")
        pipe_n, coord_n = backfill_pipeline_and_coords(cur)
        assert_leads_visible(cur)
        after = census()
        if dry_run:
            conn.rollback()
            print(f"[dry-run] would set pipeline_slug='land' on {pipe_n}, backfill coords on "
                  f"{coord_n} -> {after} (rolled back)")
            return
        conn.commit()
        print(f"visibility backfill: pipeline_slug='land' set on {pipe_n}, coords backfilled "
              f"on {coord_n} (single writer)")
        print(f"[census] after:  {after}")


def backfill_contacts(dry_run: bool = False):
    """JOB 3 — close the bridge for the existing orphans in the same motion
    (same writer, same shape). Census before/after; the leak assertion gates it."""
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        def census():
            cur.execute("select count(*) n from public.contacts "
                        "where contact_type='seller' and coalesce(phone,'')<>'' and tenant_id=%s", (TENANT,))
            with_phone = cur.fetchone()["n"]
            cur.execute(ORPHAN_CALLABLE, (TENANT,))
            orphans = len(cur.fetchall())
            return with_phone, orphans
        b_contacts, b_orphans = census()
        print(f"[census] before: seller contacts w/ phone={b_contacts}  callable-without-contact={b_orphans}")
        stats = sync_seller_contacts(cur)
        assert_no_orphan_callable_leads(cur)
        a_contacts, a_orphans = census()
        if dry_run:
            conn.rollback()
            print(f"[dry-run] would: {dict(stats)} -> contacts w/ phone={a_contacts} orphans={a_orphans} (rolled back)")
            return
        conn.commit()
        print(f"[census] after:  seller contacts w/ phone={a_contacts}  callable-without-contact={a_orphans}")
        print(f"contact backfill: {dict(stats)} (source='spine_sync', single writer)")


def check_constraints():
    """The lesson of the recipe CheckViolation, made a standing check: a dry-run that never
    touches the database cannot validate database acceptance. This mode round-trips synthetic
    discovered-tier rows through a REAL transaction against public.acquisition_leads — the
    EXACT DISCOVERED_INSERT_SQL the sync runs, exercising every NOT NULL, CHECK and DEFAULT
    the live table enforces — then ROLLS BACK, so nothing persists. It proves what a zero-DB
    dry-run could not:
      (1) the mapped source ('engine_sync') + a real tag set is DB-legal,
      (2) the live CHECK REJECTS the old 'recipe:<id>' source, and
      (3) the live NOT NULL REJECTS a null signal_tags — the Vanderburgh-502 landmine —
          AND the in-code guard (assert_insert_row_valid) catches both before the DB does.
    Exit 1 on any surprise. The pre-DB guard is the first line; the DB is the backstop."""
    from psycopg import Rollback

    PROBE_ID = "0eadbeef-0000-4000-8000-000000000001"  # a source_lead_id no real row uses

    def _params(source, signal_tags):
        # synthetic, type-valid values for every bound column (26).
        return (PROBE_ID, TENANT, "PROBE — rolled back", "PROBE-APN", "18091",
                None, None, None, None, None,
                Jsonb({"recipe": {"recipe_id": "probe"}}), None, None, Jsonb({"probe": True}), "unknown",
                None, None, None,
                Jsonb({"recipe_id": "probe"}), signal_tags, source,
                None, None, None, None, None)

    def probe(conn, cur, source, signal_tags):
        try:
            with conn.transaction():           # SAVEPOINT — isolates this probe
                cur.execute(DISCOVERED_INSERT_SQL, _params(source, signal_tags))
                raise Rollback                 # accepted by the DB -> undo, never persist
            return True, None
        except psycopg.errors.Error as e:      # the DB refused the row (CHECK/NOT NULL/…)
            return False, e

    def guard_rejects(source, signal_tags):
        """True if the in-code assert_insert_row_valid rejects this bind (no DB touched)."""
        try:
            assert_insert_row_valid(_params(source, signal_tags))
            return False
        except ValueError:
            return True

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        ok, ok_err = probe(conn, cur, "engine_sync", ["tax_delinquent"])   # legal shape -> accept
        bad_src, bad_src_err = probe(conn, cur, "recipe:probe", ["x"])     # old source -> reject
        bad_tag, bad_tag_err = probe(conn, cur, "engine_sync", None)       # null tags  -> reject
        conn.rollback()                                                    # nothing sticks

    src_check = (bad_src is False and isinstance(bad_src_err, psycopg.errors.CheckViolation)
                 and getattr(bad_src_err.diag, "constraint_name", "") == "acquisition_leads_source_check")
    tag_notnull = (bad_tag is False and isinstance(bad_tag_err, psycopg.errors.NotNullViolation)
                   and getattr(bad_tag_err.diag, "column_name", "") == "signal_tags")
    # the in-code guard must catch BOTH before the DB ever sees them.
    guard_src = guard_rejects("recipe:probe", ["x"])
    guard_tag = guard_rejects("engine_sync", None)

    print(f"\n{'='*70}\n--check-constraints: real-txn round-trip vs the LIVE table (rolled back)\n{'='*70}")
    print(f"  [DB] source=engine_sync + tags=['tax_delinquent'] -> " +
          ("ACCEPTED (DB-legal)" if ok else f"REJECTED — UNEXPECTED: {ok_err}"))
    print(f"  [DB] source='recipe:probe'                        -> " +
          ("REJECTED by acquisition_leads_source_check ✓" if src_check
           else ("ACCEPTED — UNEXPECTED!" if bad_src else f"REJECTED, wrong reason: {bad_src_err}")))
    print(f"  [DB] signal_tags=NULL                             -> " +
          ("REJECTED by NOT NULL (signal_tags) ✓" if tag_notnull
           else ("ACCEPTED — UNEXPECTED (the landmine is live!)" if bad_tag else f"REJECTED, wrong reason: {bad_tag_err}")))
    print(f"  [in-code guard] rejects bad source                -> {'✓' if guard_src else 'MISS'}")
    print(f"  [in-code guard] rejects null signal_tags          -> {'✓' if guard_tag else 'MISS'}")
    passed = ok and src_check and tag_notnull and guard_src and guard_tag
    print(f"\n  RESULT: " + ("PASS — shape DB-legal; source CHECK + signal_tags NOT NULL are real; "
                             "the in-code guard catches both first." if passed else "FAIL — see above."))
    if not passed:
        raise SystemExit(1)
    return passed


# === DEPRECATED — DO NOT CALL (superseded 2026-06-22, AXON Emergence Loop step 2) ===
# emerging_score / emerging_signals are now projected by main() above, read STRAIGHT from
# landos.owner_leads (written by scripts/score_emergence.py — the single source of the moat
# scoring). The function below FABRICATES its own score (base*0.7 + sig_conf*0.3, with a
# blanket downweight) across every lead — it violates the never-fabricate / single-source
# rails and would clobber the real scores on all 136 if wired up. Left in place only for the
# is_emerging / dale_local heat-map columns it also touches (no producer for those yet).
# Leave it OFF; do not uncomment the call below.
#
# === Emerging Intel / Heat Map extension (fleshed out for CRM module) ===
# Projects data from LandOS emerging ingest (scripts/ingest_emerging_opportunities.py)
# into public.acquisition_leads (or dedicated public.emerging_opportunities).
# Follows exact P4 pattern: read-only from landos, idempotent UPDATEs, no inserts/deletes.
# Run after the main enrichment/contact sync, or integrate into the same loop.
# Columns match the 017_emerging_intel_heat_map.sql migration.

def sync_emerging(conn, tenant_id: str, limit: int = 2000) -> None:
    """Sync emerging_score, signals, dale_local etc. from landos (via owner_leads or dedicated emerging table).
    Assumes landos has emerging data populated (e.g. from ingest_emerging + discovery adapters for deltas/negative/boomer/dale).
    In full impl, join on lead_id or use a view that includes the emerging fields from the ingest output.
    """
    print("\n[emerging] syncing pre-market boomer heat map data (LandOS → CRM public)...")
    cur = conn.cursor()
    cur.execute("""
        select 
          ol.lead_id,
          ol.final_lead_score as base_score,
          -- In real use, these come from the emerging ingest output / dedicated table
          -- For demo, we synthesize from owner_leads + discovery_signals if present.
          -- Extend ingest_emerging to also write a landos.emerging_opportunities table with these.
          coalesce( (select max( (ds.raw_payload->>'confidence')::float ) from landos.discovery_signals ds where ds.matched_lead_id = ol.lead_id), 0 ) as max_signal_conf,
          (select bool_or( (ds.source_type like 'permit%' or ds.source_type like 'satellite%') ) from landos.discovery_signals ds where ds.matched_lead_id = ol.lead_id ) as has_delta,
          (select bool_or( ds.source_type = 'negative_space' ) from landos.discovery_signals ds where ds.matched_lead_id = ol.lead_id ) as has_negative,
          (select bool_or( ds.source_type like 'boomer%' ) from landos.discovery_signals ds where ds.matched_lead_id = ol.lead_id ) as has_boomer,
          -- DALE local flag (proprietary edge) — set true in ingest when dale_ground signal present
          false as dale_local  -- placeholder; wire from DALE export in ingest_emerging
        from landos.owner_leads ol
        where ol.tenant_id = %s
          and (ol.final_lead_score is not null or exists (select 1 from landos.discovery_signals ds where ds.matched_lead_id = ol.lead_id))
        order by ol.final_lead_score desc nulls last
        limit %s
    """, (tenant_id, limit))

    emerging_rows = cur.fetchall()
    projected = 0
    for r in emerging_rows:
        lead_id = r["lead_id"]
        # Compute simple emerging_score (in prod, use the one from ingest_emerging which already fuses deltas + negative + boomer + DALE)
        # Here we blend base LandOS score with signal strength as demo.
        base = float(r["base_score"] or 50)
        sig_conf = float(r["max_signal_conf"] or 0)
        emerging_score = min(100, max(0, base * 0.7 + (sig_conf * 100) * 0.3 ))
        if not r["has_delta"] and not r["has_negative"] and not r["has_boomer"]:
            emerging_score = max(0, emerging_score - 15)  # downweight if no emerging signal

        signals = {}
        if r["has_delta"]:
            signals["satellite_delta" if "satellite" in str(r) else "permit_delta"] = {"confidence": round(sig_conf, 2)}
        if r["has_negative"]:
            signals["negative_space"] = True
        if r["has_boomer"]:
            signals["boomer_life_event"] = {"note": "life event cross"}
        if r["dale_local"]:
            signals["dale_local"] = True

        cur.execute(
            """update public.acquisition_leads set
                 emerging_score = %s,
                 emerging_signals = %s,
                 is_emerging = %s,
                 dale_local = %s
               where source_lead_id = %s""",
            (round(emerging_score, 1),
             json.dumps(signals) if signals else None,
             len(signals) > 0 or emerging_score > 65,
             r["dale_local"],
             lead_id)
        )
        if cur.rowcount > 0:
            projected += 1

    conn.commit()
    print(f"[emerging] projected {projected} emerging records (heat map layers ready for CRM /emerging and map)")

# Call from main() after the existing sync logic, e.g.:
# if __name__ == "__main__":
#     main()
#     # after the conn block in main:
#     # sync_emerging(conn, tenant_id)

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="landos -> CRM projection (single writer)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compose + preview Jarvis briefs (5); write nothing")
    ap.add_argument("--lead-id", action="append",
                    help="project only this landos.owner_leads lead_id; repeatable")
    ap.add_argument("--backfill-contacts", action="store_true",
                    help="run ONLY the A8.2 seller-contact bridge pass (census + gated)")
    ap.add_argument("--backfill-visibility", action="store_true",
                    help="run ONLY the pipeline_slug + map-coordinate backfill (census + invariant)")
    ap.add_argument("--check-constraints", action="store_true",
                    help="round-trip a synthetic row through a REAL txn + ROLLBACK; prove the "
                         "projection is DB-legal and the source CHECK is real (writes nothing)")
    args = ap.parse_args()
    if args.check_constraints:
        check_constraints()
    elif args.backfill_contacts:
        backfill_contacts(dry_run=args.dry_run)
    elif args.backfill_visibility:
        backfill_visibility(dry_run=args.dry_run)
    else:
        main(dry_run=args.dry_run, lead_ids=args.lead_id)

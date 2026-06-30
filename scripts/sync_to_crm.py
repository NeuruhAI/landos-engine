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

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
THRESHOLD = 0.6  # {{CONFIDENCE_THRESHOLD}} — same cutoff the importer validates

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


def main(dry_run: bool = False) -> None:
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
               where tenant_id = %s and not discovery_seed""", (TENANT,))
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
        discovered_inserted = 0
        for r in leads:
            # DNC GATE (compliance, hard): a lead scrubbed 'do_not_call' NEVER
            # becomes callable — phones are not projected regardless of trace
            # status or confidence. 'unchecked'/'clear'/None pass (the dialer
            # does the fresh pre-dial scrub; this only blocks known-DNC).
            is_dnc = (r["dnc_status"] or "").strip().lower() == "do_not_call"
            callable_ = (not is_dnc
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
                    "computed_at": sc["computed_at"].isoformat() if sc["computed_at"] else None,
                }
                s_signals = Jsonb(signals_dict)
                alt_use = (((snap.get("strategy_fit") or {}).get("alt_use_fitness") or {}).get("score"))
            else:
                s_owner = s_distress = s_arb = s_signals = signals_dict = alt_use = None

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
            is_disc = (r.get("provenance") or "").startswith("discovered")
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
            # typed, sortable: amount_owed + assessed_total also as numeric cols (kept in pm too)
            owed = pm_dict["amount_owed"] if pm_dict else None
            assessed = pm_dict["assessed_total"] if pm_dict else None
            # asset_class: carried NATIVELY on the discovered tier, normalized to the canonical six.
            # Source today is raw_meta->>'asset_class' (the mint stows the true six there); when it
            # graduates to an owner_leads column, only this expression changes. Curated UPDATE path
            # is left untouched (those 136 have no tax-sale raw_meta -> would normalize to NULL anyway).
            disc_asset_class = (normalize_asset_class((r["raw_meta"] or {}).get("asset_class"),
                                                      source="taxsale", parcel=r["parcel_apn"])
                                if is_disc else None)

            if dry_run:
                continue   # compose-only preview; no write

            cur.execute(
                """update public.acquisition_leads set
                     outreach_recipe = %s, controlling_human = %s,
                     commercial_agent_flag = %s, portfolio_hub = %s,
                     situs_lat = %s, situs_lng = %s,
                     phone_1 = %s, phone_1_type = %s, phone_2 = %s,
                     contact_confidence = %s,
                     requires_bea = %s, contamination_kind = %s,
                     score_owner = %s, score_distress = %s,
                     score_arbitrage = %s, score_signals = %s,
                     emerging_score = %s, emerging_signals = %s,
                     situs_address = coalesce(%s, situs_address),
                     parcel_meta = coalesce(%s, parcel_meta),
                     amount_owed = coalesce(%s, amount_owed),
                     assessed_total = coalesce(%s, assessed_total),
                     jarvis_brief = %s
                   where source_lead_id = %s""",
                (r["recipe"], r["ch_name"], r["caf"], hub,
                 r["situs_lat"], r["situs_lng"], p1, p1t, p2, conf,
                 requires_bea, contamination_kind,
                 s_owner, s_distress, s_arb, s_signals,
                 r["emerging_score"],
                 Jsonb(r["emerging_signals"]) if r["emerging_signals"] is not None else None,
                 disc_addr, pm, owed, assessed,
                 Jsonb(brief), r["lead_id"]))
            if cur.rowcount == 0:
                # The curated set stays UPDATE-only. The ONE exception: the 'discovered' intake
                # tier — leads minted from county tax-sale signals (provenance='discovered_taxsale')
                # are INSERTED here (single-writer rule). status='discovered' keeps them visibly
                # separate from the curated set; every other unmatched owner_lead is still left to
                # the CRM's own intake (warn + skip).
                if (r.get("provenance") or "").startswith("discovered"):
                    cur.execute(
                        """insert into public.acquisition_leads
                             (source_lead_id, tenant_id, status, owner_name, parcel_apn, county_fips,
                              situs_lat, situs_lng, situs_address, emerging_score, emerging_signals,
                              parcel_meta, amount_owed, assessed_total, jarvis_brief, asset_class)
                           values (%s, %s, 'discovered', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        (r["lead_id"], TENANT, r["owner_name"], r["parcel_apn"], r["county_fips"],
                         r["situs_lat"], r["situs_lng"], disc_addr, r["emerging_score"],
                         Jsonb(r["emerging_signals"]) if r["emerging_signals"] is not None else None,
                         pm, owed, assessed, Jsonb(brief), disc_asset_class))
                    discovered_inserted += 1
                    jarvis_composed += 1
                    continue
                unmatched += 1
                print(f"  WARN no acquisition_leads row for {r['owner_name']!r} "
                      f"({r['lead_id']}) — not inserted (CRM owns its row set)")
                continue
            enriched += 1
            jarvis_composed += 1
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

        if dry_run:
            conn.rollback()
            print(f"\n=== JARVIS BRIEF DRY-RUN — {len(jarvis_briefs)} composed, NO writes ===")
            for r, brief in jarvis_briefs[:5]:
                print(f"\nlead {r['lead_id']}  ({r['owner_name']})")
                print(f"  headline: {brief['headline']}")
                print(f"  scores:   {brief['scores']}")
                print(f"  contamination: {brief['contamination']}   kin: {len(brief['kin'])}")
            print("\nHARD STOP — dry-run only (nothing written). Re-run without --dry-run to project.")
            return
        conn.commit()

    print(f"\nsync summary: enriched={enriched} discovered_inserted={discovered_inserted} "
          f"phones_projected={phones_set} "
          f"non-callable_kept_phoneless={phones_cleared} dnc_suppressed={dnc_suppressed} "
          f"hub_labeled={hubs_stamped} bea_flagged={bea_flagged} "
          f"score_projected={score_projected} jarvis_composed={jarvis_composed} unmatched={unmatched}")
    print(f"hubs in effect ({len(hub_label)}):")
    for k in sorted(hub_label, key=lambda k: -groups[k]["count"]):
        print(f"  {hub_label[k]}")


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
    main(dry_run=ap.parse_args().dry_run)

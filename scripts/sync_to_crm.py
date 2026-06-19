"""
sync_to_crm.py — P4 of the Contact Resolver directive (D1 v2 §6): the sync
projection. THE SOLE WRITER of contact/pierce data into public.acquisition_leads.

    python -m scripts.sync_to_crm

Reads landos.owner_leads (the engine's source of truth — READ-ONLY here) and
projects onto public.acquisition_leads, matched on
acquisition_leads.source_lead_id = owner_leads.lead_id. Pure UPDATEs — never
inserts, never deletes, so the CRM's row set and its RLS/assignment workflow
are untouched. Idempotent: re-running converges to the same state.

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
import json
import re
from collections import Counter, defaultdict

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL

TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"
THRESHOLD = 0.6  # {{CONFIDENCE_THRESHOLD}} — same cutoff the importer validates


def hub_key(name: str) -> str:
    """'PETER S. COLOVOS' / 'Peter Colovos' -> 'PETER COLOVOS' (first+last,
    middle initials dropped) so registry spelling variants merge."""
    toks = [t for t in re.sub(r"[^A-Za-z ]", " ", name or "").upper().split() if len(t) > 1]
    return f"{toks[0]} {toks[-1]}" if len(toks) >= 2 else " ".join(toks)


def display_name(variants: Counter) -> str:
    """Most common raw variant, title-cased: 'PETER S COLOVOS' -> 'Peter S Colovos'."""
    raw = variants.most_common(1)[0][0]
    return " ".join(w.capitalize() for w in raw.split())


def main() -> None:
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn, conn.cursor() as cur:
        cur.execute(
            """select lead_id, owner_name, situs_lat, situs_lng, skip_trace_status,
                      confidence_score,
                      skip_trace_meta->>'outreach_recipe'                  as recipe,
                      skip_trace_meta->'controlling_human'->>'name'        as ch_name,
                      (skip_trace_meta->>'commercial_agent_flag')::boolean as caf,
                      skip_trace_meta->>'dnc_status'                       as dnc_status,
                      skip_trace_meta->'phones'                            as phones,
                      signal_overlap
               from landos.owner_leads
               where tenant_id = %s and not discovery_seed""", (TENANT,))
        leads = cur.fetchall()

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
        dnc_suppressed = bea_flagged = 0
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

            cur.execute(
                """update public.acquisition_leads set
                     outreach_recipe = %s, controlling_human = %s,
                     commercial_agent_flag = %s, portfolio_hub = %s,
                     situs_lat = %s, situs_lng = %s,
                     phone_1 = %s, phone_1_type = %s, phone_2 = %s,
                     contact_confidence = %s,
                     requires_bea = %s, contamination_kind = %s
                   where source_lead_id = %s""",
                (r["recipe"], r["ch_name"], r["caf"], hub,
                 r["situs_lat"], r["situs_lng"], p1, p1t, p2, conf,
                 requires_bea, contamination_kind, r["lead_id"]))
            if cur.rowcount == 0:
                unmatched += 1
                print(f"  WARN no acquisition_leads row for {r['owner_name']!r} "
                      f"({r['lead_id']}) — not inserted (CRM owns its row set)")
                continue
            enriched += 1
            if requires_bea:
                bea_flagged += 1
            if p1:
                phones_set += 1
            elif r["skip_trace_status"] != "not_traced":
                phones_cleared += 1
            if hub:
                hubs_stamped += 1
        conn.commit()

    print(f"\nsync summary: enriched={enriched} phones_projected={phones_set} "
          f"non-callable_kept_phoneless={phones_cleared} dnc_suppressed={dnc_suppressed} "
          f"hub_labeled={hubs_stamped} bea_flagged={bea_flagged} unmatched={unmatched}")
    print(f"hubs in effect ({len(hub_label)}):")
    for k in sorted(hub_label, key=lambda k: -groups[k]["count"]):
        print(f"  {hub_label[k]}")


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
    main()

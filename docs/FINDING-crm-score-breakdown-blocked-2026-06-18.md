# FINDING — CRM score breakdown projection BLOCKED (Phase 9 parked)

**Date:** 2026-06-18 · **Engine:** landos-engine → lvdi · **Lane:** engine (terminal)
**Decision:** `score_owner / score_distress / score_arbitrage / score_signals` on
`public.acquisition_leads` are left **NULL**. The `landos.scores` → CRM component
projection is **not safe to run** and is parked pending a design decision. The
contact/pierce/map sync (`scripts/sync_to_crm.py`) is unaffected.

## Request
Extend the engine→CRM sync to project `landos.scores` components
(`owner_component`, `distress_component`, `arbitrage_component`, `top_factors`)
onto `acquisition_leads.score_*`, "using the same join that already writes
`final_lead_score = scores.score`."

## Why it was stopped — three premises failed against live lvdi data

1. **`final_lead_score` is NOT `scores.score`.** On **136/136** CRM leads,
   `acquisition_leads.final_lead_score` equals `landos.owner_leads.final_lead_score`
   exactly — the **absentee / reachability** rank from `owner_leads.py`, a different
   model than the distress/owner/arbitrage composite in `landos.scores`. No code
   projects `final_lead_score`; the values came from the owner_leads path, not a
   scores join. (`sync_to_crm.main()` writes contact/pierce/map columns only.)

2. **`landos.scores` does not cover the production leads.** 3,354 score rows /
   3,254 identity_nodes — geography ≈ 3,227 **St-Joseph-IN** + a block of `DEMO-*`
   synthetic parcels. The CRM's 136 leads are **124 Berrien-MI** (`26021`) + 12
   St-Joseph-IN (`18141`). Parcel overlap scores↔leads: **EXACT 0, NORMALIZED 0**.
   The composite scorer was **never pointed at the production lead set**.

3. **Projecting anyway = invented data.** With 0 matching score rows the components
   do not exist for these parcels; a forced join would stamp DEMO / St-Joseph values
   onto Berrien leads. That crosses the engine's no-invented-data line.

## Input-facts question — can the composite even be run on the real leads?

**No. The gap is upstream.** For the composite's three input axes, over the 136 leads:

| Axis | Source | Coverage on the 136 | Verdict |
|---|---|---|---|
| Owner | `owner_leads.absentee_score`, `skip_trace_meta` | absentee **136/136**, skip-trace **101/136** | partial |
| Distress | `landos.discovery_signals` / `signals` | discovery_signals matched **1/136** (14 rows total) | sparse |
| Arbitrage | `landos.comps` | **0 rows in the entire table** | **hard-absent** |

Plus: the composite (`app/scoring.score_parcel`) is **API-only** — inputs arrive in
the `/evaluate` request body; there is **no batch path** that assembles
distress/owner/arbitrage facts from tables and scores a lead set. (The only batch
scorer, `scripts/score_addresses.py`, runs the *edge* model — a different scorer.)

→ Lighting up the breakdown requires upstream work first: build a batch
input-assembly + composite-scoring path, and populate `comps` (arbitrage) and
distress signals for the Berrien / St-Joseph leads. Until then the components
cannot be computed for these leads, let alone projected. **Parked: Phase 9.**

## Reproduce
- Join recon: `landos.scores → identity_nodes(tenant,parcel_apn) → owner_leads.lead_id = acquisition_leads.source_lead_id`, latest score `distinct on (identity_node_id) … order by computed_at desc` → 0 resolved nodes / 0 scores for the 136.
- `final_lead_score` source: 136/136 equal `owner_leads.final_lead_score`, 0 equal `scores.score`.
- Overlap: distinct `parcel_apn` exact + normalized (`regexp_replace(lower(x),'[^a-z0-9]','','g')`) intersection = 0.
- Coverage: `landos.comps` count = 0; `discovery_signals` matched to the 136 source_lead_ids = 1.

---

## UPDATE 2026-06-18 (same day) — UPSTREAM HALF RESOLVED
The batch composite path was built and run; the 136 now carry real (partial, flagged)
composite scores in `landos.scores`. See `docs/composite-batch-scored-136-2026-06-18.md`.
**Correction to premise #2 above:** distress is **136/136**, not 1/136 — the input question
counted only `discovery_signals`; `owner_leads.signal_overlap` carries a grounded
`distress_stack` for every lead (33 map to a composite key, 103 are grounded-but-unmodeled:
`environmental_part201`/`brownfield`). The CRM projection (`acquisition_leads.score_*`)
remains **NULL by design**. **Later same day — default v3** added a `distress.environmental`
key (0.15): the 103 env/brownfield leads are now grounded at distress 15, so distress coverage
is **136/0/0** and composite median 3.75 → 9.75. Remaining coverage gap: arbitrage 0/136 +
owner age. Still parked pending the absentee-vs-composite headline decision.

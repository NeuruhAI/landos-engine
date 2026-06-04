"""
deep_report.py — max-effort per-parcel intelligence report (the deep pass above
the edge brief). Consumes a scored parcel + ALL its signals and emits a long,
structured Markdown analysis for a high-stakes acquisition decision.

    python -m scripts.deep_report <apn-or-address> [--geography berrien] [--effort max]

HARD DATA-FRESHNESS GUARD: runs on LIVE parcels only. A parcel whose only signal
is from a stale (>12 months) catalog (e.g. the 2022 Berrien catalog 2049) is
REFUSED. Acceptable: the 2026 DNR catalog (2906), any 2026 main catalog, or a
manually confirmed BH parcel (1850 Pipestone / 133 E Main).

The narrative is ATTRIBUTE-DRIVEN (lakefront / downtown / in-OZ / live-auction),
so the same generator produces a coherent report for a Shoreham lakefront auction
lot and a downtown QOZ monitoring parcel alike.

CRITICAL HONESTY: every data point is tagged DATA-GROUNDED or ESTIMATED. Real data
missing (comps, precise value, zoning until BS&A) is said plainly — never a
fabricated comp/value/zoning/gate. The bid thesis shows its work; no unsupported
number (and is N/A when the parcel isn't in a sale). Scoring is REUSED from the
engine — not reimplemented here.
"""
import argparse
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

import psycopg
from psycopg.rows import dict_row

from app.config import DATABASE_URL
from app.scoring import score_edge
from app.overlays import overlay_hits, _haversine_m
from app.multiplier import compute_multiplier
from app.gates import evaluate_gates, GATE_KEYS
from scripts.score_addresses import (
    derive_subscores, load_weights, load_overlays, geocode, _f, DOWNTOWN, WATERFRONT, TENANT,
)
from scripts.lead_queue import correlation_boost, GATE_SEVERITY
from app.comps import compute_arv, compute_max_bid

# ---- provenance tags --------------------------------------------------------
G = "`DATA-GROUNDED`"


def E(src: str) -> str:
    return f"`ESTIMATED — pending {src}`"


def U(src: str) -> str:
    return f"`UNKNOWN — pending {src}`"


# Always-allowed manually-confirmed targets (apn -> label, lat, lng)
CONFIRMED = {
    "11-54-0340-0151-02-5": ("133 E Main St, Benton Harbor", 42.115911, -86.457274),
    "11-03-5800-0001-00-4": ("1850 Pipestone Rd, Benton Harbor", 42.084782, -86.420077),
}
CONFIRMED_ADDR = {  # address keyword -> apn
    "133 e main": "11-54-0340-0151-02-5",
    "1850 pipestone": "11-03-5800-0001-00-4",
}
STALE_MONTHS = 12

# Known auction calendars by tax-sale.info catalog id (GROUNDED from research).
AUCTION_BY_CATALOG = {
    2906: {"auction": "2026-08-06, 10:00 EDT (auction 790)", "reoffer": "2026-09-25", "final": "2026-10-30"},
}

# Approx redevelopment anchors (proximity narrative only — ESTIMATED anchors)
HARBOR_SHORES = (42.1206, -86.4636)
DOWNTOWN_KM = 2.5  # within this of the BH core => "downtown" branch

# APG buy-box thresholds (documented business rules — GROUNDED policy inputs)
APG_RULES = (
    "APG offer thresholds (documented): perceived value <$50K → 50% initial / 55% max, "
    "must net $10K+ after costs; double-close/whole-tale never exceeds 70–75% (−10% costs)."
)


def _looks_like_apn(s: str) -> bool:
    return bool(re.match(r"^\s*\d{2}-", s)) or s.count("-") >= 3


def resolve(cur, token: str):
    """Return (apn, address, lat, lng, signals[])."""
    token = token.strip()
    if _looks_like_apn(token):
        apn = token
        cur.execute(
            "select * from landos.signals where tenant_id=%s and parcel_apn=%s order by signal_date desc",
            (TENANT, apn),
        )
        sigs = cur.fetchall()
        addr = sigs[0]["address"] if sigs else (CONFIRMED.get(apn, (None,))[0])
        lat = _f(sigs[0]["lat"]) if sigs else (CONFIRMED.get(apn, (None, None, None))[1])
        lng = _f(sigs[0]["lng"]) if sigs else (CONFIRMED.get(apn, (None, None, None))[2])
        return apn, addr, lat, lng, sigs

    cur.execute(
        "select * from landos.signals where tenant_id=%s and address ilike %s order by signal_date desc",
        (TENANT, f"%{token}%"),
    )
    sigs = cur.fetchall()
    if sigs:
        return sigs[0]["parcel_apn"], sigs[0]["address"], _f(sigs[0]["lat"]), _f(sigs[0]["lng"]), sigs
    key = next((k for k in CONFIRMED_ADDR if k in token.lower()), None)
    if key:
        apn = CONFIRMED_ADDR[key]
        label, lat, lng = CONFIRMED[apn]
        return apn, label, lat, lng, []
    lat, lng = geocode(token)
    return None, token, lat, lng, []


def freshness_guard(apn: Optional[str], sigs: List[dict], as_of: date) -> Tuple[bool, str, str]:
    """Return (allowed, vintage_line, refusal_msg)."""
    if apn in CONFIRMED:
        return True, f"Manually confirmed target — {CONFIRMED[apn][0]}; provenance: Berrien GIS + Census/HUD OZ.", ""
    dated = [s["signal_date"] for s in sigs if s.get("signal_date")]
    if not dated:
        return (False, "",
                "STALE PARCEL — no live signal on file. Deep report is for live parcels only. "
                "Pass a 2026-catalog parcel, the 2026 DNR parcel, or a confirmed address "
                "(1850 Pipestone / 133 E Main).")
    newest = max(dated)
    cutoff = date(as_of.year - 1, as_of.month, as_of.day)
    if newest < cutoff:
        return (False, "",
                f"STALE PARCEL — data vintage {newest}. Deep report is for live parcels only. "
                f"Pass a 2026-catalog parcel, the 2026 DNR parcel, or a confirmed address "
                f"(1850 Pipestone / 133 E Main).")
    return True, f"LIVE signal — newest signal_date {newest}, source `{sigs[0]['source_id']}`.", ""


def _nearest(overlay_type, lat, lng, overlays):
    best = None
    for ov in overlays:
        if ov["overlay_type"] != overlay_type:
            continue
        g = ov.get("geometry") or {}
        if g.get("type") == "Point":
            c = g["coordinates"]
            d = _haversine_m(lat, lng, c[1], c[0])
            best = d if best is None else min(best, d)
    return best


def load_comps(cur, apn):
    if not apn:
        return []
    cur.execute(
        "select * from landos.comps where tenant_id=%s and subject_apn=%s order by sale_date desc nulls last",
        (TENANT, apn),
    )
    return cur.fetchall()


# ===========================================================================
# Report assembly
# ===========================================================================
def build_report(parcel, sigs, overlays, weights, from_db, as_of, vintage_line, comps=None):
    apn, addr, lat, lng = parcel["apn"], parcel["address"], parcel["lat"], parcel["lng"]
    tally = {"grounded": 0, "estimated": 0}

    def grounded(n=1):
        tally["grounded"] += n

    def estimated(n=1):
        tally["estimated"] += n

    # ---- live-auction detection (drives calendar + bid thesis) ----
    cutoff = date(as_of.year - 1, as_of.month, as_of.day)
    tax_sigs = sorted(
        [s for s in sigs if s["signal_type"] == "tax_delinquent" and s.get("signal_date")],
        key=lambda x: x["signal_date"], reverse=True,
    )
    live_signal = next((s for s in tax_sigs if s["signal_date"] >= cutoff), None)
    live_auction = live_signal is not None
    rm = (live_signal or (sigs[0] if sigs else {})).get("raw_meta") or {} if (live_signal or sigs) else {}
    min_bid = rm.get("minimum_bid") if live_auction else None
    sev = rm.get("sev")
    legal = rm.get("legal")
    local_unit = rm.get("local_unit")
    catalog_id = rm.get("catalog_id")
    subject_acreage = rm.get("acreage")
    comps = comps or []

    # ---- reuse the engine scorer (no reimplementation) ----
    hits = overlay_hits(lat, lng, overlays)
    mult = compute_multiplier(hits, as_of=as_of)
    subs, audit = derive_subscores(lat, lng, hits, overlays)
    has_tax = any(s["signal_type"] == "tax_delinquent" for s in sigs)
    edge = score_edge({"base": subs, "distress": {"tax_delinquent": has_tax}}, weights)
    cb, scount, stypes = correlation_boost(sigs)
    gate_flags = {k: any(bool((s.get("raw_meta") or {}).get(k)) for s in sigs) for k in GATE_KEYS}
    gates = evaluate_gates(gate_flags)
    base, m = edge["base_score"], mult["multiplier"]
    final = 0.0 if gates["killed"] else round(max(0.0, min(100.0, base * m * cb)), 2)

    in_oz = any(h["overlay_type"] == "opportunity_zone" and h["provenance_status"] == "authoritative" for h in hits)
    oz_hit = next((h for h in hits if h["overlay_type"] == "opportunity_zone"), None)
    nb = _nearest("brownfield", lat, lng, overlays)
    nox = _nearest("ox_creek_corridor", lat, lng, overlays)
    d_harbor = _haversine_m(lat, lng, *HARBOR_SHORES) / 1000.0
    is_lakefront = bool(legal and "lake michigan" in legal.lower())
    is_downtown = audit["dist_downtown_km"] <= DOWNTOWN_KM

    L: List[str] = []
    a = L.append

    # 1) HEADER + DATA VINTAGE -------------------------------------------------
    a(f"# Deep Acquisition Report — {addr or apn}")
    a("")
    a(f"> **DATA VINTAGE / PROVENANCE:** {vintage_line}  \n"
      f"> Report generated {as_of.isoformat()} · tenant `{TENANT}` · profile `benton_harbor_edge` · effort=max")
    a("")
    a(f"**benton_harbor_edge SCORE: `{final:.2f}`**  (base `{base:.2f}` × OZ `{m:.2f}` × correlation `{cb:.2f}`)")
    grounded(4)
    a("")
    a("| Field | Value | Provenance |")
    a("|---|---|---|")
    a(f"| APN | `{apn or 'unresolved'}` | {G} (Berrien tax roll / GIS) |")
    a(f"| Address / place | {addr or '—'} | {G} |")
    a(f"| Centroid | {lat:.5f}, {lng:.5f} | {G} |")
    a(f"| Local unit | {local_unit or '—'} | {G if local_unit else U('BS&A / GIS')} |")
    a(f"| In tax sale now? | {'YES — catalog '+str(catalog_id) if live_auction else 'NO (monitoring target)'} | {G} |")
    a("")

    # 2) PARCEL DOSSIER (effort=max) ------------------------------------------
    a("## 2. Parcel Dossier")
    a("")
    a("### Geometry & lot context")
    if legal:
        a(f"- **Legal description** ({G}, catalog): {legal}")
        grounded()
    a(f"- **Acreage / zoning / dimensions**: {U('Berrien Parcels_Future / BS&A')} — not exposed by public REST.")
    estimated()
    if is_lakefront:
        a(f"- **Lake Michigan frontage**: legal bound runs to *the water's edge of Lake Michigan* → "
          f"**direct lake frontage** ({G}); not credited by the BH-centric location model (see §4).")
        grounded()
    elif is_downtown:
        a(f"- **Downtown core parcel**: ~{audit['dist_downtown_km']} km from the E Main core ({G}); a prime-location "
          f"infill/commercial context (exact use {U('BS&A zoning')}).")
        grounded()
    a("")
    a("### Signals on this parcel")
    if sigs:
        a("| Signal | Strength | Date | Source | Provenance |")
        a("|---|---|---|---|---|")
        for s in sigs:
            a(f"| {s['signal_type']} | {float(s['signal_strength']):.2f} | {s['signal_date']} | "
              f"`{s['source_id']}` | {G} |")
            grounded()
    else:
        a(f"- **No rows in `landos.signals`** for this parcel — a confirmed-address **monitoring** target, "
          f"not catalog-sourced. ({G})")
        grounded()
    a("")
    a("### Correlation-stack narrative  *(effort=max)*")
    if scount == 0:
        a(f"This parcel carries **no signals** in the engine today, so the correlation boost is `×{cb:.2f}` "
          f"(neutral) and the entire thesis rests on the **policy + location** layers below — not on distress. "
          f"That is the right read of a downtown QOZ lot: its edge is *where it is and what policy covers it*, "
          f"not (yet) a forced-sale trigger.")
        a("")
        a(f"**What would pull it into the live queue:** a `tax_delinquent` hit (next Berrien catalog), an SLBA "
          f"blight/land-bank signal, or a recorded lien. Any one converts this from a *monitoring* parcel into a "
          f"*sourced* one; two or more would stack the correlation boost. {E('future tax catalog + Tier-2 SLBA/lien normalizers')}")
        estimated()
    elif scount == 1:
        a(f"This parcel carries a **single signal** (`{'/'.join(stypes)}`) → correlation boost `×{cb:.2f}` "
          f"(neutral). The signal present — a **tax-foreclosure listing** — is a strong, time-boxed acquisition "
          f"trigger: the county is forcing a sale, the moment price dislocates from value. **What would stack it:** "
          f"an SLBA blight, lien, or vacancy signal — each an independent corroboration. {E('Tier-2 SLBA + lien normalizers')}")
        estimated()
    else:
        a(f"This parcel **stacks {scount} signals** across `{', '.join(stypes)}` → correlation boost `×{cb:.2f}`. "
          f"Independent signals converging on one parcel is the core thesis — each corroborates a different failure "
          f"mode (fiscal, physical, legal), and co-occurrence is far more predictive of a motivated, below-market "
          f"disposition than any one alone.")
    a("")

    # 3) POLICY OVERLAY ANALYSIS ----------------------------------------------
    a("## 3. Policy Overlay Analysis")
    a("")
    a("| Overlay | Status | Provenance | Applied? |")
    a("|---|---|---|---|")
    if oz_hit:
        a(f"| Opportunity Zone | **IN — {oz_hit['name']}** | {G} authoritative (TIGERweb×CDFI) | **yes, ×{m:.2f}** |")
    else:
        a(f"| Opportunity Zone | **NOT in a QOZ** (PIP vs 5 Berrien tracts) | {G} authoritative | no (×1.00) |")
    grounded()
    a(f"| Brownfield (EGLE) | nearest ≈ {('%.0f m' % nb) if nb is not None else 'n/a'} | {G} sourced_unconfirmed | logged ×1.0 |")
    a(f"| Ox Creek corridor (proxy) | nearest ≈ {('%.0f m' % nox) if nox is not None else 'n/a'} | {G} sourced_unconfirmed | logged ×1.0 |")
    a(f"| BH Housing TIF | boundary not published | {G} stub | n/a |")
    grounded(3)
    a("")
    if in_oz:
        a(f"**OZ federal-flux risk:** the §1400Z-2 capital-gains deferral recognition date is **2026-12-31** ({G}); "
          f"the engine auto-expires the OZ multiplier after that date. OZ rules are in legislative flux, so the uplift "
          f"is **time-boxed** — underwrite it as expiring, not permanent. {E('final OZ legislation')}")
        estimated()
    else:
        a(f"**OZ note:** no QOZ uplift here (×1.00). {G}")
    a("")

    # 4) LOCATION + FUTURE-POTENTIAL (effort=max) -----------------------------
    a("## 4. Location & Future-Potential Analysis")
    a("")
    a("| Metric | Value | Provenance |")
    a("|---|---|---|")
    a(f"| Distance to BH downtown core | {audit['dist_downtown_km']} km | {G} |")
    a(f"| Distance to BH waterfront anchor | {audit['dist_waterfront_km']} km | {G} |")
    a(f"| Distance to Harbor Shores | {d_harbor:.2f} km | {E('exact project boundary')} |")
    a(f"| location_score | {subs['location_score']:.3f} | {G} (formula) |")
    a(f"| future_potential | {subs['future_potential']:.3f} | {G} (formula) |")
    grounded(3); estimated()
    a("")
    if is_lakefront and subs["location_score"] < 0.4:
        a(f"**Model divergence (effort=max):** the location model is **BH-centric** (rewards proximity to downtown + "
          f"Jean Klock). This parcel sits ~{audit['dist_downtown_km']} km south, so the model returns a **low** "
          f"location_score ({subs['location_score']:.3f}). **But the legal grounds direct Lake Michigan frontage** — "
          f"a premium the anchors miss. Treat the score as a **floor**; true location value is plausibly far higher. "
          f"{E('Shoreham-aware anchor + waterfront comps')}")
        estimated()
        a("")
        a(f"**Redevelopment trajectory:** BH redevelopment (Harbor Shores, Ox Creek, SLBA demo at 489 North Shore Dr) "
          f"is concentrated ~{audit['dist_downtown_km']} km north — this parcel does **not** ride those tailwinds; its "
          f"upside is **lakefront/recreational**, a different thesis. {E('Shoreham/Lincoln Twp planning + comps')}")
        estimated()
    elif is_downtown or in_oz:
        a(f"**Model/reality agreement (effort=max):** the BH-centric location model scores this **high** "
          f"({subs['location_score']:.3f}), consistent with a **prime downtown{'/QOZ' if in_oz else ''} parcel** — model "
          f"and ground truth **agree** here. (Contrast the Shoreham calibration, where the same model *understated* a "
          f"lakefront lot — the divergence is parcel-type-specific, which validates checking it per parcel.) {G}")
        grounded()
        a("")
        a(f"**Redevelopment trajectory:** this parcel sits **inside** the Benton Harbor redevelopment corridor — "
          f"downtown core (~{audit['dist_downtown_km']} km){', a designated QOZ' if in_oz else ''}, within the zone where "
          f"Harbor Shores, the Ox Creek revitalization, and SLBA demolition activity concentrate. It **rides the policy "
          f"tailwinds directly**, the opposite of a peripheral lot. {E('parcel-level zoning/use + comps')}")
        estimated()
    else:
        a(f"**Redevelopment trajectory:** neither lakefront nor downtown-core; upside case is local/secondary. "
          f"{E('local planning + comps')}")
        estimated()
    a("")

    # 5) DISPOSITION PATHS (effort=max) ---------------------------------------
    a("## 5. Disposition Paths  *(effort=max)*")
    a("")
    a("### Demand match — builder_buyers (009)")
    a(f"- builder_buyers lives in the **APG CRM DB**, not LandOS; the report runtime "
      f"{U('CRM_DATABASE_URL / cross-DB connector')} cannot query it directly. {E('cross-DB wiring')}")
    a(f"- Known seeded demand: **DR Horton Indiana** — residential lots in **IN** "
      f"(St. Joseph/Elkhart/Marshall/LaPorte). This parcel is **MI / Berrien** → **geography mismatch, NO match** "
      f"({G}). Product fit is also unconfirmed — zoning/use is {U('BS&A')}, so no residential-product assumption is made.")
    grounded(); estimated()
    a("")
    a("### Path analysis")
    a("| Path | Fit | Buy-box logic | Indicative spread |")
    a("|---|---|---|---|")
    if is_lakefront:
        a(f"| Wholesale / assignment | plausible | assign the tax-deed position to a lake-lot end buyer | {E('comps + assignment demand')} |")
        a(f"| Entitled flip | weak | no entitlement lift identified | {E('zoning + survey')} |")
        a(f"| Hold-for-DALE / recreational | strongest *thesis* | lakefront scarcity; second-home/recreational buyer | {E('lakefront comps')} |")
    else:
        a(f"| **OZ-investor / QOF** | **strong (policy-driven)** | designated QOZ → buyers seeking the §1400Z deferral; a demand pool the non-OZ Shoreham lot lacks | {E('QOF demand + comps')} |")
        a(f"| Wholesale / assignment | plausible | assign to a local downtown infill investor/developer | {E('comps + investor demand')} |")
        a(f"| Entitled / redevelopment flip | conditional | downtown QOZ redevelopment IF use permits | {E('BS&A zoning + survey')} |")
        a(f"| Hold-for-rental / land-bank side-lot | fallback | BH rental/assemblage demand | {E('rent comps')} |")
    a("")
    if in_oz:
        a(f"**Reasoning:** the standout disposition edge is the **QOZ status itself** — it widens the buyer pool to "
          f"Qualified Opportunity Funds chasing the §1400Z deferral, a *policy-manufactured* demand. Every dollar "
          f"spread still hinges on a value the engine lacks (no comps) → all spreads {E('comps — the #1 data gap')}; "
          f"no dollar figure is asserted.")
    else:
        a(f"**Reasoning:** credible exits are a quick **wholesale of the position** or a **hold/retail**; both hinge on "
          f"a value the engine lacks → spreads {E('comps — the #1 data gap')}, no dollar figure asserted.")
    estimated()
    a("")

    # 6) RISK + GATES ----------------------------------------------------------
    a("## 6. Risk & Gates")
    a("")
    a("| Gate | Status | Class | Note |")
    a("|---|---|---|---|")
    part201 = next((s for s in sigs if s["signal_type"] == "environmental_part201"), None)
    if part201:
        pm = part201.get("raw_meta") or {}
        a(f"| environmental_flag | **FLAGGED** ({G}) | **curable (Phase II)** | EGLE Part 201 site "
          f"\"{pm.get('site_name') or '?'}\" (SiteID {pm.get('site_id') or '?'}) spatially resolved to this APN — "
          f"contamination present; budget Phase II + cleanup, **not a hard-kill** |")
        grounded()
    else:
        a(f"| environmental_flag | {U('Phase I ESA')} | — | not assessable from current data; **not assumed clear**. "
          f"Nearest EGLE brownfield ≈ {('%.0f m' % nb) if nb is not None else 'n/a'} ({G}) — context |")
    a(f"| title_uncured | {U('title search')} | typically **curable** | MI tax-deeds convey clear post-judgment; "
      f"quiet-title sometimes needed {E('title commitment')} |")
    a(f"| land_bank_rofr | {U('SLBA/county land-bank check')} | possible **hard-kill** | verify no ROFR before bidding |")
    a(f"| occupied | {U('field/utility check')} | curable if vacant | {('vacant land likely' if is_lakefront else 'downtown structure possible — occupancy unknown')} |")
    estimated(2)
    a("")
    a(f"**No gate is marked clear without data** — unassessed gates are `UNKNOWN`, not passed. The scorer applied "
      f"**0 hard-kill gates** only because the source carries no gate fields — absence of data, not absence of risk.")
    a("")

    # 7) ACQUISITION CALENDAR --------------------------------------------------
    a("## 7. Acquisition Calendar")
    a("")
    if live_auction and catalog_id in AUCTION_BY_CATALOG:
        au = AUCTION_BY_CATALOG[catalog_id]
        a("| Milestone | Date | Provenance |")
        a("|---|---|---|")
        a(f"| Catalog drop | LIVE — id {catalog_id} | {G} (tax-sale.info) |")
        a(f"| **Auction** | **{au['auction']}** | {G} |")
        a(f"| Re-offer sale | {au['reoffer']} | {G} |")
        a(f"| No-reserve final | {au['final']} | {G} |")
        a(f"| MI forfeiture to treasurer | March 1 (statutory) | {G} (MCL 211.78) |")
        a(f"| Judgment / redemption deadline | ~Feb–Mar 31 | {E('this parcel’s judgment docket')} |")
        a(f"| Registration + deposit deadline | a few days pre-auction | {E('auction bidder terms')} |")
        grounded(5); estimated(2)
    else:
        a(f"**N/A — this parcel is NOT in a current tax sale** ({G}: no live tax-foreclosure signal). No auction "
          f"calendar applies. For context, the generic MI tax-foreclosure timeline (if it ever enters): forfeiture to "
          f"treasurer **March 1**, foreclosure judgment + **redemption deadline ~March 31**, county auction in summer "
          f"(this cycle's Berrien sale is 2026-08-06). {G} statutory dates; **not tied to this parcel.**")
        grounded()
        a("")
        a(f"Acquisition here would be **off-market / negotiated** (direct-to-owner), which needs owner-contact data "
          f"({U('BS&A owner + skip-trace — OUT OF SCOPE')}), or a **future tax-sale entry**.")
        estimated()
    a("")

    # 8) BID THESIS (effort=max) ----------------------------------------------
    a("## 8. Bid Thesis  *(effort=max)*")
    a("")
    arv_res = compute_arv(subject_acreage, comps, as_of) if comps else None
    if arv_res and arv_res.get("arv"):
        bidm = compute_max_bid(arv_res["arv"], costs_pct=0.10, prep=0.0)
        sparse = arv_res["n"] < 3
        a(f"**ARV = ${arv_res['arv']:,.0f}** from **{arv_res['n']} manual comp(s)** — {G} (manual_entry, real sales). "
          f"Method: {arv_res['method']}; basis **{arv_res['basis']}**; weighted unit ${arv_res['weighted_unit']:,.2f} "
          f"vs median ${arv_res['median_unit']:,.2f}. Confidence **{arv_res['confidence']}**.")
        grounded()
        if sparse:
            a("")
            a(f"> ⚠️ **Low-confidence ARV — only {arv_res['n']} comp(s) (<3).** Indicative; widen comps before committing.")
        a("")
        a("**Comps cited:**")
        a("| Comp | Sale date | Sale price | Acreage | Dist mi | Unit | Wt | Provenance |")
        a("|---|---|---|---|---|---|---|---|")
        for r in arv_res["rows"]:
            c = r["comp"]
            label = c.get("comp_address") or c.get("comp_apn") or "—"
            a(f"| {label} | {c.get('sale_date') or '—'} | ${float(c['sale_price']):,.0f} | "
              f"{c.get('acreage') if c.get('acreage') is not None else '—'} | "
              f"{c.get('distance_mi') if c.get('distance_mi') is not None else '—'} | "
              f"${r['unit']:,.0f} | {r['weight']:.2f} | {G} ({c.get('provenance') or 'manual_entry'}) |")
            grounded()
        a("")
        a("**APG ceiling math (each link tagged):**")
        a(f"1. *ARV* → **${arv_res['arv']:,.0f}** ({G}, manual comps).")
        a(f"2. *Policy uplift* → already realized in the edge score (×{m:.2f}); ARV uses actual comp prices → no double-count.")
        a(f"3. *Costs* → ~10% = **${bidm['costs']:,.0f}** ({G} APG standard).")
        a(f"4. *Prep-lift* → **$0 deducted**, {U('Phase I + survey')} — range is gross of prep; subtract real prep before bidding.")
        a(f"5. *APG tier* → **{bidm['tier']}**: acquire at **{int(bidm['initial_pct']*100)}–{int(bidm['max_pct']*100)}% of ARV**, must net ≥$10K ({G} business rule).")
        a("")
        a(f"### → INDICATIVE MAX-BID RANGE: **${bidm['bid_low']:,.0f} – ${bidm['bid_high']:,.0f}**")
        a(f"(binding constraint: **{bidm['binding']}**; net at ceiling ≈ ${bidm['net_at_high']:,.0f})")
        a("")
        if live_auction and min_bid is not None:
            within = min_bid <= bidm["bid_high"]
            verdict = ("**BID** ✅ — the minimum bid sits within the disciplined buy-box" if within
                       else "**PASS** 🚫 — the minimum bid EXCEEDS the disciplined max bid")
            tail = (f"headroom of ${bidm['bid_high'] - min_bid:,.0f} to the ceiling."
                    if within else
                    f"acquiring at the ${min_bid:,.0f} floor would break APG discipline (over by ${min_bid - bidm['bid_high']:,.0f}).")
            a(f"**vs. auction floor (min bid ${min_bid:,.0f}, {G}): {verdict}** — {tail}")
            grounded()
        elif not live_auction:
            a(f"**Acquisition mechanism:** parcel is **not in a sale** — this range is the **off-market indicative ceiling** "
              f"(direct-to-owner), which still needs owner-contact data ({U('BS&A + skip-trace — OUT OF SCOPE')}).")
        estimated()  # prep (and off-market mechanism) remain open
    elif live_auction:
        a(f"**Grounded anchors:** minimum bid **${min_bid:,.0f}** ({G}); SEV **${(sev or 0):,.0f}** "
          f"({G}{' — state-exempt, not a value proxy' if (sev == 0) else ''}).")
        a("")
        a(f"**BLOCKED on comps.** No comps for this subject APN → ARV is {U('comps — the #1 data gap')} and **no max "
          f"bid is asserted** (fabricating one is forbidden). Unblock with `python -m scripts.import_comps`: ARV → APG "
          f"ceiling (0.50–0.55×ARV − ~10% − prep, net ≥$10K) → BID/PASS vs the ${min_bid:,.0f} floor. Staged + grounded: "
          f"auction date, min-bid floor, OZ ×{m:.2f}, gates-to-clear.")
        grounded(); estimated()
    else:
        a(f"**N/A — this parcel is not in a sale, so there is no auction bid to thesize.** ({G})")
        a("")
        a(f"Confirmed **monitoring target**: edge is **policy + location** (downtown {'QOZ ' if in_oz else ''}parcel, "
          f"score `{final:.2f}`). A bid number applies only when an acquisition mechanism is priced — a tax-sale min bid "
          f"(+ this engine's ARV→ceiling once comps exist) or an off-market negotiation (needs owner data + comps, "
          f"{U('BS&A + skip-trace — OUT OF SCOPE')}). No max bid asserted. Grounded: designated QOZ (×{m:.2f}) in the "
          f"downtown core → high-conviction watchlist pending a trigger + comps.")
        grounded()
    a("")

    # 9) CONFIDENCE & DATA GAPS ------------------------------------------------
    a("## 9. Confidence & Data Gaps")
    a("")
    a(f"Grounded data points: **{tally['grounded']}** · Estimated/Unknown: **{tally['estimated']}**.")
    a("")
    a("Estimated/unknown inputs and what would ground each:")
    if comps:
        a(f"- **Market value / ARV** → now from {len(comps)} manual comp(s) (manual_entry) — the #1 gap is being filled here.")
    else:
        a("- **Market value / ARV / spreads** → comps ingestion *(the #1 gap; blocks any bid number)*.")
    a("- **Acreage, zoning, owner, value** → BS&A / Berrien Parcels_Future wiring.")
    a("- **Environmental / title / land-bank-ROFR / occupancy gates** → Phase I, title, SLBA inventory, field check.")
    a("- **builder_buyers demand match** → cross-DB connector to the APG CRM (009).")
    if is_lakefront:
        a("- **Lakefront location value** → a Shoreham-aware location anchor + waterfront comps.")
    a("")
    a("- **Out of scope this build:** obituary/deceased-owner signals (need BS&A owner data — TODO, no fake data); outreach automation.")
    a("")
    a("*Structure + the grounded layers now; it sharpens as comps / macro / BS&A data wire in.*")

    summary = {
        "apn": apn, "address": addr, "final": final, "base": base, "mult": m, "cb": cb,
        "grounded": tally["grounded"], "estimated": tally["estimated"], "live_auction": live_auction,
        "comps_n": len(comps),
        "top_grounded": [
            f"benton_harbor_edge score {final:.2f} (base {base:.2f} × OZ {m:.2f} × corr {cb:.2f})",
            (f"In QOZ {oz_hit['name']} (authoritative, ×{m:.2f} applied)" if oz_hit
             else ("Lake Michigan frontage per legal" if is_lakefront else "Not in a QOZ (PIP vs 5 tracts)")),
            (f"Live auction {AUCTION_BY_CATALOG.get(catalog_id, {}).get('auction', '—')}, min bid ${min_bid:,.0f}"
             if live_auction else "Not in a sale — confirmed monitoring target (bid thesis N/A)"),
        ],
    }
    return "\n".join(L), summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="APN or address")
    ap.add_argument("--geography", default="berrien")
    ap.add_argument("--effort", default="max")
    args = ap.parse_args()
    as_of = date.today()

    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            apn, addr, lat, lng, sigs = resolve(cur, args.target)
            weights, from_db = load_weights(cur, "benton_harbor_edge")
            overlays = load_overlays(cur)
            comps = load_comps(cur, apn)

    allowed, vintage_line, refusal = freshness_guard(apn, sigs, as_of)
    if not allowed:
        print(refusal)
        raise SystemExit(2)
    if lat is None or lng is None:
        print(f"Could not resolve a centroid for {args.target!r}. Cannot generate report.")
        raise SystemExit(2)

    parcel = {"apn": apn, "address": addr, "lat": lat, "lng": lng}
    md, summary = build_report(parcel, sigs, overlays, weights, from_db, as_of, vintage_line, comps)

    reports_dir = os.path.expanduser("~/Documents/landos-engine/reports")
    os.makedirs(reports_dir, exist_ok=True)
    safe = (apn or re.sub(r"[^a-z0-9]+", "-", (addr or "parcel").lower())).strip("-")
    out = os.path.join(reports_dir, f"{safe}_deep.md")
    with open(out, "w") as f:
        f.write(md + "\n")

    print("================  DEEP REPORT  ================")
    print(f"Parcel : {summary['address']}  (APN {summary['apn']})")
    print(f"Score  : {summary['final']:.2f}  (base {summary['base']:.2f} × OZ {summary['mult']:.2f} × corr {summary['cb']:.2f})")
    print(f"In sale: {'YES (live bid thesis)' if summary['live_auction'] else 'NO (bid thesis = N/A)'}")
    print(f"Comps  : {summary['comps_n']} for subject APN")
    print("Top 3 GROUNDED findings:")
    for i, fnd in enumerate(summary["top_grounded"], 1):
        print(f"  {i}. {fnd}")
    print(f"Data points: GROUNDED={summary['grounded']}  ESTIMATED/UNKNOWN={summary['estimated']}")
    print(f"Report: {out}")
    print("==============================================")


if __name__ == "__main__":
    main()

"""
nve_land.py — NVE-LAND: the SOVEREIGN land-valuation scorer.

This is the land-portal-dd rubric (references/dd-rubric.md), ported verbatim, with the
Land Portal API DELETED as a data source. The scoring judgment is UNCHANGED; only the
inputs change: adapters (app/adapters/land_dd_adapters.py) feed grounded county/federal
data in, this module applies the exact same 0-100 rubric out.

KEY SUBSTITUTION (Land Portal had an independent AVM; we don't):
  lp_total / lp_per_ac  ->  base_total / base_per_ac  = the COUNTY market (or assessed)
  value. The valuation-gap factor keeps its meaning: does the tax-roll value the parcel
  above or below what comparable land actually sells for?

UNKNOWN-VALUE DOCTRINE (graceful degradation, gate-in-data): a metric that could not be
grounded arrives as None. A deal-KILLER only fires on a GROUNDED value — a None road
frontage or None land-locked flag is "unverified", never fabricated into a killer. Every
such provisional is surfaced in field_provenance + the verify checklist, never hidden.

Pure module: no I/O, no network, no DB. `assess(inputs) -> dict`.
"""
import statistics

# ---- factor maxes (sum = 100), kept identical to the rubric ----
FACTOR_MAX = {"Access": 20, "Wetlands": 15, "FEMA": 15, "Slope": 10, "Valuation": 25, "Size": 15}


def _f(v, default=None):
    """Defensive parse: strip %,$,commas,units -> float. None/''/'null'/non-numeric -> default
    (NOT zero — the rubric forbids assuming zero for missing)."""
    if v is None or v == "" or v == "null":
        return default
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace("%", "").replace("$", "").replace(",", "")
                     .replace(" ft", "").replace(" / acre", "").strip())
    except ValueError:
        return default


def _as_bool(v):
    """land_locked may be bool or 'Yes'/'No'; unknown -> None (not False)."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "yes", "y", "1"):
        return True
    if s in ("false", "no", "n", "0"):
        return False
    return None


# ------------------------------------------------------------------ deal-killers
def deal_killers(m):
    """Rubric Step 1. A killer fires ONLY on a grounded value (None = unverified = no fire)."""
    k = []
    size = m.get("size")
    road = m.get("road_frontage")
    wet = m.get("wetlands")
    fema = m.get("fema")
    base = m.get("base_total")
    ll = m.get("land_locked")           # True / False / None(unknown)

    if ll is True:
        k.append("Land-locked (no legal access)")
    if road is not None and road == 0 and ll is not True:
        k.append("No road frontage — verify access on the ground (extreme caution)")
    if fema is not None and fema >= 75:
        k.append(f"FEMA flood cover {fema:.0f}% (dominantly in flood zone)")
    if wet is not None and wet >= 75:
        k.append(f"Wetlands cover {wet:.0f}% (dominantly wetlands)")
    if size is not None and size <= 0.1:
        k.append("Size <= 0.1 ac (sliver / data-quality)")
    if size is None:
        k.append("Parcel size unavailable")
    if base is None or (base is not None and base <= 0):
        k.append("No base valuation — needs manual valuation")
    return k


# ------------------------------------------------------------------ factor scoring
def score_factors(m):
    """Rubric Step 2. Returns {factor: points}. Unknown inputs -> provisional + est flag
    handled by the caller via `estimated_factors`."""
    size = m.get("size") or 0
    road = m.get("road_frontage")
    ll = m.get("land_locked")
    wet = m.get("wetlands")
    fema = m.get("fema")
    buildable = m.get("buildable")
    base_total = m.get("base_total") or 0
    comp_median = m.get("comp_median")
    zip_pa = m.get("zip_per_ac")
    county_pa = m.get("county_per_ac")

    s, est = {}, set()

    # Access (max 20)
    if road is None:
        # unverified frontage: provisional mid-tier, flagged. Not a killer, not a 20.
        s["Access"] = 10
        est.add("Access")
    elif road >= 200:
        s["Access"] = 20
    elif road >= 100:
        s["Access"] = 15
    elif road >= 50:
        s["Access"] = 10
    elif road >= 1:
        s["Access"] = 5
    elif ll is not True:
        s["Access"] = 2
    else:
        s["Access"] = 0

    # Wetlands (max 15, +3 bonus for big clean parcels, cap 18)
    if wet is None:
        s["Wetlands"] = 12
        est.add("Wetlands")
    else:
        if wet < 5:
            s["Wetlands"] = 15
        elif wet < 15:
            s["Wetlands"] = 12
        elif wet < 30:
            s["Wetlands"] = 8
        elif wet < 50:
            s["Wetlands"] = 4
        elif wet < 75:
            s["Wetlands"] = 1
        else:
            s["Wetlands"] = 0
        if size > 20 and wet < 50:
            s["Wetlands"] = min(s["Wetlands"] + 3, 18)

    # FEMA (max 15)
    if fema is None:
        s["FEMA"] = 12
        est.add("FEMA")
    elif fema < 5:
        s["FEMA"] = 15
    elif fema < 15:
        s["FEMA"] = 12
    elif fema < 30:
        s["FEMA"] = 8
    elif fema < 50:
        s["FEMA"] = 4
    elif fema < 75:
        s["FEMA"] = 1
    else:
        s["FEMA"] = 0

    # Slope / buildability (max 10)
    if buildable is None:
        s["Slope"] = 5
        est.add("Slope")
    elif buildable >= 80:
        s["Slope"] = 10
    elif buildable >= 60:
        s["Slope"] = 7
    elif buildable >= 40:
        s["Slope"] = 4
    elif buildable >= 20:
        s["Slope"] = 2
    else:
        s["Slope"] = 0

    # Valuation gap vs comps (max 25). anchor = comp median -> zip -> county.
    anchor = comp_median or zip_pa or county_pa
    benchmark = anchor * size if (anchor and size) else 0
    gap = base_total / benchmark if benchmark > 0 else None
    if gap is None:
        s["Valuation"] = 0
        est.add("Valuation")
    elif gap >= 1.3:
        s["Valuation"] = 25
    elif gap >= 1.1:
        s["Valuation"] = 20
    elif gap >= 0.9:
        s["Valuation"] = 15
    elif gap >= 0.7:
        s["Valuation"] = 8
    else:
        s["Valuation"] = 3

    # Size sweet spot (max 15)
    if 5 <= size <= 20:
        s["Size"] = 15
    elif 1 <= size <= 40:
        s["Size"] = 12
    elif 0.5 <= size <= 80:
        s["Size"] = 8
    elif 0.1 <= size <= 160:
        s["Size"] = 4
    else:
        s["Size"] = 2

    return s, est, gap


# ------------------------------------------------------------------ expected value
def compute_expected_value(m, comps, size):
    """Outlier-removed weighted blend: 50% clean comp median + 30% base value + 20% ZIP.
    Ported from the land-portal-dd PDF builder; lp_per_ac -> base_per_ac."""
    base_pa = m.get("base_per_ac") or 0
    clean, dropped = [], []
    for c in comps:
        reasons = []
        cs = _f(c.get("size"), 0)
        if size and (cs > 2 * size or cs < 0.5 * size):
            reasons.append("size out of 0.5-2x range")
        if _as_bool(c.get("land_locked")) is True:
            reasons.append("land-locked")
        (dropped if reasons else clean).append(c)

    if len(clean) < 2:
        return {
            "blend_per_ac": base_pa,
            "low": base_pa * size * 0.9, "high": base_pa * size * 1.1,
            "comp_median_clean": None, "comps_used": len(clean),
            "comps_dropped": len(comps) - len(clean),
            "final_comp_ids": [c.get("apn") for c in clean],
            "methodology": "Fewer than 2 comparable sales — base (county) value ±10%",
        }

    per_acres = sorted(_f(c["per_ac"], 0) for c in clean)
    final = clean
    if len(clean) >= 4:
        q1 = statistics.quantiles(per_acres, n=4)[0]
        q3 = statistics.quantiles(per_acres, n=4)[2]
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        final = [c for c in clean if lo <= _f(c["per_ac"], 0) <= hi]
    elif len(clean) == 3:
        med = statistics.median(per_acres)
        extreme = sorted(clean, key=lambda c: -abs(_f(c["per_ac"], 0) - med) / med if med else 0)
        if extreme and med and abs(_f(extreme[0]["per_ac"], 0) - med) / med > 0.25:
            final = [c for c in clean if c is not extreme[0]]

    clean_pa = [_f(c["per_ac"], 0) for c in final]
    comp_median_clean = statistics.median(clean_pa)
    zip_avg = m.get("zip_per_ac") or base_pa

    if zip_avg and zip_avg != base_pa:
        blend = 0.50 * comp_median_clean + 0.30 * base_pa + 0.20 * zip_avg
    else:
        blend = (0.50 * comp_median_clean + 0.30 * base_pa) / 0.80

    spread = 0.05
    return {
        "blend_per_ac": blend,
        "low": blend * size * (1 - spread), "high": blend * size * (1 + spread),
        "comp_median_clean": comp_median_clean,
        "comps_used": len(final), "comps_dropped": len(comps) - len(final),
        "final_comp_ids": [c.get("apn") for c in final],
        "methodology": (f"50% comp median ({len(final)} clean sales) + 30% county base "
                        f"+ 20% ZIP avg, ± {spread*100:.1f}%"),
    }


# ------------------------------------------------------------------ offers
def _double_close(ev_mid, size, ev, primary_high=None):
    margin = 50000 if ev_mid >= 200000 else 20000
    ceiling = ev["low"] - margin
    floor = ceiling * 0.90
    if floor <= 0 or ceiling <= 0:
        return None
    if primary_high is not None and ceiling <= primary_high * 1.05:
        return None
    return {"name": "DOUBLE CLOSE", "offer_low": floor, "offer_high": ceiling,
            "per_ac_mid": ((floor + ceiling) / 2) / size if size else None,
            "rationale": (f"If seller resists cash, offer a double-close at a higher number over a "
                          f"3-4 month timeline. Leaves a ${margin:,.0f} margin even at the bottom of "
                          f"the comp range (${ev['low']:,.0f}).")}


def build_offers(m, size, score, stance, ev, comps):
    """Strategy-aware primary + optional double-close secondary. Ported from build_opinion."""
    road = m.get("road_frontage") or 0
    wet = m.get("wetlands") or 0
    fema = m.get("fema") or 0
    buildable = m.get("buildable") or 0
    ll = m.get("land_locked")
    ev_mid = (ev["low"] + ev["high"]) / 2

    doms = [_f(c.get("dom")) for c in comps if _f(c.get("dom"))]
    avg_dom = sum(doms) / len(doms) if doms else None
    is_fast = len(comps) >= 5 and (avg_dom is None or avg_dom < 150)

    subdivide_ok = (road >= 1000 and size >= 5 and buildable >= 50
                    and ll is not True and wet < 30 and fema < 30)

    if stance == "PASS":
        return None, None
    if subdivide_ok and stance == "PURSUE":
        low, high = ev_mid * 0.55, ev_mid * 0.70
        primary = {"name": "SUBDIVIDE", "offer_low": low, "offer_high": high,
                   "per_ac_mid": ((low + high) / 2) / size if size else None,
                   "rationale": (f"Frontage {road:.0f} ft and {buildable:.0f}% buildable on {size:.1f} ac "
                                 f"favor subdivision; pay 55-70% of EV (${ev_mid:,.0f}).")}
        return primary, _double_close(ev_mid, size, ev, high)

    if stance == "PURSUE":
        lo_pct, hi_pct = (0.45, 0.55) if is_fast else (0.40, 0.50)
        note = "active market signals justify the upper end" if is_fast else "standard flip discount"
    else:  # PURSUE_CAUTION
        lo_pct, hi_pct = (0.30, 0.40)
        note = "the concerns warrant a wider discount than a standard flip"
    low, high = ev_mid * lo_pct, ev_mid * hi_pct
    primary = {"name": "FLIP", "offer_low": low, "offer_high": high,
               "per_ac_mid": ((low + high) / 2) / size if size else None,
               "rationale": (f"Cash flip — {note}. Offer {int(lo_pct*100)}-{int(hi_pct*100)}% of EV "
                             f"(${ev_mid:,.0f}).")}
    return primary, _double_close(ev_mid, size, ev, high)


# ------------------------------------------------------------------ flags
def derive_flags(m, gap, ev):
    red, green = [], []
    road, wet, fema = m.get("road_frontage"), m.get("wetlands"), m.get("fema")
    buildable, flop15 = m.get("buildable"), m.get("flop15")
    base_pa, comp_med = m.get("base_per_ac"), (ev or {}).get("comp_median_clean")

    if wet is not None and wet >= 30:
        red.append(f"High wetlands cover ({wet:.0f}%)")
    if fema is not None and fema >= 30:
        red.append(f"High FEMA flood cover ({fema:.0f}%)")
    if road is not None and road < 50:
        red.append(f"Low road frontage ({road:.0f} ft)")
    if flop15 is not None and flop15 > 25:
        red.append(f"Extreme slope on {flop15:.0f}% of the parcel")
    if comp_med and base_pa and base_pa < comp_med * 0.7:
        red.append("County base value well below comp median (hidden constraints?)")

    if road is not None and road > 100:
        green.append(f"Healthy road frontage ({road:.0f} ft)")
    if wet is not None and fema is not None and wet < 15 and fema < 15:
        green.append("Clean of wetlands and flood (<15% each)")
    if buildable is not None and buildable >= 80:
        green.append(f"High buildable share ({buildable:.0f}%)")
    if comp_med and base_pa and base_pa > comp_med:
        green.append("County base value above comp median")
    size = m.get("size")
    if size and 5 <= size <= 20:
        green.append(f"Size in the 5-20 ac sweet spot ({size:.1f} ac)")
    return red, green


# ------------------------------------------------------------------ orchestrator
def assess(inputs):
    """Full NVE-LAND assessment. `inputs` = {metrics{}, comps[], parcel{}}.
    Returns the internal result dict (serialized to the CRM contract elsewhere)."""
    m = dict(inputs.get("metrics") or {})
    comps = list(inputs.get("comps") or [])

    # normalize
    for key in ("size", "road_frontage", "wetlands", "fema", "buildable", "flop15",
                "base_per_ac", "base_total", "zip_per_ac", "county_per_ac", "comp_median"):
        m[key] = _f(m.get(key))
    m["land_locked"] = _as_bool(m.get("land_locked"))
    if m.get("comp_median") is None:
        pa = [_f(c.get("per_ac")) for c in comps if _f(c.get("per_ac"))]
        m["comp_median"] = statistics.median(pa) if pa else None

    killers = deal_killers(m)
    score, estimated, gap = score_factors(m)
    total = sum(score.values())

    # stance
    if killers:
        stance = "PASS"
    elif total >= 75:
        stance = "PURSUE"
    elif total >= 50:
        stance = "PURSUE_CAUTION"
    else:
        stance = "PASS"
    # tier-downgrade: 2+ factors in the lowest tier (<=2) drops PURSUE -> PURSUE_CAUTION
    if not killers and sum(1 for v in score.values() if v <= 2) >= 2 and stance == "PURSUE":
        stance = "PURSUE_CAUTION"

    ev = compute_expected_value(m, comps, m.get("size") or 0)
    primary, secondary = build_offers(m, m.get("size") or 0, score, stance, ev, comps)
    red, green = derive_flags(m, gap, ev)

    return {
        "land_score": total,
        "score_breakdown": score,
        "score_max": FACTOR_MAX,
        "estimated_factors": sorted(estimated),
        "stance": stance,
        "deal_killers": killers,
        "valuation_gap_ratio": gap,
        "expected_value": ev,
        "primary_strategy": primary,
        "secondary_strategy": secondary,
        "red_flags": red,
        "green_flags": green,
        "usable_acres": (m["size"] * (1 - (m.get("wetlands") or 0) / 100 - (m.get("fema") or 0) / 100)
                         if m.get("size") else None),
        "metrics": m,
    }

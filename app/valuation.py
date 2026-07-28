"""
valuation.py — VALUATION LAB: the auditable land value + house MAO behind
GET /v1/valuation/{lead_id}?mode=land|house.

FIREWALL: this is NEURUH IP. The comp selection, the adjustment weights, the acreage
curve, the MAO formula live here; the CRM consumes the REST result and DISPLAYS it.

HONESTY LAW (the only failure mode that matters): a wrong number is worse than no number
— a VA quotes it to a seller and we are bound by it. So:
  * every value is DATA-GROUNDED in real comparable SALES (landos.comps). No comps -> the
    value is null and the response says why. We NEVER interpolate a value out of an
    assessed number and call it a valuation.
  * every adjustment is ITEMIZED with the input it came from and the factor it applied, so
    the whole number is auditable back to its parts.
  * an overlay we cannot measure (no road layer, federal REST down) is reported UNVERIFIED
    with factor 1.0 — it neither silently helps nor hurts the number.

LAND value model (JOB 2):
    base $/acre (median of comps)  ->  × acreage-curve factor (price/acre is NOT linear)
    applied to BUILDABLE acres (wetlands + slope carve out unbuildable acres, which are a
    different asset, valued at a residual)  ->  × flood(SFHA) discount  × water premium.
HOUSE MAO (JOB 3):
    MAO = (ARV × pct) − repairs − fee, with pct / repairs / fee read from TENANT CONFIG
    (public.tenant_settings; default the 70% rule) and CARRIED in the response.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

METHOD_VERSION = "valuation-lab-v1"
TENANT = "1840ac90-54bd-4b3d-8baf-227c8d84ab5a"

# The generic wholesale default when a tenant carries no config (the "70% rule"). A
# configured tenant (APG offer_pct=55, commission_pct=6) overrides every field.
DEFAULT_MAO = {"pct": 0.70, "fee_pct": 0.0, "fee_flat": 0.0, "repairs": 0.0}


def _f(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---- acreage curve (JOB 2): $/acre is not linear — 40 ac is not 4× ten ------------
# A documented, conservative taper: parcels up to REF acres price at full comp $/acre;
# beyond that, $/acre decays as (REF/acres)^K (bulk discount). Clamped so it never adds a
# premium and never collapses below FLOOR. A HEURISTIC, itemized so the operator can tune
# it — not a market truth. REF/K/FLOOR are the knobs.
CURVE_REF_AC = 5.0
CURVE_K = 0.18
CURVE_FLOOR = 0.45


def acreage_curve_factor(acres: Optional[float]) -> float:
    a = _f(acres)
    if not a or a <= CURVE_REF_AC:
        return 1.0
    return round(max(CURVE_FLOOR, (CURVE_REF_AC / a) ** CURVE_K), 4)


def _adj(name: str, input_desc: str, factor: Optional[float], rationale: str,
         *, verified: bool = True) -> Dict[str, Any]:
    """One itemized adjustment: what it is, the INPUT it read, the factor it applied."""
    return {"name": name, "input": input_desc, "factor": factor,
            "verified": verified, "rationale": rationale}


def land_value(base_ppa: Optional[float], gross_acres: Optional[float], *,
               buildable_fraction: Optional[float] = None, buildable_input: Optional[str] = None,
               in_sfha: Optional[bool] = None, dist_to_water_m: Optional[float] = None,
               road_frontage_ft: Optional[float] = None) -> Dict[str, Any]:
    """PURE land value from comp $/acre + measured overlays. Returns value, range,
    confidence and the itemized adjustment ledger. base_ppa None (no comps) -> null value
    but the ledger still returns (auditable absence)."""
    adjustments: List[Dict[str, Any]] = []
    ppa = _f(base_ppa)
    acres = _f(gross_acres)

    # 1) acreage curve — always itemized (factor 1.0 for small parcels)
    curve = acreage_curve_factor(acres)
    adjustments.append(_adj(
        "acreage_curve", f"gross_acres={acres}", curve,
        f"$/acre taper beyond {CURVE_REF_AC:.0f} ac (bulk discount); factor {curve}"))

    # 2) buildable acres — wetlands + slope carve out unbuildable acres (a different asset,
    #    valued at a residual, not zero). Unverified -> assume fully buildable, factor 1.0.
    if buildable_fraction is None:
        adjustments.append(_adj(
            "buildable_acres", buildable_input or "wetlands/slope unverified", 1.0,
            "buildable fraction unverified (federal NWI/3DEP not resolved) — treated as "
            "fully buildable; value may be OVERSTATED for a wet/steep parcel", verified=False))
        bf = 1.0
    else:
        bf = max(0.0, min(1.0, float(buildable_fraction)))
        # unbuildable acres retain a residual (recreation/agput) ~ 25% of buildable $/acre
        eff = bf + (1.0 - bf) * 0.25
        adjustments.append(_adj(
            "buildable_acres", buildable_input or f"buildable_fraction={bf}", round(eff, 4),
            f"{bf*100:.0f}% buildable; unbuildable acres valued at 25% residual (a wetland "
            f"acre is a different asset, not a cheap acre)"))

    # 3) flood (SFHA) discount
    if in_sfha is None:
        adjustments.append(_adj("flood_sfha", "parcel_flood unresolved", 1.0,
                                "SFHA overlay unverified", verified=False))
        flood_f = 1.0
    elif in_sfha:
        flood_f = 0.80
        adjustments.append(_adj("flood_sfha", "in_sfha=true", flood_f,
                                "parcel intersects a FEMA Special Flood Hazard Area — 20% discount"))
    else:
        flood_f = 1.0
        adjustments.append(_adj("flood_sfha", "in_sfha=false", 1.0, "outside SFHA — no discount"))

    # 4) water proximity premium (proven in the assemblage recipe: dist_to_lake_m)
    dw = _f(dist_to_water_m)
    if dw is None:
        water_f = 1.0
        adjustments.append(_adj("water_proximity", "dist_to_water_m unknown", 1.0,
                                "water distance unverified", verified=False))
    elif dw <= 150:
        water_f = 1.25
        adjustments.append(_adj("water_proximity", f"dist_to_water_m={dw:.0f}", water_f,
                                "within 150m of water — lakefront/near-water premium"))
    elif dw <= 800:
        water_f = 1.08
        adjustments.append(_adj("water_proximity", f"dist_to_water_m={dw:.0f}", water_f,
                                "within 800m of water — modest proximity premium"))
    else:
        water_f = 1.0
        adjustments.append(_adj("water_proximity", f"dist_to_water_m={dw:.0f}", 1.0,
                                "no water-proximity premium"))

    # 5) road frontage — NO road layer staged (named gap); always unverified in v1
    adjustments.append(_adj("road_frontage",
                            f"road_frontage_ft={road_frontage_ft}" if road_frontage_ft else "no road layer",
                            1.0, "road frontage unverified — no road-centerline layer staged; "
                            "a land-locked parcel would be OVERVALUED here", verified=False))

    if ppa is None or not acres:
        return {"value": None, "range": None, "confidence": "none",
                "base_ppa": ppa, "gross_acres": acres,
                "adjustments": adjustments,
                "reason": "no comparable sales for the subject — value not grounded"}

    factor = curve * (bf + (1.0 - bf) * 0.25) * flood_f * water_f
    value = round(ppa * acres * factor, 2)
    # range from the adjustment uncertainty: ±15% base, widened when overlays are unverified
    unverified = sum(1 for a in adjustments if not a["verified"])
    spread = 0.15 + 0.05 * unverified
    return {
        "value": value,
        "range": [round(value * (1 - spread), 2), round(value * (1 + spread), 2)],
        "confidence": "low" if unverified >= 3 else ("medium" if unverified >= 1 else "high"),
        "base_ppa": round(ppa, 2), "gross_acres": acres, "net_factor": round(factor, 4),
        "adjustments": adjustments,
    }


# ---- house MAO (JOB 3) --------------------------------------------------------------
def compute_mao(arv: Optional[float], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """MAO = (ARV × pct) − repairs − fee. pct/repairs/fee from tenant cfg, CARRIED back so
    the CRM shows where the number came from. ARV None (no residential comps) -> null MAO."""
    pct = float(cfg.get("pct", DEFAULT_MAO["pct"]))
    repairs = float(cfg.get("repairs", 0.0) or 0.0)
    a = _f(arv)
    fee_pct = float(cfg.get("fee_pct", 0.0) or 0.0)
    fee_flat = float(cfg.get("fee_flat", 0.0) or 0.0)
    if a is None:
        return {"value": None, "arv": None, "pct": pct, "repairs": repairs,
                "fee": None, "config_source": cfg.get("source"),
                "reason": "no residential ARV comps for the subject county — MAO not grounded"}
    fee = round(fee_pct * a + fee_flat, 2)
    mao = round(a * pct - repairs - fee, 2)
    spread = 0.10
    return {
        "value": mao, "range": [round(mao * (1 - spread), 2), round(mao * (1 + spread), 2)],
        "arv": round(a, 2), "pct": pct, "repairs": repairs, "fee": fee,
        "formula": "MAO = (ARV × pct) − repairs − fee",
        "config_source": cfg.get("source"),
    }


def tenant_mao_config(cur, tenant_id: str = TENANT) -> Dict[str, Any]:
    """Read the tenant's underwriting numbers from public.tenant_settings; fall back to the
    generic 70% rule. offer_pct -> the ARV multiplier; commission_pct -> the fee %; a
    prefs.repairs_default (flat $) if the tenant set one."""
    cur.execute("""select offer_pct, commission_pct, close_days, prefs
                     from public.tenant_settings where tenant_id = %s""", (tenant_id,))
    row = cur.fetchone()
    if not row:
        return {**DEFAULT_MAO, "source": "default 70% rule (no tenant_settings row)"}
    prefs = row["prefs"] if isinstance(row["prefs"], dict) else {}
    pct = (float(row["offer_pct"]) / 100.0) if row["offer_pct"] is not None else DEFAULT_MAO["pct"]
    fee_pct = (float(row["commission_pct"]) / 100.0) if row["commission_pct"] is not None else 0.0
    return {
        "pct": pct, "fee_pct": fee_pct,
        "fee_flat": float(prefs.get("fee_flat", 0.0) or 0.0),
        "repairs": float(prefs.get("repairs_default", 0.0) or 0.0),
        "close_days": row["close_days"],
        "source": f"tenant_settings(offer_pct={row['offer_pct']}, commission_pct={row['commission_pct']})",
    }


# ---- lead resolution ----------------------------------------------------------------
def resolve_lead(cur, lead_id: str) -> Optional[Dict[str, Any]]:
    """acquisition_leads lead_id OR source_lead_id -> the subject facts a valuation needs."""
    cur.execute(
        """select a.lead_id, a.source_lead_id, a.county_fips, a.parcel_apn,
                  a.situs_lat, a.situs_lng, a.assessed_total,
                  a.score_signals, a.parcel_meta, po.acres as roll_acres,
                  po.land_assessed, po.total_assessed
             from public.acquisition_leads a
             left join landos.parcel_owner po
               on po.county_fips = a.county_fips and po.parcel_apn = a.parcel_apn
            where a.tenant_id = %(t)s
              and (a.lead_id = %(lid)s::uuid or a.source_lead_id = %(lid)s::uuid)
            limit 1""",
        {"t": TENANT, "lid": str(lead_id)})
    row = cur.fetchone()
    return dict(row) if row else None


def _lead_acres(lead: Dict[str, Any]) -> Optional[float]:
    pm = lead.get("parcel_meta") if isinstance(lead.get("parcel_meta"), dict) else {}
    return _f(pm.get("acreage")) or _f(lead.get("roll_acres"))


def _dist_to_water(lead: Dict[str, Any]) -> Optional[float]:
    ss = lead.get("score_signals") if isinstance(lead.get("score_signals"), dict) else {}
    return _f(ss.get("dist_to_lake_m"))


def _in_sfha(cur, county_fips, parcel_apn) -> Optional[bool]:
    """Stored SFHA overlay (landos.parcel_flood, built by cross_flood_sfha). None when the
    county has no flood layer at all (can't say either way — honest unverified)."""
    if not county_fips or not parcel_apn:
        return None
    cur.execute("select 1 from landos.parcel_flood where county_fips=%s limit 1", (county_fips,))
    if cur.fetchone() is None:
        return None                      # no flood layer for this county -> unverified
    cur.execute("""select in_sfha from landos.parcel_flood
                    where county_fips=%s and parcel_apn=%s limit 1""", (county_fips, parcel_apn))
    r = cur.fetchone()
    return bool(r["in_sfha"]) if r else False   # layer exists, parcel absent -> not in SFHA


def _geom_parcel(cur, county_fips, parcel_apn) -> Dict[str, Any]:
    """Acreage + interior sample rings from the LOADED parcel polygon (landos.parcel_geom):
    $0, DB-grounded, geodesic ST_Area — works for every loaded county with no per-county
    ArcGIS adapter. Acreage this way is a MEASURED value, not an assessor estimate."""
    if not county_fips or not parcel_apn:
        return {"acres": None, "rings": None}
    cur.execute("""select ST_Area(geom::geography)/4046.8564224 as acres, ST_AsGeoJSON(geom) as gj
                     from landos.parcel_geom where county_fips=%s and parcel_apn=%s limit 1""",
                (county_fips, parcel_apn))
    r = cur.fetchone()
    if not r:
        return {"acres": None, "rings": None}
    import json as _json
    gj = _json.loads(r["gj"]) if r.get("gj") else {}
    coords = gj.get("coordinates") or []
    rings = (coords[0] if coords else None) if gj.get("type") == "MultiPolygon" else (coords or None)
    return {"acres": _f(r["acres"]), "rings": rings}


def _buildable_from_rings(rings) -> tuple:
    """Wetlands(NWI)+slope(3DEP) buildable fraction from a parcel's interior points. Any
    failure -> (None, reason). Never fabricates."""
    try:
        from . import land_dd_adapters as lda
        pts = lda.sample_points(rings, max_pts=6)
        if not pts:
            return None, "no interior sample points"
        wf = _f(lda.wetlands_cover(pts).get("value"))
        sb = _f(lda.slope_buildable(pts).get("buildable"))
        if wf is None and sb is None:
            return None, "federal NWI/3DEP returned no coverage"
        dry = (1.0 - wf) if wf is not None else 1.0
        build = sb if sb is not None else 1.0
        return round(max(0.0, min(1.0, dry * build)), 4), f"NWI wetlands={wf}, 3DEP buildable={sb}"
    except Exception as e:
        return None, f"federal pull failed: {e!r}"


def _federal_parcel(county_fips, parcel_apn, lat, lng) -> Dict[str, Any]:
    """Optional live county-ArcGIS + NWI(wetlands) + 3DEP(slope) pull. Returns
    {acres, buildable_fraction, buildable_input}. Any failure degrades to Nones + a reason —
    never fabricates, never hangs the whole valuation."""
    out = {"acres": None, "buildable_fraction": None, "buildable_input": None}
    try:
        from . import land_dd_adapters as lda
        parcel = lda.parcel_lookup(str(county_fips), apn=parcel_apn, lat=lat, lng=lng)
        if parcel.get("_error") or not parcel.get("rings"):
            out["buildable_input"] = f"parcel geometry unresolved ({parcel.get('_error')})"
            return out
        out["acres"] = _f(parcel.get("acres"))
        pts = lda.sample_points(parcel["rings"], max_pts=6)
        wet = lda.wetlands_cover(pts)          # {value: wetland fraction 0-1 or None}
        slope = lda.slope_buildable(pts)       # {buildable: fraction 0-1 or None}
        wf = _f(wet.get("value"))
        sb = _f(slope.get("buildable"))
        if wf is None and sb is None:
            out["buildable_input"] = "federal NWI/3DEP returned no coverage"
            return out
        dry = (1.0 - wf) if wf is not None else 1.0
        build = sb if sb is not None else 1.0
        out["buildable_fraction"] = round(max(0.0, min(1.0, dry * build)), 4)
        out["buildable_input"] = f"NWI wetlands={wf}, 3DEP buildable={sb}"
        return out
    except Exception as e:                     # network/parse — honest degrade
        out["buildable_input"] = f"federal pull failed: {e!r}"
        return out


COMP_OUT = lambda c: {"comp_apn": c.get("apn"), "sale_date": c.get("sold_date"),
                      "sale_price": c.get("price"), "acreage": c.get("size"),
                      "price_per_acre": c.get("per_ac"), "source": c.get("source")}


def value_land_for_lead(cur, lead: Dict[str, Any], federal: bool = False) -> Dict[str, Any]:
    """The land contract for a resolved lead. Comps ALWAYS returned (even when too few to
    ground a value). Overlays: flood from the stored layer; wetlands/slope only when
    federal=True (else unverified). Honest-null value when comps don't ground it."""
    from . import land_dd_adapters as lda
    # Resolve acreage FIRST (parcel_meta/roll, else the measured parcel_geom area) so the comp
    # pull can BAND to the subject's size — a median $/acre over mixed lot sizes is dominated
    # by tiny high-$/acre slivers and would badly OVERVALUE a normal lot ($/acre is not linear).
    acres = _lead_acres(lead)
    acres_source = ("parcel_meta/parcel_owner" if acres else None)
    geom = _geom_parcel(cur, lead.get("county_fips"), lead.get("parcel_apn"))
    if acres is None and geom["acres"]:
        acres, acres_source = round(geom["acres"], 4), "parcel_geom ST_Area (geodesic, measured)"

    comps = lda.comps_for(cur, lead.get("parcel_apn"), acres)   # banded to subject acreage
    in_sfha = _in_sfha(cur, lead.get("county_fips"), lead.get("parcel_apn"))
    dw = _dist_to_water(lead)

    bf, b_input = None, None
    if federal:
        if geom["rings"]:
            bf, b_input = _buildable_from_rings(geom["rings"])
        else:
            fed = _federal_parcel(lead.get("county_fips"), lead.get("parcel_apn"),
                                  lead.get("situs_lat"), lead.get("situs_lng"))
            bf, b_input = fed["buildable_fraction"], fed["buildable_input"]

    v = land_value(comps.get("comp_median"), acres, buildable_fraction=bf, buildable_input=b_input,
                   in_sfha=in_sfha, dist_to_water_m=dw)

    inputs_used = [
        {"field": "acquisition_leads.parcel_apn", "value": lead.get("parcel_apn")},
        {"field": "acquisition_leads.county_fips", "value": lead.get("county_fips")},
        {"field": "acreage", "value": acres, "source": acres_source},
        {"field": "landos.comps", "value": f"{comps.get('n')} comps ({comps.get('method')})"},
        {"field": "landos.parcel_flood.in_sfha", "value": in_sfha},
        {"field": "score_signals.dist_to_lake_m", "value": dw},
    ]
    return {
        "mode": "land", "method_version": METHOD_VERSION,
        "value": v["value"], "range": v["range"], "confidence": v["confidence"],
        "comps": [COMP_OUT(c) for c in comps.get("comps", [])],
        "comps_grounded": comps.get("grounded"),
        "adjustments": v["adjustments"],
        "inputs_used": inputs_used,
        "reason": v.get("reason"),
        "base_ppa": v.get("base_ppa"), "net_factor": v.get("net_factor"),
    }


RESIDENTIAL_COMPS_SQL = """
    select comp_apn, comp_address, sale_date, sale_price, acreage, property_type, source
      from landos.comps
     where tenant_id = %s and subject_apn = %s
       and property_type is not null
       and property_type ~* 'resid|single|sfr|dwelling|house'
     order by sale_date desc nulls last
"""


def value_house_for_lead(cur, lead: Dict[str, Any]) -> Dict[str, Any]:
    """The house MAO contract. ARV from RESIDENTIAL comps; none staged -> honest-null MAO
    (never an assessed-value interpolation). Tenant config is carried regardless."""
    cfg = tenant_mao_config(cur, TENANT)
    cur.execute(RESIDENTIAL_COMPS_SQL, (TENANT, lead.get("parcel_apn")))
    rows = cur.fetchall()
    prices = [float(r["sale_price"]) for r in rows if r.get("sale_price") is not None]
    arv = round(statistics.median(prices), 2) if len(prices) >= 2 else None
    mao = compute_mao(arv, cfg)
    return {
        "mode": "house", "method_version": METHOD_VERSION,
        "value": mao["value"], "range": mao.get("range"),
        "confidence": "high" if arv is not None else "none",
        "comps": [{"comp_apn": r["comp_apn"], "sale_date": str(r["sale_date"] or ""),
                   "sale_price": float(r["sale_price"]) if r["sale_price"] is not None else None,
                   "property_type": r["property_type"]} for r in rows],
        "comps_grounded": arv is not None,
        "adjustments": [
            _adj("arv", f"median of {len(prices)} residential comps", None,
                 "after-repair value from residential sales" if arv else "no residential comps"),
            _adj("offer_pct", cfg["source"], cfg["pct"], "tenant ARV multiplier"),
            _adj("repairs", "tenant_settings.prefs.repairs_default", None,
                 f"${cfg.get('repairs', 0):,.0f} repair allowance"),
            _adj("fee", f"fee_pct={cfg.get('fee_pct')}", None, "tenant assignment fee"),
        ],
        "inputs_used": [
            {"field": "acquisition_leads.parcel_apn", "value": lead.get("parcel_apn")},
            {"field": "landos.comps(residential)", "value": f"{len(rows)} rows"},
            {"field": "tenant_settings", "value": cfg["source"]},
        ],
        "mao": mao, "reason": mao.get("reason"),
    }


def value_for_lead(cur, lead_id: str, mode: str = "land", federal: bool = False) -> Optional[Dict[str, Any]]:
    """Top orchestrator: resolve the lead, then land or house. None -> lead not found (404)."""
    lead = resolve_lead(cur, lead_id)
    if lead is None:
        return None
    out = (value_house_for_lead(cur, lead) if mode == "house"
           else value_land_for_lead(cur, lead, federal=federal))
    out["lead_id"] = str(lead["lead_id"])
    out["county_fips"] = lead.get("county_fips")
    out["parcel_apn"] = lead.get("parcel_apn")
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    return out

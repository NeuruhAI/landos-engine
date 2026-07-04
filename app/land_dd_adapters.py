"""
land_dd_adapters.py — NVE-LAND data adapters (the Land Portal replacement).

Tiered, $0, graceful-degradation feeds for the /land-dd endpoint. Every adapter returns a
value plus a provenance stub {source, confidence, grounded}; the estimated-vs-grounded flag
travels with each field (scoring doctrine). No adapter ever fabricates a value — a feed that
can't ground a field returns None, and the scorer treats None as unverified (never a killer).

Feeds:
  parcel / acreage / owner / legal / base value  <- county ArcGIS REST (verified: Berrien 26021;
                                                     Allegan 26005 / Cass 26027 best-effort)
  flood %   <- FEMA NFHL  (hazards.fema.gov .../NFHL/MapServer/28, SFHA_TF point sample)
  wetlands% <- USFWS NWI  (fwspublicservices.wim.usgs.gov .../Wetlands/MapServer/0, point sample)
  slope/buildable <- USGS 3DEP EPQS (epqs.nationalmap.gov, elevation grid -> pairwise slope)
  comps $/acre    <- landos.comps (engine store) via SQL median price/acre

All HTTP is stdlib urllib (engine convention — no requests/httpx). No shapely — polygon %-cover
is a pure-python ray-cast point sample of the parcel interior.
"""
import json
import math
import statistics
import urllib.parse
import urllib.request

UA = {"User-Agent": "landos-engine/1.0 (nve-land)"}
FEMA_NFHL = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
NWI_WETLANDS = "https://fwspublicservices.wim.usgs.gov/wetlandsmapservice/rest/services/Wetlands/MapServer/0/query"
EPQS = "https://epqs.nationalmap.gov/v1/json"

# --- per-county parcel config. Berrien VERIFIED live 2026-07-04; others best-effort ---
COUNTY_PARCEL = {
    "26021": {  # Berrien — VERIFIED
        "url": "https://gis.berriencountymi.gov/server/rest/services/Hosted/Parcel_2025/FeatureServer/0/query",
        "apn_field": "pin", "apn_prefix": "11-",
        "owner": "ownername1", "acres": ["acres", "totalac"], "class_field": "currclass",
        "sev": ["sev24", "sev23", "sev22"], "legal": "taxdesc",
        "situs": ["propadd", "propcity", "propzip"], "verified": True,
    },
    "26005": {  # Allegan — best-effort (classic MapServer; rejects resultRecordCount)
        "url": "https://gis.allegancounty.org/server/rest/services/Parcel_Viewer_Map_v1_MIL1/MapServer/2/query",
        "apn_field": "MAPPING_ID", "apn_prefix": "",
        "owner": "ownername1", "acres": ["acres", "Acres", "totalacres"], "class_field": "propclass",
        "sev": ["sev", "SEV"], "legal": "legaldesc", "situs": ["propaddr"], "verified": False,
    },
    "26027": {  # Cass — best-effort
        "url": "https://services3.arcgis.com/2Pxs2zBdOuEfbewK/arcgis/rest/services/county_parcels/FeatureServer/0/query",
        "apn_field": "Parcel_Num", "apn_prefix": "",
        "owner": "Owner1", "acres": ["Acres", "acres", "GIS_Acres"], "class_field": "Class",
        "sev": ["SEV", "sev"], "legal": "Legal", "situs": ["Physical", "City", "Zip"], "verified": False,
    },
}
SEV_TO_MARKET = 2.0  # MI SEV ~= 50% of true cash value
ASSET_BY_HUNDREDS = {1: "agricultural", 2: "commercial", 3: "industrial",
                     4: "residential", 5: "timber", 6: "developmental"}


# ------------------------------------------------------------------ http
def _get(url, params, timeout=12):
    """GET + parse JSON. Returns dict; on any failure returns {'_error': repr}."""
    try:
        req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 — degrade, never raise into the request
        return {"_error": repr(e)}


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ geometry (no shapely)
def _pip(lng, lat, ring):
    """Ray-cast point-in-polygon. ring = [[lng,lat],...]."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lng < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def _centroid(rings):
    if not rings:
        return None
    ring = rings[0]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return (sum(ys) / len(ys), sum(xs) / len(xs))  # (lat, lng)


def sample_points(rings, max_pts=6):
    """Interior sample points (lat,lng) via a grid clipped to the outer ring by ray-cast PIP.
    Falls back to centroid if the polygon is degenerate/tiny."""
    cen = _centroid(rings)
    if not rings:
        return [cen] if cen else []
    ring = rings[0]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    fr = [0.5, 0.25, 0.75, 0.375, 0.625, 0.15, 0.85]
    pts = []
    for fy in fr:
        for fx in fr:
            lng = minx + fx * (maxx - minx)
            lat = miny + fy * (maxy - miny)
            if _pip(lng, lat, ring):
                pts.append((lat, lng))
            if len(pts) >= max_pts:
                break
        if len(pts) >= max_pts:
            break
    if not pts and cen:
        pts = [cen]
    return pts


# ------------------------------------------------------------------ parcel adapter
def _mi_value_and_class(attrs, cfg):
    """MI: base_total = latest nonzero SEV * 2; class -> asset_class + improved flag."""
    sev = None
    sev_field = None
    for fld in cfg["sev"]:
        v = _num(attrs.get(fld))
        if v and v > 0:
            sev, sev_field = v, fld
            break
    base_total = sev * SEV_TO_MARKET if sev else None
    cls = attrs.get(cfg["class_field"])
    cls_i = None
    try:
        cls_i = int(float(cls))
    except (TypeError, ValueError):
        pass
    asset_class = improved = None
    if cls_i:
        asset_class = ASSET_BY_HUNDREDS.get(cls_i // 100)
        improved = (cls_i % 10) == 1  # MI: xx1 = improved, xx2 = vacant
    return base_total, sev, sev_field, cls_i, asset_class, improved


def _parcel_from_feature(feat, cfg, county_fips):
    a = feat.get("attributes", {}) or {}
    rings = (feat.get("geometry") or {}).get("rings") or []
    acres = None
    acres_field = None
    for fld in cfg["acres"]:
        v = _num(a.get(fld))
        if v and v > 0:
            acres, acres_field = v, fld
            break
    base_total, sev, sev_field, cls_i, asset_class, improved = _mi_value_and_class(a, cfg)
    situs = " ".join(str(a.get(f, "")).strip() for f in cfg["situs"] if a.get(f)).strip() or None
    prov = {
        "apn": {"value": a.get(cfg["apn_field"]), "source": "county_arcgis", "grounded": True},
        "acreage": {"value": acres, "source": f"county_arcgis:{acres_field}" if acres else None,
                    "grounded": acres is not None, "confidence": 0.95 if acres else 0.0},
        "base_value": {"value": base_total,
                       "source": f"county_arcgis:{sev_field}x{SEV_TO_MARKET:g}(TCV)" if sev else None,
                       "grounded": base_total is not None, "confidence": 0.8 if base_total else 0.0},
        "owner": {"value": a.get(cfg["owner"]), "source": "county_arcgis", "grounded": True},
        "class_code": {"value": cls_i, "source": "county_arcgis:" + cfg["class_field"],
                       "grounded": cls_i is not None},
    }
    return {
        "apn": a.get(cfg["apn_field"]),
        "owner": a.get(cfg["owner"]),
        "acres": acres,
        "class_code": cls_i,
        "asset_class": asset_class,
        "improved": improved,
        "sev": sev,
        "base_total": base_total,
        "base_per_ac": (base_total / acres) if (base_total and acres) else None,
        "legal": a.get(cfg["legal"]),
        "situs": situs,
        "rings": rings,
        "centroid": _centroid(rings),
        "provenance": prov,
        "attrs_source": "county_arcgis",
    }


def parcel_lookup(county_fips, apn=None, lat=None, lng=None):
    """Resolve a parcel by APN (preferred) or by point (coords-only leads). Returns parcel dict
    or {'_error': reason} when the county has no adapter / nothing resolves."""
    cfg = COUNTY_PARCEL.get(str(county_fips))
    if not cfg:
        return {"_error": f"no county ArcGIS adapter for fips {county_fips}"}
    base = {"outFields": "*", "returnGeometry": "true", "outSR": 4326, "f": "json"}
    if apn:
        q_apn = (cfg["apn_prefix"] or "") + str(apn) if not str(apn).startswith(cfg["apn_prefix"] or "\0") \
            else str(apn)
        params = dict(base, where=f"{cfg['apn_field']}='{q_apn}'")
        d = _get(cfg["url"], params)
        if d.get("error") and "agination" in str(d.get("error")):
            params.pop("resultRecordCount", None)
            d = _get(cfg["url"], params)
        feats = d.get("features", [])
        if feats:
            return _parcel_from_feature(feats[0], cfg, county_fips)
        # fall through to point if coords available
    if lat is not None and lng is not None:
        params = dict(base,
                      geometry=json.dumps({"x": float(lng), "y": float(lat),
                                           "spatialReference": {"wkid": 4326}}),
                      geometryType="esriGeometryPoint", inSR=4326,
                      spatialRel="esriSpatialRelIntersects")
        d = _get(cfg["url"], params)
        feats = d.get("features", [])
        if feats:
            return _parcel_from_feature(feats[0], cfg, county_fips)
        return {"_error": f"no parcel at point ({lat},{lng}) in fips {county_fips}",
                "_svc": d.get("_error")}
    return {"_error": f"parcel not found for apn={apn} in fips {county_fips}"}


# ------------------------------------------------------------------ flood / wetlands
def flood_cover(pts):
    """% of interior sample points inside a FEMA SFHA (SFHA_TF='T'). Grounded service, sampled %."""
    if not pts:
        return {"value": None, "source": "fema_nfhl", "grounded": False, "confidence": 0.0}
    hits = ok = 0
    for lat, lng in pts:
        d = _get(FEMA_NFHL, {
            "geometry": json.dumps({"x": lng, "y": lat, "spatialReference": {"wkid": 4326}}),
            "geometryType": "esriGeometryPoint", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "SFHA_TF,FLD_ZONE", "returnGeometry": "false", "f": "json"})
        if "_error" in d or d.get("error"):
            continue
        ok += 1
        for f in d.get("features", []):
            if str(f.get("attributes", {}).get("SFHA_TF", "")).upper() == "T":
                hits += 1
                break
    if ok == 0:
        return {"value": None, "source": "fema_nfhl", "grounded": False, "confidence": 0.0,
                "method": "all point queries failed"}
    return {"value": round(100.0 * hits / ok, 1), "source": "fema_nfhl", "grounded": True,
            "confidence": 0.7, "method": f"SFHA point sample n={ok}"}


def wetlands_cover(pts):
    """% of interior sample points inside a USFWS NWI wetland polygon."""
    if not pts:
        return {"value": None, "source": "usfws_nwi", "grounded": False, "confidence": 0.0}
    hits = ok = 0
    for lat, lng in pts:
        # NWI layer 0 is a JOINED layer (fields are qualified 'Wetlands.WETLAND_TYPE'), so an
        # unqualified outFields 400s. returnCountOnly is field-agnostic and reliable: any NWI
        # polygon at the point == wetland present.
        d = _get(NWI_WETLANDS, {
            "geometry": json.dumps({"x": lng, "y": lat, "spatialReference": {"wkid": 4326}}),
            "geometryType": "esriGeometryPoint", "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "returnCountOnly": "true", "f": "json"})
        if "_error" in d or d.get("error"):
            continue
        ok += 1
        if (d.get("count") or 0) > 0:
            hits += 1
    if ok == 0:
        return {"value": None, "source": "usfws_nwi", "grounded": False, "confidence": 0.0,
                "method": "all point queries failed"}
    return {"value": round(100.0 * hits / ok, 1), "source": "usfws_nwi", "grounded": True,
            "confidence": 0.7, "method": f"NWI point sample n={ok}"}


# ------------------------------------------------------------------ slope / buildable (3DEP)
def _haversine_m(a, b):
    (la1, lo1), (la2, lo2) = a, b
    r = 6371000.0
    p1, p2 = math.radians(la1), math.radians(la2)
    dp = math.radians(la2 - la1)
    dl = math.radians(lo2 - lo1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def slope_buildable(pts):
    """Estimate buildable share from USGS 3DEP elevations at the sample grid.
    buildable = share of adjacent-point slopes that are <=10% (reasonably buildable)."""
    if len(pts) < 3:
        return {"buildable": None, "source": "usgs_3dep", "grounded": False, "confidence": 0.0,
                "method": "too few interior points for a slope estimate"}
    elevs = []
    for lat, lng in pts:
        d = _get(EPQS, {"x": lng, "y": lat, "units": "Meters", "wkid": 4326, "includeDate": "false"})
        v = _num(d.get("value")) if isinstance(d, dict) else None
        elevs.append(((lat, lng), v))
    good = [(p, e) for p, e in elevs if e is not None]
    if len(good) < 3:
        return {"buildable": None, "source": "usgs_3dep", "grounded": False, "confidence": 0.0,
                "method": "EPQS returned too few elevations"}
    slopes = []
    for i in range(len(good)):
        for k in range(i + 1, len(good)):
            dist = _haversine_m(good[i][0], good[k][0])
            if dist < 3:
                continue
            slopes.append(abs(good[i][1] - good[k][1]) / dist * 100.0)
    if not slopes:
        return {"buildable": None, "source": "usgs_3dep", "grounded": False, "confidence": 0.0,
                "method": "no measurable point spacing"}
    buildable = round(100.0 * sum(1 for s in slopes if s <= 10.0) / len(slopes), 1)
    return {"buildable": buildable, "source": "usgs_3dep", "grounded": True, "confidence": 0.45,
            "method": f"3DEP pairwise-slope estimate over {len(good)} pts",
            "mean_slope_pct": round(statistics.mean(slopes), 1)}


# ------------------------------------------------------------------ comps ($/acre)
COMPS_SQL = """
    select comp_apn, sale_price, acreage, sale_date, source,
           case when acreage > 0 then (sale_price / acreage) end as ppa
    from landos.comps
    where subject_apn = %s and sale_price >= 1000 and acreage >= 0.01
"""


def comps_for(cur, subject_apn, subject_acres):
    """Median $/acre from landos.comps (engine store). Arms-length + acreage-banded to subject.
    Returns provenance-stamped dict; comps absent -> grounded=False (EV degrades to base value)."""
    if not subject_apn:
        return {"comp_median": None, "comps": [], "n": 0, "source": "landos.comps",
                "grounded": False, "confidence": 0.0, "method": "no subject apn"}
    cur.execute(COMPS_SQL, (subject_apn,))
    rows = cur.fetchall()
    pool = []
    for r in rows:
        ppa = _num(r.get("ppa"))
        ac = _num(r.get("acreage"))
        if ppa is None or not (1000 <= ppa <= 1_000_000):
            continue
        pool.append({"apn": r.get("comp_apn"), "per_ac": ppa, "size": ac,
                     "price": _num(r.get("sale_price")), "sold_date": str(r.get("sale_date") or ""),
                     "source": r.get("source")})
    used = pool
    if subject_acres:
        band = [c for c in pool if c["size"] and 0.5 * subject_acres <= c["size"] <= 2 * subject_acres]
        if len(band) < 3:
            band = [c for c in pool if c["size"] and 0.25 * subject_acres <= c["size"] <= 4 * subject_acres]
        used = band if len(band) >= 3 else pool
    if len(used) < 2:
        return {"comp_median": None, "comps": used, "n": len(used), "source": "landos.comps",
                "grounded": False, "confidence": 0.2,
                "method": f"only {len(used)} arms-length comps for subject"}
    med = statistics.median([c["per_ac"] for c in used])
    return {"comp_median": round(med, 2), "comps": used[:25], "n": len(used),
            "source": "landos.comps", "grounded": True, "confidence": 0.75,
            "method": f"median $/ac over {len(used)} banded arms-length comps"}

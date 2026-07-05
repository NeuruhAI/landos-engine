"""
main.py — the LandOS Engine API.

This is the service APG (and any future LandOS tenant) calls over REST.
It does four jobs:
  1. /evaluate  — score a parcel, store the score, return it with the "why"
  2. /claim + /release — the lead-ownership LOCK (kills duplicate work)
  3. /feedback  — record a deal outcome (fuel for retraining the model)
  4. /leads/top — return the highest-scored UNCLAIMED leads (the team's queue)

Every endpoint except /health requires the X-LANDOS-SECRET header. That secret
is the trust boundary: only callers who hold it (your APG deployment) get in.

Run it with:  uvicorn app.main:app --host 0.0.0.0 --port 8400
"""
import json
import os
from datetime import date
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.responses import PlainTextResponse
from psycopg.types.json import Jsonb

from .config import LANDOS_SECRET
from .db import pool
from .models import (
    EvaluateRequest,
    ClaimRequest,
    ReleaseRequest,
    FeedbackRequest,
    EmergingHeatMapRequest,
    EmergingHeatMapResponse,
    EmergingOpportunity,
    LandDDRequest,
)
from .scoring import score_parcel, DEFAULT_WEIGHTS, EDGE_PROFILE
from . import nve_land
from . import land_dd_adapters as lda
from . import receipt as receipt_mod

app = FastAPI(title="LandOS Engine", version="1.0.0")


# ---------------------------------------------------------------------------
# Auth: every protected route depends on this. It checks the header against
# the secret loaded from .env. Wrong/missing secret -> 401, request stops here.
# ---------------------------------------------------------------------------
def auth(x_landos_secret: str = Header(default="")):
    if not x_landos_secret or x_landos_secret != LANDOS_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing X-LANDOS-SECRET")
    return True


# The CRM's land-dd proxy authenticates with `Authorization: Bearer <token>`, not the
# X-LANDOS-SECRET header the rest of the engine uses. /land-dd is read-only (county-public
# valuation data, no DB writes), so it takes a lenient guard: if NVE_LAND_TOKEN is set the
# Bearer token (or the X-LANDOS-SECRET) must match; if it is NOT set, the endpoint is open
# (matches the CRM's "coming soon" default where LAND_PORTAL_JWT may be any placeholder).
def auth_land_dd(authorization: str = Header(default=""),
                 x_landos_secret: str = Header(default="")):
    required = os.environ.get("NVE_LAND_TOKEN")
    if not required:
        return True
    bearer = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if bearer == required or (x_landos_secret and x_landos_secret == LANDOS_SECRET):
        return True
    raise HTTPException(status_code=401, detail="Invalid or missing land-dd credential")


def _active_weights(cur, tenant_id: str, profile: str = "default"):
    """Load this tenant's active scoring weights for a profile, or fall back to defaults."""
    cur.execute(
        "select version, weights from landos.scoring_weights "
        "where tenant_id = %s and profile = %s and active = true limit 1",
        (tenant_id, profile),
    )
    row = cur.fetchone()
    if row:
        w = row["weights"]
        if isinstance(w, str):  # defensive: jsonb usually decodes to dict already
            w = json.loads(w)
        return row["version"], w
    return 0, DEFAULT_WEIGHTS


@app.get("/health")
def health():
    """Liveness + database reachability. No auth — safe to hit from anywhere."""
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select 1 as ok")
                cur.fetchone()
        db = "up"
    except Exception as e:  # noqa: BLE001 - report any DB failure plainly
        db = f"down: {e.__class__.__name__}"
    return {"status": "ok", "service": "landos-engine", "db": db}


@app.post("/land-dd")
def land_dd(req: LandDDRequest, _=Depends(auth_land_dd)):
    """NVE-LAND: sovereign land due-diligence. Replaces the Land Portal API with tiered
    county ArcGIS + FEMA NFHL + USFWS NWI + USGS 3DEP + engine comps, scored by the exact
    land-portal-dd rubric (app/nve_land.py). Returns the CRM-consumed {land_score, ev_low,
    ev_high} plus the full provenance-stamped diagnostic under `raw`. Read-only — no DB writes;
    the CRM proxy caches the result into acquisition_leads."""
    fips = str(req.county_fips) if req.county_fips else None
    apn = req.parcel_apn or req.apn
    if not fips:
        return {"land_score": None, "ev_low": None, "ev_high": None,
                "valuation_source": "nve-land", "stance": "INSUFFICIENT_DATA",
                "deal_killers": ["county_fips required"], "field_provenance": {},
                "raw": {"error": "county_fips required", "requested_apn": apn}}

    # 1. Parcel + geometry (county ArcGIS) — by APN, else by point (coords-only leads)
    parcel = lda.parcel_lookup(fips, apn=apn, lat=req.lat, lng=req.lng)
    if parcel.get("_error"):
        return {"land_score": None, "ev_low": None, "ev_high": None,
                "valuation_source": "nve-land", "stance": "INSUFFICIENT_DATA",
                "deal_killers": ["parcel not resolvable"],
                "field_provenance": {"parcel": {"grounded": False, "source": "county_arcgis",
                                                 "detail": parcel["_error"]}},
                "raw": {"error": parcel["_error"], "county_fips": fips, "requested_apn": apn,
                        "lat": req.lat, "lng": req.lng, "valuation_source": "nve-land"}}

    # 2. Interior sample points -> flood / wetlands / slope (federal REST)
    pts = lda.sample_points(parcel["rings"], max_pts=max(3, min(req.samples, 9)))
    flood = lda.flood_cover(pts)
    wet = lda.wetlands_cover(pts)
    slope = lda.slope_buildable(pts)

    # 3. Comps ($/acre) from the engine store (landos.comps)
    comps_subject = apn or parcel.get("apn")
    with pool.connection() as conn:
        with conn.cursor() as cur:
            comps = lda.comps_for(cur, comps_subject, parcel.get("acres"))

    # 4. Assemble scorer inputs. Unverified feeds arrive as None (never fabricated).
    metrics = {
        "size": parcel.get("acres"),
        "land_locked": None,            # not derivable from free county REST in v1 -> unverified
        "road_frontage": None,          # unverified (no roads-layer intersection in v1)
        "wetlands": wet.get("value"),
        "fema": flood.get("value"),
        "buildable": slope.get("buildable"),
        "flop15": None,
        "base_per_ac": parcel.get("base_per_ac"),
        "base_total": parcel.get("base_total"),
        "zip_per_ac": None,
        "county_per_ac": None,
        "comp_median": comps.get("comp_median"),
    }
    result = nve_land.assess({"metrics": metrics, "comps": comps.get("comps", []), "parcel": parcel})

    # 5. Asset-class anomaly (rubric Step 0): an improved parcel scored as vacant land
    anomaly = None
    if parcel.get("improved"):
        anomaly = (f"Asset-class anomaly: class {parcel.get('class_code')} "
                   f"({parcel.get('asset_class')}, IMPROVED) has a structure. The land score and "
                   f"EV treat it as vacant land and do not reflect the improvement.")
        result["red_flags"] = [anomaly] + result.get("red_flags", [])

    # 6. field_provenance — grounded-vs-estimated travels with every field
    def _pick(d, keys):
        return {k: d.get(k) for k in keys}
    prov = {
        "acreage": parcel["provenance"]["acreage"],
        "base_value": parcel["provenance"]["base_value"],
        "owner": parcel["provenance"]["owner"],
        "class_code": parcel["provenance"]["class_code"],
        "flood_pct": _pick(flood, ("value", "source", "grounded", "confidence", "method")),
        "wetlands_pct": _pick(wet, ("value", "source", "grounded", "confidence", "method")),
        "buildable_pct": _pick(slope, ("buildable", "source", "grounded", "confidence", "method")),
        "comps": _pick(comps, ("comp_median", "n", "source", "grounded", "confidence", "method")),
        "road_frontage": {"value": None, "grounded": False,
                          "note": "not derivable from free county REST — verify on the ground"},
        "land_locked": {"value": None, "grounded": False, "note": "unverified — verify legal access"},
        "estimated_factors": result.get("estimated_factors", []),
    }

    ev = result["expected_value"]
    prim = result.get("primary_strategy") or {}

    def _r(x):
        return round(x) if isinstance(x, (int, float)) else None

    return {
        # --- consumed by the CRM proxy (land_score int, ev_* numeric) ---
        "land_score": result["land_score"],
        "ev_low": _r(ev.get("low")),
        "ev_high": _r(ev.get("high")),
        # --- STEP-2 contract fields (persisted into valuation_raw; typed readers later) ---
        "stance": result["stance"],
        "deal_killers": result["deal_killers"],
        "access_score": result["score_breakdown"].get("Access"),
        "offer_band_low": _r(prim.get("offer_low")),
        "offer_band_high": _r(prim.get("offer_high")),
        "valuation_source": "nve-land",
        "field_provenance": prov,
        "asset_class_anomaly": anomaly,
        # --- full diagnostic detail -> valuation_raw jsonb ---
        "raw": {
            "parcel": _pick(parcel, ("apn", "owner", "acres", "class_code", "asset_class",
                                     "improved", "sev", "base_total", "base_per_ac", "legal",
                                     "situs", "centroid")),
            "score_breakdown": result["score_breakdown"], "score_max": result["score_max"],
            "estimated_factors": result["estimated_factors"],
            "valuation_gap_ratio": result["valuation_gap_ratio"],
            "expected_value": ev,
            "primary_strategy": result.get("primary_strategy"),
            "secondary_strategy": result.get("secondary_strategy"),
            "red_flags": result["red_flags"], "green_flags": result["green_flags"],
            "usable_acres": result["usable_acres"], "sample_points": len(pts),
            "feeds": {"flood": flood, "wetlands": wet, "slope": slope,
                      "comps_meta": _pick(comps, ("n", "source", "grounded", "method"))},
            "field_provenance": prov, "county_fips": fips, "requested_apn": apn,
            "lead_id": req.lead_id, "valuation_source": "nve-land",
        },
    }


@app.get("/lead-receipt/{lead_id}")
def lead_receipt(lead_id: str, format: str = "json", _=Depends(auth_land_dd)):
    """AUDITABLE-ACQUISITION: a tamper-evident, per-lead provenance receipt assembled
    from the real spine — SIGNAL / SCORING / PIERCE / COMPLIANCE / FRESHNESS, each
    SHA-256'd, under one chain hash (see app/receipt.py). `lead_id` may be the engine
    owner_leads.lead_id or a board acquisition_leads.lead_id (resolved via source_lead_id).
    JSON by default; `?format=md` renders the human-readable receipt document. Read-only;
    same Bearer-tolerant guard as /land-dd. Never emits third-party PII beyond the lead row."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            receipt = receipt_mod.assemble_receipt(cur, lead_id, date.today())
    if receipt is None:
        raise HTTPException(status_code=404, detail=f"No lead resolves for {lead_id}")
    if format == "md":
        return PlainTextResponse(receipt_mod.render_markdown(receipt))
    return receipt


@app.post("/evaluate")
def evaluate(req: EvaluateRequest, _=Depends(auth)):
    """
    Score a parcel. Resolves the canonical identity node (find-or-create),
    computes the score, stores it, logs the event, and writes an audit record
    — all in one transaction.
    """
    # The benton_harbor_edge profile uses a different input shape (geo sub-scores
    # + policy overlays + gates) and is scored via the batch CLI in v1.
    if req.profile == EDGE_PROFILE:
        raise HTTPException(
            status_code=400,
            detail="Profile 'benton_harbor_edge' is scored via the batch CLI "
                   "(python -m scripts.score_addresses), not /evaluate, in v1.",
        )
    inputs = {
        "distress": req.distress.model_dump(),
        "owner": req.owner.model_dump(),
        "arbitrage": req.arbitrage.model_dump(),
    }
    with pool.connection() as conn:
        with conn.cursor() as cur:
            version, weights = _active_weights(cur, req.tenant_id, req.profile)
            result = score_parcel(inputs, weights)

            # Find-or-create the identity node. ON CONFLICT means the same
            # (tenant, parcel, owner) never creates a duplicate — it just
            # touches updated_at and returns the existing id. This is the dedup.
            cur.execute(
                """
                insert into landos.identity_nodes
                    (tenant_id, parcel_apn, owner_hash, county, state)
                values (%s, %s, %s, %s, %s)
                on conflict (tenant_id, parcel_apn, owner_hash)
                    do update set updated_at = now()
                returning id
                """,
                (req.tenant_id, req.parcel_apn, req.owner_hash, req.county, req.state),
            )
            node_id = cur.fetchone()["id"]

            # Store the score, including the inputs snapshot (used later to retrain).
            cur.execute(
                """
                insert into landos.scores
                    (tenant_id, identity_node_id, score, distress_component,
                     owner_component, arbitrage_component, top_factors,
                     weights_version, inputs_snapshot)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (
                    req.tenant_id, node_id, result["score"],
                    result["distress_component"], result["owner_component"],
                    result["arbitrage_component"], Jsonb(result["top_factors"]),
                    version, Jsonb(inputs),
                ),
            )
            score_id = cur.fetchone()["id"]

            # Append to the cross-channel event ledger (idempotency_key unique).
            cur.execute(
                """
                insert into landos.event_ledger
                    (tenant_id, identity_node_id, channel, actor, action,
                     payload, idempotency_key)
                values (%s, %s, 'system', 'scoring-service', 'scored', %s, %s)
                """,
                (req.tenant_id, node_id,
                 Jsonb({"score": result["score"], "score_id": str(score_id)}),
                 f"score:{score_id}"),
            )

            # Audit trail.
            cur.execute(
                """
                insert into landos.audit_log
                    (tenant_id, actor, operation, target_table, target_id, metadata)
                values (%s, 'scoring-service', 'score_request', 'scores', %s, %s)
                """,
                (req.tenant_id, score_id, Jsonb({"parcel_apn": req.parcel_apn})),
            )

    return {
        "node_id": str(node_id),
        "score_id": str(score_id),
        "weights_version": version,
        **result,
    }


@app.post("/claim")
def claim(req: ClaimRequest, _=Depends(auth)):
    """
    Claim a lead for a team member. If someone already holds an active claim,
    returns 409 with who holds it. The database's partial-unique index is the
    hard guarantee — even under a race, two active claims are impossible.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "select claimed_by, claimed_at from landos.claims "
                "where identity_node_id = %s and status = 'active' limit 1",
                (req.node_id,),
            )
            existing = cur.fetchone()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "Lead already claimed",
                        "claimed_by": existing["claimed_by"],
                        "claimed_at": existing["claimed_at"].isoformat()
                        if existing["claimed_at"] else None,
                    },
                )
            cur.execute(
                "insert into landos.claims (tenant_id, identity_node_id, claimed_by, status) "
                "values (%s, %s, %s, 'active') returning id",
                (req.tenant_id, req.node_id, req.claimed_by),
            )
            claim_id = cur.fetchone()["id"]
            cur.execute(
                """
                insert into landos.event_ledger
                    (tenant_id, identity_node_id, channel, actor, action,
                     payload, idempotency_key)
                values (%s, %s, 'system', %s, 'claimed', %s, %s)
                """,
                (req.tenant_id, req.node_id, req.claimed_by,
                 Jsonb({"claim_id": str(claim_id)}), f"claim:{claim_id}"),
            )
            cur.execute(
                "insert into landos.audit_log (tenant_id, actor, operation, target_table, target_id) "
                "values (%s, %s, 'claim', 'claims', %s)",
                (req.tenant_id, req.claimed_by, claim_id),
            )
    return {
        "status": "claimed",
        "claim_id": str(claim_id),
        "node_id": req.node_id,
        "claimed_by": req.claimed_by,
    }


@app.post("/release")
def release(req: ReleaseRequest, _=Depends(auth)):
    """Release the active claim on a lead so someone else can work it."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "update landos.claims set status = 'released', released_at = now() "
                "where identity_node_id = %s and status = 'active' "
                "returning id, claimed_by",
                (req.node_id,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="No active claim on this lead")
            cur.execute(
                """
                insert into landos.event_ledger
                    (tenant_id, identity_node_id, channel, actor, action,
                     payload, idempotency_key)
                values (%s, %s, 'system', %s, 'released', %s, %s)
                """,
                (req.tenant_id, req.node_id, row["claimed_by"],
                 Jsonb({}), f"release:{row['id']}"),
            )
    return {"status": "released", "node_id": req.node_id}


@app.post("/feedback")
def feedback(req: FeedbackRequest, _=Depends(auth)):
    """
    Record a deal outcome (closed / dead / passed). This is the fuel for the
    recursive feedback loop. v1 stores the outcome; the retrain hook below is
    where v2 recomputes weights from accumulated outcomes.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into landos.feedback_events
                    (tenant_id, identity_node_id, outcome, projected_multiple,
                     actual_multiple, notes)
                values (%s, %s, %s, %s, %s, %s)
                returning id
                """,
                (req.tenant_id, req.node_id, req.outcome,
                 req.projected_multiple, req.actual_multiple, req.notes),
            )
            fid = cur.fetchone()["id"]
            cur.execute(
                """
                insert into landos.event_ledger
                    (tenant_id, identity_node_id, channel, actor, action,
                     payload, idempotency_key)
                values (%s, %s, 'system', 'scoring-service', 'feedback', %s, %s)
                """,
                (req.tenant_id, req.node_id,
                 Jsonb({"outcome": req.outcome}), f"feedback:{fid}"),
            )
            cur.execute(
                "insert into landos.audit_log (tenant_id, actor, operation, target_table, target_id, metadata) "
                "values (%s, 'scoring-service', 'feedback', 'feedback_events', %s, %s)",
                (req.tenant_id, fid, Jsonb({"outcome": req.outcome})),
            )
    # --- RETRAIN HOOK (v2) ---------------------------------------------------
    # Pull landos.feedback_events for this tenant, compare projected vs actual
    # multiples and closed/dead ratios per signal, nudge the weights, then write
    # a NEW landos.scoring_weights version with active=true. Left as a clean
    # extension point so v1 ships today.
    # -------------------------------------------------------------------------
    return {"status": "recorded", "feedback_id": str(fid)}


@app.get("/leads/top")
def leads_top(tenant_id: str, limit: int = 25, _=Depends(auth)):
    """
    The team's work queue: highest-scored leads that nobody has claimed yet.
    Uses the latest score per node and excludes any node with an active claim.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                select s.identity_node_id, n.parcel_apn, n.county, n.state,
                       s.score, s.top_factors, s.computed_at
                from (
                    select distinct on (identity_node_id) *
                    from landos.scores
                    where tenant_id = %s
                    order by identity_node_id, computed_at desc
                ) s
                join landos.identity_nodes n on n.id = s.identity_node_id
                where not exists (
                    select 1 from landos.claims c
                    where c.identity_node_id = s.identity_node_id
                      and c.status = 'active'
                )
                order by s.score desc
                limit %s
                """,
                (tenant_id, limit),
            )
            rows = cur.fetchall()
    leads = [
        {
            "node_id": str(r["identity_node_id"]),
            "parcel_apn": r["parcel_apn"],
            "county": r["county"],
            "state": r["state"],
            "score": float(r["score"]),
            "top_factors": r["top_factors"],
            "computed_at": r["computed_at"].isoformat(),
        }
        for r in rows
    ]
    return {"tenant_id": tenant_id, "count": len(leads), "leads": leads}


@app.get("/emerging/heat-map", response_model=EmergingHeatMapResponse)
def emerging_heat_map(
    tenant_id: str,
    county_fips: Optional[str] = None,
    min_emerging_score: float = 50.0,
    include_dale_local: bool = True,
    limit: int = 500,
    _=Depends(auth),
):
    """
    Heat map data for emerging pre-market boomer opportunities.
    Returns scored opportunities with signal breakdown for CRM map layers
    (permit_delta, satellite_delta, negative_space, boomer_life_event, dale_local).

    This powers the explicit emerging layers in the CRM MapView and /emerging module.
    Data is the fusion of LandOS discovery adapters (deltas, negative space, life events)
    + DALE local intel. The 'emerging_score' is the blended pre-market motivation score.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Query owner_leads joined with recent discovery_signals for emerging signals.
            # In production the ingest_emerging script also materializes a denormalized view
            # or table; this is the live query version for the API.
            where = ["ol.tenant_id = %s"]
            params: list = [tenant_id]
            if county_fips:
                where.append("ol.county_fips = %s")
                params.append(county_fips)
            if not include_dale_local:
                where.append("(coalesce( (ol.raw_meta->>'dale_local')::bool , false) = false )")

            sql = f"""
                select 
                    ol.lead_id,
                    ol.parcel_apn,
                    ol.owner_name,
                    ol.situs_address,
                    ol.county_fips,
                    ol.final_lead_score as base_score,
                    -- Emerging score: blend base LandOS score with strongest recent signal confidence.
                    -- Real production version comes pre-computed from the emerging ingest pipeline.
                    greatest(
                        ol.final_lead_score,
                        60 + 40 * coalesce((
                            select max( (ds.raw_payload->>'confidence')::float )
                            from landos.discovery_signals ds
                            where ds.matched_lead_id = ol.lead_id
                              and ds.signal_date > now() - interval '180 days'
                        ), 0)
                    ) as emerging_score,
                    -- Build signals json from recent discovery rows (one per type for demo)
                    jsonb_object_agg(
                        ds.source_type,
                        jsonb_build_object(
                            'date', ds.signal_date,
                            'confidence', (ds.raw_payload->>'confidence')::float,
                            'raw', ds.raw_payload
                        )
                    ) filter (where ds.id is not null) as emerging_signals,
                    exists (
                        select 1 from landos.discovery_signals ds2
                        where ds2.matched_lead_id = ol.lead_id and ds2.source_type like 'dale%'
                    ) as dale_local,
                    ol.situs_lat, ol.situs_lng,
                    (select max(ds.signal_date) from landos.discovery_signals ds where ds.matched_lead_id = ol.lead_id) as last_signal_date
                from landos.owner_leads ol
                left join lateral (
                    select * from landos.discovery_signals ds
                    where ds.matched_lead_id = ol.lead_id
                    order by ds.signal_date desc
                    limit 5
                ) ds on true
                where {" and ".join(where)}
                group by ol.lead_id
                having greatest(
                    ol.final_lead_score,
                    60 + 40 * coalesce((
                        select max( (ds.raw_payload->>'confidence')::float )
                        from landos.discovery_signals ds
                        where ds.matched_lead_id = ol.lead_id
                          and ds.signal_date > now() - interval '180 days'
                    ), 0)
                ) >= %s
                order by emerging_score desc
                limit %s
            """
            params.extend([min_emerging_score, limit])
            cur.execute(sql, params)
            rows = cur.fetchall()

    opps = []
    for r in rows:
        opps.append(EmergingOpportunity(
            lead_id=str(r["lead_id"]),
            parcel_apn=r["parcel_apn"],
            owner_name=r["owner_name"],
            situs_address=r["situs_address"],
            county_fips=r["county_fips"],
            base_score=float(r["base_score"]) if r["base_score"] is not None else None,
            emerging_score=float(r["emerging_score"]),
            emerging_signals=r["emerging_signals"] or {},
            dale_local=bool(r["dale_local"]),
            lat=float(r["situs_lat"]) if r["situs_lat"] is not None else None,
            lng=float(r["situs_lng"]) if r["situs_lng"] is not None else None,
            last_signal_date=r["last_signal_date"].isoformat() if r["last_signal_date"] else None,
        ))

    return EmergingHeatMapResponse(
        tenant_id=tenant_id,
        count=len(opps),
        opportunities=opps,
    )


if __name__ == "__main__":
    main()

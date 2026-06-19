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
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Depends
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
)
from .scoring import score_parcel, DEFAULT_WEIGHTS, EDGE_PROFILE

app = FastAPI(title="LandOS Engine", version="1.0.0")


# ---------------------------------------------------------------------------
# Auth: every protected route depends on this. It checks the header against
# the secret loaded from .env. Wrong/missing secret -> 401, request stops here.
# ---------------------------------------------------------------------------
def auth(x_landos_secret: str = Header(default="")):
    if not x_landos_secret or x_landos_secret != LANDOS_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing X-LANDOS-SECRET")
    return True


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

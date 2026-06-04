# LandOS Engine

Neuruh-owned scoring + coordination service. Lives on the Neuruh Supabase
(`lvdimujkfheeyqchjzra`, schema `landos`). APG and future LandOS tenants call
it over REST — they never hold the model logic. This is the crown jewel.

## What it does
- **Scores parcels** (0–100) with explainable top-factor reasoning
- **Locks leads** so no two team members work the same one (DB-enforced)
- **Logs every action** across channels into one event ledger (dedup via idempotency keys)
- **Records deal outcomes** to retrain the model over time

## Endpoints
| Method | Path | Purpose |
|--------|------|---------|
| GET  | `/health`     | Liveness + DB check (no auth) |
| POST | `/evaluate`   | Score a parcel, store it, return score + why |
| POST | `/claim`      | Claim a lead (409 if already claimed) |
| POST | `/release`    | Release a claim |
| POST | `/feedback`   | Record a deal outcome |
| GET  | `/leads/top`  | Top-scored unclaimed leads (the queue) |

All except `/health` require header `X-LANDOS-SECRET: <your secret>`.

## Run
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then edit .env with real values
python -m scripts.seed_weights <TENANT_ID>
uvicorn app.main:app --host 0.0.0.0 --port 8400
```

## Architecture notes
- Multi-tenant from day one — each customer is a row in `landos.tenants`.
- No hardcoded branding. White-label / resale ready.
- RLS on every table; this service connects as the trusted backend.
- Audit logging on sensitive operations (SOC2 / ISO 27001 alignment).

# Deploying the LandOS Engine (durable host) + wiring the CRM

The engine is a stateless FastAPI service. It connects **out** to Supabase (`DATABASE_URL`)
and serves the REST API (incl. `POST /land-dd`). Hosting it on Fly.io gives the CRM (on Vercel)
a stable HTTPS URL to call. Files in this repo: `Dockerfile`, `fly.toml`, `.dockerignore`.

## One-time: install + auth (interactive — operator runs these)

```bash
brew install flyctl          # or: curl -L https://fly.io/install.sh | sh
fly auth login               # opens a browser — needs a Fly account + card on file (always-on ~$2-5/mo)
```

## Launch (from this repo root: ~/neuruh-repos/NeuruhAI/landos-engine)

```bash
fly launch --no-deploy --copy-config --name landos-engine --region iad
# --copy-config reuses fly.toml; --no-deploy so we set secrets BEFORE first boot
# (config.py fail-closes if DATABASE_URL / LANDOS_SECRET are missing).
```

## Set secrets (values pulled from local .env — never commit them)

```bash
# run from the repo root; reads .env, sets Fly secrets without echoing values
set -a; . ./.env; set +a
fly secrets set \
  DATABASE_URL="$DATABASE_URL" \
  LANDOS_SECRET="$LANDOS_SECRET" \
  NVE_LAND_TOKEN="$LANDOS_SECRET"     # lock /land-dd to a Bearer token (reuse LANDOS_SECRET or pick a new one)
```

## Deploy + verify

```bash
fly deploy
fly status                       # confirm 1 machine, health check passing on /health
APPURL="https://$(fly info --json | python3 -c 'import sys,json;print(json.load(sys.stdin)["Hostname"])')"
curl -s "$APPURL/health"         # -> {"status":"ok","db":"up"}
curl -s -X POST "$APPURL/land-dd" -H "Authorization: Bearer <NVE_LAND_TOKEN>" \
  -H 'Content-Type: application/json' \
  -d '{"county_fips":"26021","lat":42.0144742,"lng":-86.3592138}'   # Belinda -> land_score + anomaly
```

## Wire the CRM (Vercel project: apg-land-crm, scope anchorpointpg)

Set two prod env vars, then redeploy the CRM:

```bash
# in the CRM repo (~/neuruh-repos/AnchorPG/apg-land-crm):
vercel env add VALUATION_ENGINE_URL production   # value: the $APPURL above
vercel env add LAND_PORTAL_JWT production         # value: the NVE_LAND_TOKEN you set on Fly
vercel --prod                                     # redeploy so the vars take effect
```

The CRM gates on `VALUATION_ENGINE_URL && LAND_PORTAL_JWT` both being set (page.tsx) — once both
are present it flips from the graceful "engine offline" state to live, and `/acquisition/[leadId]`
"Run valuation" calls `POST $VALUATION_ENGINE_URL/land-dd` with `Authorization: Bearer $LAND_PORTAL_JWT`.

## Notes
- **Always-on** (`auto_stop_machines="off"`, `min_machines_running=1`) is deliberate: the CRM's
  land-dd fetch has a 30s timeout, so a cold-starting machine would fail the call.
- **Alternatives:** Render (native Python, but free tier cold-starts — use a paid always-on plan) or
  a VPS (`uvicorn` behind caddy/nginx). Fly is the least-effort always-on option.
- The container image is `app/` only — batch jobs (mint/score/sync, which need the local JSONL
  staging tree) keep running on the workstation, not in the container.

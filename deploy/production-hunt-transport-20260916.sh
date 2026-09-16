#!/bin/zsh
# Production HUNT transport bootstrap for the canonical LandOS host.
#
# Result:
#   DeedSonar/Vercel --HTTPS--> landos.neuruh.com --Cloudflare Tunnel-->
#   127.0.0.1:8400 (LandOS FastAPI) --> /hunt --> hunt_jobs
#   launchd also keeps app.hunt_worker draining queued jobs.
#
# Secrets are kept ONLY in ~/.neuruh/secrets/landos-hunt-token (0600) and the
# generated launchd plist. Nothing secret is committed to git.
set -euo pipefail

REPO="${HOME}/neuruh-repos/NeuruhAI/landos-engine"
PY="${REPO}/.venv/bin/python"
PORT="8400"
HOSTNAME="landos.neuruh.com"
TUNNEL="neuruh-landos-origin"
SECRET_DIR="${HOME}/.neuruh/secrets"
TOKEN_FILE="${SECRET_DIR}/landos-hunt-token"
ENV_FILE="${REPO}/.env"
API_PLIST="${HOME}/Library/LaunchAgents/com.neuruh.landos-engine.api.plist"
WORKER_PLIST="${HOME}/Library/LaunchAgents/com.neuruh.landos-engine.hunt-worker.plist"
TUNNEL_PLIST="${HOME}/Library/LaunchAgents/com.neuruh.landos-engine.hunt-origin-tunnel.plist"
CF_DIR="${HOME}/.cloudflared"
CF_CONFIG="${CF_DIR}/${TUNNEL}.yml"
LOG_DIR="${REPO}/data/hunt-runtime"
GUI="gui/$(id -u)"

fail() { print -u2 "ERROR: $*"; exit 1; }

[[ -d "$REPO" ]] || fail "canonical repo missing: $REPO"
[[ -x "$PY" ]] || fail "LandOS venv missing: $PY"
[[ -f "$ENV_FILE" ]] || fail "LandOS .env missing: $ENV_FILE"
[[ -s "${CF_DIR}/cert.pem" ]] || fail "cloudflared is not logged in; run: cloudflared tunnel login"
command -v cloudflared >/dev/null 2>&1 || fail "cloudflared missing; install with: brew install cloudflared"
command -v curl >/dev/null 2>&1 || fail "curl missing"

# Load the canonical env once so the API's own config remains authoritative.
set -a
source "$ENV_FILE"
set +a
[[ -n "${DEEDSONAR_DATABASE_URL:-${DATABASE_URL:-}}" ]] || fail "canonical database URL missing"
[[ -n "${LANDOS_SECRET:-}" ]] || fail "LANDOS_SECRET missing from $ENV_FILE"

# Generate/adopt the dedicated DeedSonar->LandOS bearer token.
mkdir -p "$SECRET_DIR"; chmod 700 "$SECRET_DIR"
if [[ ! -s "$TOKEN_FILE" ]]; then
  openssl rand -base64 48 | tr -d '\n' > "$TOKEN_FILE"
  print "generated $TOKEN_FILE"
else
  print "adopting existing $TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"
TOKEN="$(cat "$TOKEN_FILE")"
[[ ${#TOKEN} -ge 32 ]] || fail "hunt token is too short"

mkdir -p "$LOG_DIR"

# API service. Bind loopback only; Cloudflare is the only public path.
cat > "$API_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.neuruh.landos-engine.api</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>uvicorn</string><string>app.main:app</string>
    <string>--host</string><string>127.0.0.1</string><string>--port</string><string>$PORT</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>DEEDSONAR_DATABASE_URL</key><string>${DEEDSONAR_DATABASE_URL:-}</string>
    <key>DATABASE_URL</key><string>${DATABASE_URL:-}</string>
    <key>LANDOS_SECRET</key><string>$LANDOS_SECRET</string>
    <key>NVE_LAND_TOKEN</key><string>$TOKEN</string>
    <key>PORT</key><string>$PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/api.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/api.err.log</string>
</dict></plist>
PLIST
chmod 600 "$API_PLIST"

# Queue worker. Each pass advances every queued Hunt to the existing terminal
# proposal state. No owner_leads write occurs before operator mint.
cat > "$WORKER_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.neuruh.landos-engine.hunt-worker</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>-lc</string>
    <string>while true; do cd "$REPO"; "$PY" -m app.hunt_worker || true; sleep 15; done</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>DEEDSONAR_DATABASE_URL</key><string>${DEEDSONAR_DATABASE_URL:-}</string>
    <key>DATABASE_URL</key><string>${DATABASE_URL:-}</string>
    <key>LANDOS_SECRET</key><string>$LANDOS_SECRET</string>
    <key>NVE_LAND_TOKEN</key><string>$TOKEN</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/worker.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/worker.err.log</string>
</dict></plist>
PLIST
chmod 600 "$WORKER_PLIST"

# Create/adopt the dedicated outbound Cloudflare tunnel.
if ! cloudflared tunnel list --output json 2>/dev/null | grep -q "\"name\":\"$TUNNEL\""; then
  cloudflared tunnel create "$TUNNEL"
fi
UUID="$(cloudflared tunnel list --output json | python3 -c "import json,sys; print(next(t['id'] for t in json.load(sys.stdin) if t['name']=='$TUNNEL'))")"
cloudflared tunnel route dns --overwrite-dns "$TUNNEL" "$HOSTNAME"
cat > "$CF_CONFIG" <<YAML
# Dedicated LandOS Hunt transport. Public path is HTTPS at the edge;
# the origin is loopback-only on the operator Mac.
tunnel: $UUID
credentials-file: $CF_DIR/$UUID.json
ingress:
  - hostname: $HOSTNAME
    service: http://127.0.0.1:$PORT
  - service: http_status:404
YAML
chmod 600 "$CF_CONFIG"
cloudflared tunnel --config "$CF_CONFIG" ingress validate

cat > "$TUNNEL_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple Computer//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.neuruh.landos-engine.hunt-origin-tunnel</string>
  <key>ProgramArguments</key><array>
    <string>$(command -v cloudflared)</string><string>tunnel</string><string>--config</string><string>$CF_CONFIG</string><string>--no-autoupdate</string><string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/tunnel.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/tunnel.err.log</string>
</dict></plist>
PLIST
chmod 600 "$TUNNEL_PLIST"

# Reload all three services. Never expose 8400 publicly.
for p in "$API_PLIST" "$WORKER_PLIST" "$TUNNEL_PLIST"; do
  plutil -lint "$p" >/dev/null || fail "invalid plist: $p"
done
for label in \
  com.neuruh.landos-engine.api \
  com.neuruh.landos-engine.hunt-worker \
  com.neuruh.landos-engine.hunt-origin-tunnel; do
  launchctl bootout "$GUI/$label" 2>/dev/null || true
done
launchctl bootstrap "$GUI" "$API_PLIST"
launchctl bootstrap "$GUI" "$WORKER_PLIST"
launchctl bootstrap "$GUI" "$TUNNEL_PLIST"

print "waiting for LandOS API..."
for i in {1..30}; do
  if [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" || true)" == "200" ]]; then break; fi
  sleep 1
done
LOCAL_CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/health" || true)"
[[ "$LOCAL_CODE" == "200" ]] || fail "LandOS local health failed; inspect $LOG_DIR/api.err.log"

# Protected route must be 401 without credentials.
UNAUTH="$(curl -s -o /dev/null -w '%{http_code}' -X POST "http://127.0.0.1:$PORT/hunt" -H 'content-type: application/json' -d '{"territory":[{"fips":"37183"}]}' || true)"
[[ "$UNAUTH" == "401" ]] || fail "Hunt auth guard failed: expected 401, got $UNAUTH"

print "local LandOS health : HTTP $LOCAL_CODE"
print "local /hunt unsigned: HTTP $UNAUTH (401 is correct)"
print "public origin        : https://$HOSTNAME"
print "hunt token file      : $TOKEN_FILE (0600)"
print "Vercel env values required on DeedSonar production:"
print "  LANDOS_ENGINE_URL=https://$HOSTNAME"
print "  LANDOS_ENGINE_TOKEN=<contents of $TOKEN_FILE>"
print "Run this from the linked DeedSonar project checkout to configure Vercel:"
print "  printf '%s' 'https://$HOSTNAME' | vercel env add LANDOS_ENGINE_URL production"
print "  printf '%s' \"\$(cat '$TOKEN_FILE')\" | vercel env add LANDOS_ENGINE_TOKEN production"
print "Then redeploy production and exercise a saved Hunt recipe."
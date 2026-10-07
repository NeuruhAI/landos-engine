#!/bin/bash
set -euo pipefail
REPO="$HOME/neuruh-repos/NeuruhAI/landos-engine"
TOKEN_FILE="$HOME/.neuruh/secrets/landos-hunt-token"
cd "$REPO"
if [ -f "$REPO/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$REPO/.env"
  set +a
fi
TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
if [ ${#TOKEN} -lt 16 ]; then
  echo "landos-api: NVE_LAND_TOKEN file empty/short" >&2
  exit 1
fi
export NVE_LAND_TOKEN="$TOKEN"
export PORT=8410
echo "landos-api: starting with NVE_LAND_TOKEN_LENGTH=${#NVE_LAND_TOKEN}" >&2
exec "$REPO/.venv/bin/python" -m uvicorn app.main:app --host 127.0.0.1 --port 8410

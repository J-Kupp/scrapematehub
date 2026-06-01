#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=${APP_ROOT:-/opt/yourbarmate-suppliers}
APP_DIR=${APP_DIR:-$APP_ROOT/app}
VENV_DIR=${VENV_DIR:-$APP_ROOT/venv}
SERVICE_NAME=${SERVICE_NAME:-yourbarmate-suppliers}
HEALTHCHECK_URL=${HEALTHCHECK_URL:-http://127.0.0.1:8765/healthz}

if [ ! -d "$APP_DIR" ]; then
  echo "App directory not found: $APP_DIR" >&2
  exit 1
fi

cd "$APP_DIR"

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install -r requirements.txt

sudo systemctl restart "$SERVICE_NAME"

for _ in $(seq 1 20); do
  if curl --silent --fail "$HEALTHCHECK_URL" >/dev/null; then
    echo "Deployment health check passed."
    exit 0
  fi
  sleep 3
done

echo "Deployment health check failed: $HEALTHCHECK_URL" >&2
exit 1

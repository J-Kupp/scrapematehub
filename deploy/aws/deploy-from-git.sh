#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=${APP_ROOT:-/opt/yourbarmate-suppliers}
APP_DIR=${APP_DIR:-$APP_ROOT/app}
VENV_DIR=${VENV_DIR:-$APP_ROOT/venv}
SERVICE_NAME=${SERVICE_NAME:-yourbarmate-suppliers}
HEALTHCHECK_URL=${HEALTHCHECK_URL:-http://127.0.0.1:8765/healthz}
CONTROL_PANEL_STATE_DIR=${CONTROL_PANEL_STATE_DIR:-/var/lib/yourbarmate-suppliers/control_panel}
RELEASE_METADATA_PATH=${RELEASE_METADATA_PATH:-$CONTROL_PANEL_STATE_DIR/release.json}
DEPLOY_SOURCE_REVISION=${DEPLOY_SOURCE_REVISION:-}
DEPLOY_SOURCE_LABEL=${DEPLOY_SOURCE_LABEL:-github-actions}

if [ ! -d "$APP_DIR" ]; then
  echo "App directory not found: $APP_DIR" >&2
  exit 1
fi

cd "$APP_DIR"

if [ ! -d "$VENV_DIR" ]; then
  python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/pip" install -r requirements.txt

mkdir -p "$CONTROL_PANEL_STATE_DIR"
python3 - <<PY
import json
from datetime import datetime, timezone
from pathlib import Path
import socket

release_path = Path(${RELEASE_METADATA_PATH@Q})
revision = ${DEPLOY_SOURCE_REVISION@Q}.strip()
source = ${DEPLOY_SOURCE_LABEL@Q}
if not revision:
    git_head = Path(${APP_DIR@Q}) / ".git"
    if git_head.exists():
        import subprocess
        try:
            revision = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=${APP_DIR@Q},
                text=True,
            ).strip()
        except Exception:
            revision = ""
payload = {
    "revision": revision,
    "deployed_at": datetime.now(timezone.utc).isoformat(),
    "source": source,
    "hostname": socket.gethostname(),
}
release_path.write_text(json.dumps(payload, indent=2) + "\\n", encoding="utf-8")
PY

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

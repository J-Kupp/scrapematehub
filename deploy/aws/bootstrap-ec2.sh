#!/usr/bin/env bash
set -euo pipefail

APP_ROOT=/opt/yourbarmate-suppliers
APP_DIR="$APP_ROOT/app"
VENV_DIR="$APP_ROOT/venv"
DATA_ROOT=/var/lib/yourbarmate-suppliers
LOG_ROOT=/var/log/yourbarmate-suppliers
ENV_FILE=/etc/yourbarmate-suppliers.env

sudo apt-get update
sudo apt-get install -y \
  git \
  python3 \
  python3-venv \
  python3-pip \
  curl \
  unzip \
  ca-certificates \
  gnupg \
  libasound2t64 \
  libatk-bridge2.0-0 \
  libatk1.0-0 \
  libcups2 \
  libdbus-1-3 \
  libdrm2 \
  libgbm1 \
  libgtk-3-0 \
  libnspr4 \
  libnss3 \
  libxcomposite1 \
  libxdamage1 \
  libxfixes3 \
  libxkbcommon0 \
  libxrandr2 \
  fonts-liberation

if ! command -v caddy >/dev/null 2>&1; then
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y caddy
fi

sudo mkdir -p "$APP_ROOT" "$DATA_ROOT/control_panel" "$DATA_ROOT/state" "$DATA_ROOT/output" "$DATA_ROOT/cache" "$DATA_ROOT/playwright-browsers" "$LOG_ROOT"
sudo chown -R "$USER":"$USER" "$APP_ROOT" "$DATA_ROOT" "$LOG_ROOT"

if [ ! -d "$APP_DIR/.git" ]; then
  echo "Clone the repository into $APP_DIR before running this script to completion."
  mkdir -p "$APP_DIR"
fi

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"
PLAYWRIGHT_BROWSERS_PATH="$DATA_ROOT/playwright-browsers" "$VENV_DIR/bin/python" -m playwright install chromium

if [ ! -f "$ENV_FILE" ]; then
  sudo cp "$APP_DIR/deploy/aws/yourbarmate-suppliers.env.example" "$ENV_FILE"
  sudo chown root:root "$ENV_FILE"
  sudo chmod 600 "$ENV_FILE"
  echo "Created $ENV_FILE from template. Fill in real secrets before starting the service."
fi

sudo cp "$APP_DIR/deploy/aws/yourbarmate-suppliers.service" /etc/systemd/system/yourbarmate-suppliers.service
sudo cp "$APP_DIR/deploy/aws/Caddyfile" /etc/caddy/Caddyfile

sudo systemctl daemon-reload
sudo systemctl enable yourbarmate-suppliers.service
sudo systemctl enable caddy

echo "Bootstrap complete."
echo "Next steps:"
echo "1. Edit $ENV_FILE with real secrets."
echo "2. Update /etc/caddy/Caddyfile with your real domain."
echo "3. Point DNS to this EC2 instance."
echo "4. Start services: sudo systemctl restart caddy yourbarmate-suppliers"

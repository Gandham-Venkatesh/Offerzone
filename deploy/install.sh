#!/usr/bin/env bash
# One-time setup on a small always-on Linux server (Ubuntu / Debian / Oracle Linux 9).
# Installs the monitor as a service that starts on boot and restarts itself if it stops.
#
#   git clone <your repo> loot-monitor && cd loot-monitor
#   bash deploy/install.sh
#
# Safe to run again after a `git pull`: it only refreshes packages and restarts the service.
set -euo pipefail

cd "$(dirname "$0")/.."
APP_DIR="$(pwd)"
RUN_AS="${SUDO_USER:-$(id -un)}"
SERVICE=/etc/systemd/system/lootwatch.service

if [ "$(id -u)" -eq 0 ] && [ -z "${SUDO_USER:-}" ]; then
  echo "Run this as your normal user (it uses sudo only where needed), not as root." >&2
  exit 1
fi

echo "==> Installing Python"
if command -v apt-get >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3 python3-venv python3-pip
elif command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y -q python3 python3-pip
else
  echo "Unknown package manager: install python3 (3.9 or newer) with venv support yourself, then re-run." >&2
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Python 3.9 or newer is needed; this machine has $(python3 --version 2>&1)." >&2
  exit 1
fi

echo "==> Installing the monitor into $APP_DIR/.venv"
python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
.venv/bin/pip install --quiet -r requirements-optional.txt \
  || echo "    (optional speed-up packages could not be installed; continuing without them)"

if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "==> Created .env from the template"
fi

echo "==> Installing the service"
sudo tee "$SERVICE" >/dev/null <<EOF
[Unit]
Description=Loot monitor (price-error alerts through ntfy)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m lootwatch
Restart=always
RestartSec=15
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload

if grep -Eq '^NTFY_TOPIC=[^[:space:]#]+' .env; then
  sudo systemctl enable lootwatch >/dev/null 2>&1
  sudo systemctl restart lootwatch
  echo "==> Running. Useful commands:"
  echo "    sudo journalctl -u lootwatch -f          # live log"
  echo "    .venv/bin/python -m lootwatch --doctor   # which sources work from this server"
  echo "    sudo systemctl restart lootwatch         # after editing .env or the .txt lists"
else
  echo
  echo "==> Almost done. Put your ntfy topic into $APP_DIR/.env (the NTFY_TOPIC= line), then:"
  echo "    .venv/bin/python -m lootwatch --test-ntfy     # your phone should ring"
  echo "    .venv/bin/python -m lootwatch --doctor        # which sources work from this server"
  echo "    sudo systemctl enable --now lootwatch         # start it and keep it running"
fi

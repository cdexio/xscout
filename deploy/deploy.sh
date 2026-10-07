#!/usr/bin/env bash
# Deploy xscout to the VPS from this machine: push, pull there, sync deps, migrate, restart, health check.
# First-time setup (user, database, .env, accounts) is described in README "Deploy".
set -euo pipefail

HOST="${XSCOUT_DEPLOY_HOST:-root@46.250.236.190}"
KEY="${XSCOUT_DEPLOY_KEY:-$HOME/.ssh/vps-contabo}"
SSH=(ssh -i "$KEY" -o BatchMode=yes "$HOST")

cd "$(dirname "$0")/.."
if [[ -n "$(git status --porcelain)" ]]; then
  echo "working tree not clean; commit first" >&2
  exit 1
fi
git push origin main

"${SSH[@]}" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/xscout
git pull --ff-only
install -d -o xscout -g xscout -m 750 /opt/xscout/logs
sudo -u xscout -H env UV_PROJECT_ENVIRONMENT=/var/lib/xscout/venv \
  /usr/local/bin/uv sync --frozen --no-dev --python 3.14
sudo -u xscout -H /var/lib/xscout/venv/bin/alembic upgrade head
install -m 644 deploy/xscout.service /etc/systemd/system/xscout.service
install -m 644 deploy/xscout-watchdog.service /etc/systemd/system/xscout-watchdog.service
install -m 644 deploy/xscout-watchdog.timer /etc/systemd/system/xscout-watchdog.timer
install -m 755 deploy/xscout /usr/local/bin/xscout
install -m 755 deploy/xscout-watchdog /usr/local/bin/xscout-watchdog
systemctl daemon-reload
# Start on boot (it was installed but never enabled until 2026-10-07), and watch /health.
systemctl enable xscout xscout-watchdog.timer
systemctl restart xscout
systemctl restart xscout-watchdog.timer
for i in $(seq 1 30); do
  curl -sf http://127.0.0.1:8791/health >/dev/null && break
  sleep 1
done
curl -s http://127.0.0.1:8791/health | head -c 400; echo
git log --oneline -1
REMOTE

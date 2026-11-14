#!/usr/bin/env bash
# Überträgt den aktuellen Stand vom Mac auf den Server und startet die App neu.
#   deploy/deploy.sh root@<server-ip>
# Beim ersten Mal danach auf dem Server setup.sh ausführen (siehe deploy/README.md).
set -euo pipefail

TARGET=${1:?"Ziel fehlt, z. B. root@203.0.113.5"}
APP_DIR=/opt/crypto-dashboard
cd "$(dirname "$0")/.."

if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  echo "Hinweis: Es gibt nicht committete Änderungen – übertragen wird der Stand der Dateien, nicht der letzte Commit."
fi

ssh "$TARGET" "mkdir -p $APP_DIR"
rsync -az --delete \
  --exclude .git --exclude .DS_Store --exclude __pycache__ --exclude '*.pyc' \
  --exclude .claude --exclude .env --exclude .venv \
  ./ "$TARGET:$APP_DIR/"

# Neustart nur, wenn die App schon eingerichtet ist (beim ersten Mal übernimmt das setup.sh)
ssh "$TARGET" "if test -f /etc/systemd/system/crypto-dashboard.service && id -u dashboard >/dev/null 2>&1; then
  chown -R dashboard:dashboard $APP_DIR && systemctl restart crypto-dashboard && systemctl is-active crypto-dashboard
else
  echo 'Noch nicht eingerichtet: jetzt auf dem Server bash $APP_DIR/deploy/setup.sh <domain> <benutzer> ausführen.'
fi"

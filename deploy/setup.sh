#!/usr/bin/env bash
# Einmalige Einrichtung auf dem Server (Ubuntu 24.04 oder Debian 12), als root:
#   bash /opt/crypto-dashboard/deploy/setup.sh <domain> [benutzername]
# Beispiel: bash /opt/crypto-dashboard/deploy/setup.sh 203-0-113-5.sslip.io markus
# Fragt das Passwort für den Zugang ab. Kann gefahrlos erneut ausgeführt werden
# (z. B. um Domain oder Passwort zu ändern).
set -euo pipefail

DOMAIN=${1:?"Domain fehlt, z. B. 203-0-113-5.sslip.io"}
AUTH_USER=${2:-admin}
APP_DIR=/opt/crypto-dashboard

if [[ $EUID -ne 0 ]]; then echo "Bitte als root ausführen." >&2; exit 1; fi
if [[ ! -f "$APP_DIR/server.py" ]]; then
  echo "$APP_DIR/server.py fehlt – zuerst mit deploy/deploy.sh vom Mac übertragen." >&2; exit 1
fi

echo "== Pakete"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 curl gpg debian-keyring debian-archive-keyring apt-transport-https ufw unattended-upgrades

if ! command -v caddy >/dev/null; then
  echo "== Caddy (offizielles Paket-Repository)"
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi

echo "== Benutzer und App"
id -u dashboard >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin dashboard
chown -R dashboard:dashboard "$APP_DIR"
install -m 644 "$APP_DIR/deploy/crypto-dashboard.service" /etc/systemd/system/crypto-dashboard.service
systemctl daemon-reload
systemctl enable --now crypto-dashboard
systemctl restart crypto-dashboard

echo "== Zugang für $AUTH_USER"
HASH=$(caddy hash-password)   # fragt das Passwort zweimal ab
python3 - "$DOMAIN" "$AUTH_USER" "$HASH" "$APP_DIR/deploy/Caddyfile.template" <<'EOF'
import sys
domain, user, pw_hash, template = sys.argv[1:5]
text = open(template).read()
text = text.replace("__DOMAIN__", domain).replace("__USER__", user).replace("__HASH__", pw_hash)
open("/etc/caddy/Caddyfile", "w").write(text)
EOF
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
systemctl enable caddy
systemctl restart caddy

echo "== Firewall: nur SSH, HTTP (für das Zertifikat) und HTTPS"
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "== Automatische Sicherheitsupdates"
dpkg-reconfigure -f noninteractive unattended-upgrades

echo
echo "Fertig. Das Dashboard ist gleich unter https://$DOMAIN erreichbar (Zertifikat kann eine Minute dauern)."
echo "Status: systemctl status crypto-dashboard caddy · Log: journalctl -u crypto-dashboard -f"

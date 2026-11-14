# Deployment auf einen eigenen Server

Das Dashboard läuft auf einem kleinen Linux-Server, ist per HTTPS erreichbar und mit
Benutzername und Passwort geschützt.

```
Browser ──HTTPS──▶ Caddy (Port 443, Zertifikat, Passwort) ──▶ server.py (nur 127.0.0.1:8000)
```

- `crypto-dashboard.service`: systemd-Dienst, startet `server.py` beim Booten und nach Abstürzen neu
- `Caddyfile.template`: HTTPS mit automatischem Let's-Encrypt-Zertifikat und Basic Auth
- `setup.sh`: einmalige Einrichtung auf dem Server (Pakete, Dienst, Caddy, Firewall, Updates)
- `deploy.sh`: überträgt den Stand vom Mac und startet die App neu

## 1. Server anlegen (einmalig)

1. Konto bei einem Anbieter anlegen, z. B. Hetzner Cloud.
2. Server erstellen:
   - Standort **in der EU**, z. B. Falkenstein oder Nürnberg. Binance sperrt Anfragen von US-Servern.
   - Image **Ubuntu 24.04**, kleinster Typ (2 vCPU / 4 GB reicht locker)
   - SSH-Key hinterlegen. Hast du keinen, auf dem Mac `ssh-keygen -t ed25519` ausführen und den
     Inhalt von `~/.ssh/id_ed25519.pub` beim Anbieter eintragen.
3. Die IPv4-Adresse des Servers notieren, z. B. `203.0.113.5`.

## 2. Domain

Ohne eigene Domain geht es über **sslip.io**: Die Adresse `203-0-113-5.sslip.io` zeigt automatisch
auf die IP `203.0.113.5`. Caddy bekommt dafür ein gültiges HTTPS-Zertifikat.

Mit eigener Domain beim Domain-Anbieter einen A-Record auf die Server-IP setzen und diese Domain
verwenden.

## 3. Übertragen und einrichten

Auf dem Mac im Projektordner:

```bash
deploy/deploy.sh root@203.0.113.5
ssh root@203.0.113.5
bash /opt/crypto-dashboard/deploy/setup.sh 203-0-113-5.sslip.io markus
```

`setup.sh` fragt das Passwort für den Zugang ab. Danach ist das Dashboard unter
`https://203-0-113-5.sslip.io` erreichbar.

## Aktualisieren

Nach Änderungen auf dem Mac:

```bash
deploy/deploy.sh root@203.0.113.5
```

Passwort oder Domain ändern: `setup.sh` einfach erneut ausführen.

## Nützliche Befehle auf dem Server

```bash
systemctl status crypto-dashboard caddy   # läuft alles?
journalctl -u crypto-dashboard -f         # Log der App (Warnungen, API-Fehler)
journalctl -u caddy -f                    # Log von Caddy (Zertifikat, Zugriffe)
```

## Hinweise

- **CoinGecko:** Von Rechenzentrums-IPs ist das kostenlose Limit oft noch knapper als zu Hause.
  Häufen sich die Hinweise „CoinGecko-Limit erreicht“, hilft ein kostenloser Demo-Key.
- Die App speichert nichts auf der Platte. Nach einem Neustart sind die Caches leer, und die
  ersten Aufrufe dauern etwas länger.
- Das Passwort schützt den Zugang. Die App selbst hat keine Benutzerverwaltung, daher nicht ohne
  Caddy davor ins Netz stellen. Sie lauscht ohnehin nur auf `127.0.0.1`.

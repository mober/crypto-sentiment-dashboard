# Deployment auf einen eigenen Server

Das Dashboard läuft auf einem kleinen Linux-Server und ist per HTTPS erreichbar, wahlweise
öffentlich oder mit Benutzername und Passwort geschützt.

```
Browser ──HTTPS──▶ Caddy (Port 443, Zertifikat, ggf. Passwort) ──▶ server.py (nur 127.0.0.1:8000)
```

- `crypto-dashboard.service`: systemd-Dienst, startet `server.py` beim Booten und nach Abstürzen neu
- `Caddyfile.template`: HTTPS mit automatischem Let's-Encrypt-Zertifikat, optional Basic Auth
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

Mehrere Domains gehen mit Komma getrennt (ohne Leerzeichen), z. B.
`setup.sh beispiel.de,maxheadroom23.org`. Jede braucht einen A-Record auf die Server-IP; Caddy holt
für jede ein eigenes Zertifikat.

## 3. Übertragen und einrichten

Auf dem Mac im Projektordner:

```bash
deploy/deploy.sh root@203.0.113.5
ssh root@203.0.113.5
bash /opt/crypto-dashboard/deploy/setup.sh 203-0-113-5.sslip.io          # öffentlich
bash /opt/crypto-dashboard/deploy/setup.sh 203-0-113-5.sslip.io markus   # mit Anmeldung
```

Mit Benutzername fragt `setup.sh` das Passwort ab, und der Browser verlangt beim Aufruf eine
Anmeldung. Ohne Benutzername ist das Dashboard für alle erreichbar. Danach läuft es unter
`https://203-0-113-5.sslip.io`.

## Aktualisieren

Nach Änderungen auf dem Mac:

```bash
deploy/deploy.sh root@203.0.113.5
```

Passwort, Domain oder Anmeldung ändern: `setup.sh` einfach erneut ausführen (mit oder ohne Benutzername).

## Unter einem Unterpfad (z. B. `/trading/`)

Auf maxheadroom23.org läuft das Dashboard neben anderen Seiten unter `/trading/`. Dieser Teil steht
nicht in `Caddyfile.template`, sondern ist von Hand in `/etc/caddy/Caddyfile` eingetragen (das
Frontend nutzt relative Pfade, deshalb reicht `handle_path`):

```
redir /trading /trading/ 308
handle_path /trading/* {
	reverse_proxy 127.0.0.1:8000
}

# Alte Adressen ohne /trading/ weiterleiten
@altdashboard path /coin.html /markets.html /sectors.html /about.html /methodology.html /liquidations.html /alerts.html
redir @altdashboard /trading{uri} 308
handle /api/* {
	reverse_proxy 127.0.0.1:8000
}
```

**Neue Seite in `web/` → auch in `@altdashboard` eintragen**, sonst gibt die alte Adresse 404.
Danach `caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy`.

## Nützliche Befehle auf dem Server

```bash
systemctl status crypto-dashboard caddy   # läuft alles?
journalctl -u crypto-dashboard -f         # Log der App (Warnungen, API-Fehler)
journalctl -u caddy -f                    # Log von Caddy (Zertifikat, Zugriffe)
```

## Hinweise

- **CoinGecko:** Von Rechenzentrums-IPs ist das kostenlose Limit oft noch knapper als zu Hause.
  Häufen sich die Hinweise „CoinGecko-Limit erreicht“, hilft ein kostenloser Demo-Key.
- Dauerhaft gespeichert werden nur Liquidationen und Alarme in `data/` (wird von `deploy.sh` nicht
  überschrieben). Nach einem Neustart sind die Caches leer, und die ersten Aufrufe dauern etwas länger.
- Die App selbst hat keine Benutzerverwaltung; eine Anmeldung gibt es nur über Caddy. Öffentlich
  erreichbar teilen sich alle Besucher das CoinGecko-Limit des Servers. Die App nicht ohne Caddy
  davor ins Netz stellen; sie lauscht ohnehin nur auf `127.0.0.1`.

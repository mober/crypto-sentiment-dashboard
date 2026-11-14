# Crypto-Sentiment-Dashboard

Lokales Dashboard, das aus kostenlosen öffentlichen Marktdaten (ohne API-Keys) einen
Überhitzungs-Score je Coin berechnet. Nur Python-Standardbibliothek, kein Build-Schritt.

## Starten

- `python3 server.py` → http://localhost:8000 (öffnet den Browser; `--no-browser`, `--port`)
- `start.command` per Doppelklick im Finder macht dasselbe
- `python3 sentiment.py --symbol ETH` → Bericht im Terminal (`--json` für Rohdaten)

## Aufbau

- `sentiment.py` – Datenabfragen, Score-Skalen und Bewertung (auch als CLI nutzbar)
- `server.py` – HTTP-Server mit Cache (5 Min.) und den Endpoints `/api/sentiment`,
  `/api/coin?symbol=…&id=…`, `/api/markets?page=…`, `/api/klines?symbol=…&interval=1d|4h|1h`
  (Binance-Spot-Kerzen, Cache 60 s); liefert `web/` statisch aus
- `web/index.html` – Dashboard, `web/markets.html` – Rangliste (Top 1000),
  `web/coin.html?symbol=…&id=…` – Detailseite je Coin (Button „Details“ auf jeder Kachel)
  mit Candlestick-Chart, `web/base.css` – gemeinsame Styles und Farbvariablen (hell/dunkel)
- Das Chart nutzt TradingView Lightweight Charts **4.2.0** per CDN (unpkg, feste Version).
  API von v4 (`addCandlestickSeries`); v5 hat eine andere API.

## Score-Logik

- Jedes Signal rechnet seinen Messwert über Ankerpunkte (`*_SCALE` oben in `sentiment.py`)
  linear in −100 … +100 um; der Score ist der **Durchschnitt** aller vorhandenen Signale,
  damit Coins mit unterschiedlich vielen Signalen vergleichbar bleiben.
- DVOL gibt es nur für BTC und ETH.
- Fehlen Binance-Futures oder -Tageskerzen, kommen Ersatz-Signale aus CoinGecko
  (Momentum 7T/30T, Umschlag, RSI auf Stundenbasis).

## Fallstricke

- **Nach Änderungen an `.py`-Dateien den Server neu starten** – er hält die Module im
  Speicher. HTML/CSS wird ohne Neustart ausgeliefert (`Cache-Control: no-cache`).
- **CoinGecko ohne API-Key ist stark limitiert (HTTP 429).** Abfragen bündeln statt pro Coin
  einzeln abzufragen; `fetch_json` wiederholt bei 429, und der Server behält bei Fehlern den
  letzten erfolgreichen Wert. Neue Features nicht mit zusätzlichen CoinGecko-Abfragen pro
  Seitenaufruf bauen.
- **Skalen stehen an zwei Stellen:** Ändert sich ein Ankerpunkt in `sentiment.py`, auch den
  Tooltip-Text im Objekt `TIPS` in `web/index.html` anpassen. Neue Signale brauchen dort
  einen Eintrag in `TIPS` und `SIGNAL_TIPS`.
- Symbole sind nicht eindeutig: Für Einzelabfragen bei CoinGecko die Coin-ID verwenden,
  wenn sie bekannt ist (Trending und Marktübersicht liefern sie mit).
- Es gibt kein Node.js; JS-Syntax lässt sich mit `osascript -l JavaScript` prüfen.

## Arbeitsweise

- Oberfläche und Texte auf Deutsch, Commit-Nachrichten auf Englisch.
- Änderungen erst im Browser prüfen lassen, **committen nur auf ausdrückliche Anweisung**.
- Keine Anlageberatung: Schwellenwerte sind Erfahrungswerte und so zu kennzeichnen.

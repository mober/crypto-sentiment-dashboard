# Crypto-Sentiment-Dashboard

Lokales Dashboard, das aus kostenlosen öffentlichen Marktdaten (ohne API-Keys) einen
Überhitzungs-Score je Coin berechnet. Nur Python-Standardbibliothek, kein Build-Schritt.

## Starten

- `python3 server.py` → http://localhost:8000 (öffnet den Browser; `--no-browser`, `--port`)
- `./start.sh` im Terminal (gleiche Optionen, prüft vorher, ob der Port frei ist) oder
  `start.command` per Doppelklick im Finder macht dasselbe
- `python3 sentiment.py --symbol ETH` → Bericht im Terminal (`--json` für Rohdaten)

- Deployment auf einen eigenen Server (HTTPS über Caddy, Passwort optional, systemd): `deploy/README.md`,
  Aktualisieren mit `deploy/deploy.sh root@<server-ip>`

## Aufbau

- `sentiment.py` – Datenabfragen, Score-Skalen und Bewertung (auch als CLI nutzbar)
- `server.py` – HTTP-Server mit Cache (5 Min.) und den Endpoints `/api/sentiment`,
  `/api/coin?symbol=…&id=…`, `/api/markets?page=…` (oder `?category=…`: größte 100 einer Sparte),
  `/api/categories` (alle CoinGecko-Sparten, eine Abfrage), `/api/defi` (TVL je Kategorie und
  Chain von DefiLlama, aus `/protocols` summiert, Chain-Stände aus `/v2/chains`, Cache 10 Min.), `/api/liquidations?symbol=…`
  (Futures-Kerzen und OI je Stunde, ~21 Tage, für die Liquidations-Heatmap; das Modell selbst
  rechnet `buildLiqModel` in `coin.html`), `/api/fees?symbol=…&id=…` (Gebühren, Einnahmen und
  Weitergabe an Token-Halter von DefiLlama; Coin → Protokoll/Chain über `get_llama_index`,
  Vorrang Dachprotokoll > Chain > Einzelprotokoll, Cache 30 Min., Index 1 Tag), `/api/klines?symbol=…&interval=1d|4h|1h|15m|5m|3m|1m`
  (Binance-Kerzen, Cache 60 s), `/api/derivatives?symbol=…` (Funding 90 T, Open Interest 30 T
  von Binance Futures, Cache 5 Min.), `/api/score-history?symbol=…` (Score je Tag, rückwirkend
  aus Tageswerten mit `score()` berechnet, 1 Jahr), `/api/news?symbol=…&name=…` (deutschsprachige Schlagzeilen aus den RSS-Feeds `NEWS_FEEDS`, nach Symbol/Name
  gefiltert in `match_news`, Feeds gemeinsam 15 Min. gecacht; Anzeige im Kopf der Detailseite),
  `/api/search?q=…` (CoinGecko-Suche plus
  Marktdaten der Treffer, 2 Abfragen je Suchbegriff, Cache 5 Min.); liefert `web/` statisch aus
- `web/index.html` – Dashboard, `web/markets.html` – Rangliste (Top 1000, Suche, Sparten-Filter),
  `web/sectors.html` – Sparten: Rotation der Hauptsparten in 24 h (Liste `MAIN` mit deutschen
  Namen und CoinGecko-IDs), DeFi-TVL (Kategorien/Chains, 24 h/7 T) und Tabelle aller Sparten
  (TVL-Spalte über `CG_TO_DEFI`),
  `web/coin.html?symbol=…&id=…` – Detailseite je Coin (Button „Details“ auf jeder Kachel)
  mit Kennzahlen (Allzeithoch, Menge, FDV …; Felder `market_stats` aus der ohnehin laufenden
  CoinGecko-Abfrage `coins/markets`, keine eigene Abfrage), Candlestick-Chart (MA50/MA200, RSI-Fenster als zweites, synchronisiertes Chart),
  Funding-/OI-Charts und Zusammensetzung des Scores (Balken je Signal; Fear & Greed steht
  bewusst nur dort und im Tacho, nicht in der Signalliste der Kacheln), `web/base.css` – gemeinsame Styles und Farbvariablen (hell/dunkel),
  `web/tips.js` – gemeinsame Tooltip-Texte und -Logik, `web/format.js` – gemeinsame Hilfsfunktionen
  (`$`, `esc`, Zahlenformate `pct`, `change`, `usd`, `usd6` …) für alle vier Seiten; Formate dort ändern,
  nicht in den Seiten
- Live-Kurs im Chart der Detailseite: `coin.html` verbindet sich direkt per WebSocket mit Binance
  (`connectLive`, Spot oder Futures je nach Quelle der Kerzen) und zieht die laufende Kerze samt
  MA/RSI und Kurs im Kopf nach; der Server ist nicht beteiligt. Rückfall ist `refreshChart` (60 s).
- Das Chart nutzt TradingView Lightweight Charts **4.2.0** per CDN (unpkg, feste Version).
  API von v4 (`addCandlestickSeries`); v5 hat eine andere API.

## Score-Logik

- Jedes Signal rechnet seinen Messwert über Ankerpunkte (`*_SCALE` oben in `sentiment.py`)
  linear in −100 … +100 um; der Score ist der **Durchschnitt** aller vorhandenen Signale,
  damit Coins mit unterschiedlich vielen Signalen vergleichbar bleiben.
- DVOL gibt es nur für BTC und ETH.
- Funding wird auf 8 h umgerechnet (`normalize_funding`), da manche Coins alle 4 h oder 1 h zahlen;
  `FUNDING_SCALE` gilt pro 8 h.
- Kerzen (Chart und MA200/RSI) kommen von Binance Spot; fehlt das Spot-Paar oder ist die
  Spot-Historie kürzer, von Binance Futures (Perpetual), siehe `get_binance_klines`. Der MA200
  wird nur bei mindestens 200 Tageskerzen berechnet, sonst `price_ma200 = None` (kein MA52 o. ä.).
- Fehlen Binance-Futures oder -Tageskerzen, kommen Ersatz-Signale aus CoinGecko
  (Momentum 7T/30T, Umschlag, RSI auf Stundenbasis).

- Lagebild im Kopf der Detailseite: `situation()` in `sentiment.py` ordnet Stimmung (Score ab
  ±`VERDICT_WARM`) × Trend (Kurs zum MA200, sonst 30-T-Änderung) in ein Feld der Tabelle `SITUATIONS`
  ein und sortiert ausgeprägte Signale (ab ±`SIGNAL_STRONG`) in „spricht dafür / dagegen“
  (antizyklisch gelesen). Dazu eine Handlungsempfehlung je Feld aus `ACTIONS` (bei Score ab
  ±`VERDICT_HOT` Varianten aus `ACTIONS_EXTREME`), Felder `action`, `action_tone`
  (buy/hold/sell/wait), `action_text`; Tooltips `situation` und `action`.

## Fallstricke

- **Nach Änderungen an `.py`-Dateien den Server neu starten** – er hält die Module im
  Speicher. HTML/CSS wird ohne Neustart ausgeliefert (`Cache-Control: no-cache`).
- **CoinGecko ohne API-Key ist stark limitiert (HTTP 429).** Abfragen bündeln statt pro Coin
  einzeln abzufragen; der Server behält bei Fehlern den letzten erfolgreichen Wert (bei 429
  wiederholt `fetch_json` nur für die anderen Quellen, für CoinGecko siehe unten). Neue Features nicht mit zusätzlichen CoinGecko-Abfragen pro
  Seitenaufruf bauen.
- **Skalen stehen an zwei Stellen:** Ändert sich ein Ankerpunkt in `sentiment.py`, auch den
  Tooltip-Text im Objekt `TIPS` in `web/tips.js` anpassen (gemeinsam für Dashboard und Detailseite). Neue Signale brauchen dort
  einen Eintrag in `TIPS` und `SIGNAL_TIPS`.
- Symbole sind nicht eindeutig: Für Einzelabfragen bei CoinGecko die Coin-ID verwenden,
  wenn sie bekannt ist (Trending und Marktübersicht liefern sie mit).
- **CoinGecko-Abfragen laufen nacheinander** (`fetch_json`, Mindestabstand `COINGECKO_MIN_INTERVAL`).
  Nach HTTP 429 pausieren alle CoinGecko-Abfragen; währenddessen bricht `fetch_json` sofort mit
  `RateLimited` ab, der Server antwortet 502 mit `retry_in`, und die Seiten laden danach selbst neu.
- Die Liquidations-Heatmap ist ein **Modell** (OI-Zuwachs × angenommene Hebel), keine echten
  Aufträge; sie liegt als Canvas unter einem transparenten Lightweight-Chart und wird bei
  Bereichsänderungen neu gezeichnet. Die Preisachse ist dort nicht von Hand skalierbar.
- Es gibt kein Node.js; JS-Syntax lässt sich mit `osascript -l JavaScript` prüfen.

## Arbeitsweise

- Oberfläche und Texte auf Deutsch, Commit-Nachrichten auf Englisch.
- Änderungen erst im Browser prüfen lassen, **committen nur auf ausdrückliche Anweisung**.

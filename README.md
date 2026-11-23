# Crypto-Sentiment-Dashboard

Ein lokales Dashboard, das aus kostenlosen öffentlichen Marktdaten einschätzt, wie „heiß“ der
Kryptomarkt gerade ist – je Coin ein **Stimmungs-Score** von −100 (extreme Angst) bis +100
(Überhitzung), dazu Kurs-Charts, Derivate, echte Liquidationen, Orderbuch, On-Chain-Daten und News.

- **Keine API-Keys, keine Konten:** alle Quellen sind öffentlich und kostenlos.
- **Keine Abhängigkeiten:** nur die Python-Standardbibliothek, kein Build-Schritt, kein Node.js.
- **Deutsche Oberfläche** mit Erklärungen zu jeder Kennzahl (Tooltips).

> Kein Anlageberater. Score, Lagebild und Empfehlungen sind Einordnungen aus Erfahrungswerten und
> einem Rückblick auf vergangene Daten – keine Prognose und keine Aufforderung zum Kauf oder Verkauf.

## Schnellstart

Voraussetzung: Python 3.9 oder neuer.

```bash
python3 server.py            # → http://localhost:8000, öffnet den Browser
python3 server.py --port 8080 --no-browser
./start.sh                   # dasselbe, prüft vorher, ob der Port frei ist
```

Auf dem Mac startet `start.command` das Dashboard per Doppelklick im Finder.

Bericht im Terminal ohne Server:

```bash
python3 sentiment.py --symbol ETH          # Score und Signale als Text
python3 sentiment.py --symbol ETH --json   # Rohdaten
python3 liquidations.py --seconds 60       # Liquidationen live mitschneiden (Test)
```

## Seiten

| Seite | Inhalt |
|---|---|
| **Dashboard** (`/`) | Score für BTC, ETH und SOL mit Tacho und Signalen, Fear & Greed, Stablecoins, BTC-Dominanz, Trending bzw. Top 20 – jeder Coin lässt sich als zusätzliche Kachel einblenden |
| **Detailseite** (`coin.html?symbol=BTC`) | Lagebild mit Empfehlung, Trefferquote des Scores, Kennzahlen, Candlestick-Chart mit MA50/MA200, RSI und Live-Kurs, Orderbuch live, Gebühren & Einnahmen, Funding und Open Interest, On-Chain (Börsenflüsse, MVRV), Liquidations-Heatmap (Modell), Score-Verlauf, Zusammensetzung des Scores, News |
| **Profil** (`about.html?symbol=BTC`) | Was ist der Coin? Art, Kategorien, Risikostufe mit Stärken und Risiken, Fakten und Links |
| **Marktübersicht** (`markets.html`) | Top 1000 nach Marktkapitalisierung, Suche, Filter nach Sparte |
| **Sparten** (`sectors.html`) | Rotation der Hauptsparten, DeFi-TVL nach Kategorie und Chain |
| **Liquidationen** (`liquidations.html`) | Echte Zwangsschließungen von Binance, Bybit und OKX: Heatmap je Coin, Summen 1 h–24 h, Long/Short, größte Order, Live-Feed und Verlauf über Monate |

## Der Score

Jedes Signal wird über Ankerpunkte linear auf −100 … +100 umgerechnet, der Score ist der
**gewichtete Durchschnitt** der vorhandenen Signale:

| Signal | Quelle | Gewicht |
|---|---|---|
| Preis vs. MA200 | Binance | 1,5 |
| Optionsvola DVOL (nur BTC/ETH) | Deribit | 1,5 |
| MVRV (nicht für alle Coins) | Coin Metrics | 1 |
| Funding Rate (auf 8 h umgerechnet) | Binance Futures | 1 |
| Open Interest (Änderung 7 Tage) | Binance Futures | 1 |
| Long/Short Ratio | Binance Futures | 1 |
| Fear & Greed | alternative.me | 0,5 |
| RSI (14) | Binance | 0,5 |

Fehlen Binance-Daten, springen Ersatz-Signale aus CoinGecko ein (Momentum 7/30 Tage, Umschlag,
RSI auf Stundenbasis).

Die Gewichte stammen aus einem **Backtest** (BTC, ETH, SOL ab 04/2024; MVRV über mehrere Zyklen
ab 2011): Der Score wurde rückwirkend berechnet und mit der Kursentwicklung 30 und 90 Tage danach
verglichen. Ergebnis in Kürze: Werte ab +40 („überhitzt“) gingen deutlich schlechteren Kursen voraus,
extreme Angst allein war dagegen kein verlässliches Kaufsignal – außer bei niedrigem MVRV. Die
Detailseite zeigt dazu je Coin die **Trefferquote** der aktuellen Score-Zone.

## Datenquellen

| Quelle | Wofür |
|---|---|
| [Binance](https://www.binance.com) (Spot und Futures) | Kerzen, Live-Kurs, Funding, Open Interest, Long/Short, Orderbuch, Liquidationen |
| [Bybit](https://www.bybit.com), [OKX](https://www.okx.com) | Liquidationen (WebSocket) |
| [CoinGecko](https://www.coingecko.com) | Marktkapitalisierung, Rangliste, Sparten, Trending, Profile |
| [alternative.me](https://alternative.me/crypto/fear-and-greed-index/) | Fear & Greed Index |
| [Deribit](https://www.deribit.com) | Optionsvolatilität DVOL |
| [DefiLlama](https://defillama.com) | Stablecoins, TVL, Gebühren und Einnahmen |
| [Coin Metrics](https://coinmetrics.io) (Community-API) | Börsenzu- und -abflüsse, Bestand auf Börsen, MVRV |
| RSS-Feeds deutschsprachiger Krypto-Medien | News zum Coin |

CoinGecko ist ohne Key stark limitiert. Der Server bündelt die Abfragen, hält Ergebnisse im Cache
und zeigt bei einer Limit-Pause einen Countdown, nach dem die Seite selbst neu lädt.

## Aufbau

```
server.py          HTTP-Server, Caches (TTLCache), API-Endpunkte unter /api/…, liefert web/ aus
sentiment.py       Datenabfragen, Score-Skalen, Bewertung, Lagebild, Backtest-Auswertung (auch CLI)
liquidations.py    Liquidationen live per WebSocket (eigener Client), Speicherung in SQLite
web/               Oberfläche: HTML, CSS und JavaScript ohne Build-Schritt
  coin.html        Detailseite; Code je Abschnitt in coin-*.js, Styles in coin.css
  format.js        gemeinsame Zahlenformate
  tips.js          Tooltip-Texte und -Logik
deploy/            Betrieb auf einem eigenen Server (systemd, Caddy, HTTPS)
data/              Liquidations-Datenbank (nicht im Repository)
```

Charts: [TradingView Lightweight Charts](https://www.tradingview.com/lightweight-charts/) 4.2.0 per CDN.

Liquidationen werden dauerhaft in `data/liquidations.db` (SQLite) gespeichert, mit Summen je
Stunde und Tag für schnelle Abfragen über Monate. Die Liquidations-Heatmap auf der Detailseite ist
dagegen ein **Modell** aus Open-Interest-Zuwachs und angenommenen Hebeln, keine echten Aufträge.

## Auf einem Server betreiben

Das Dashboard läuft auch auf einem kleinen Linux-Server, per HTTPS erreichbar und optional mit
Passwort geschützt. Die Schritte stehen in [`deploy/README.md`](deploy/README.md); Aktualisieren geht
danach mit:

```bash
deploy/deploy.sh root@<server>
```

Den Server in der EU wählen – Binance sperrt Anfragen von US-Servern.

## Entwicklung

- Nach Änderungen an `.py`-Dateien den Server neu starten; HTML, CSS und JS werden ohne Neustart
  ausgeliefert.
- Hinweise zu Aufbau, Fallstricken und Konventionen für die Weiterentwicklung stehen in
  [`CLAUDE.md`](CLAUDE.md).

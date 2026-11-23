# Crypto-Sentiment-Dashboard

Lokales Dashboard, das aus kostenlosen öffentlichen Marktdaten (ohne API-Keys) einen
Stimmungs-Score je Coin berechnet. Nur Python-Standardbibliothek, kein Build-Schritt.

## Starten

- `python3 server.py` → http://localhost:8000 (öffnet den Browser; `--no-browser`, `--port`)
- `./start.sh` im Terminal (gleiche Optionen, prüft vorher, ob der Port frei ist) oder
  `start.command` per Doppelklick im Finder macht dasselbe
- `python3 sentiment.py --symbol ETH` → Bericht im Terminal (`--json` für Rohdaten)

- Deployment auf einen eigenen Server (HTTPS über Caddy, Passwort optional, systemd): `deploy/README.md`,
  Aktualisieren mit `deploy/deploy.sh root@<server-ip>`

## Aufbau

- `sentiment.py` – Datenabfragen, Score-Skalen und Bewertung (auch als CLI nutzbar)
- `liquidations.py` – echte Liquidationen live von Binance, Bybit und OKX (eigener WebSocket-Client
  ohne Zusatzpakete, je Börse ein Thread, `Collector` startet in `server.main()`); 24 h im Speicher
  für Übersicht und Feed, dauerhaft in SQLite `data/liquidations.db` (alle 5 s gesammelt geschrieben,
  beim Beenden per SIGTERM ebenfalls). Tabellen: `events` (jede Liquidation, ohne Ablauf), Summentabellen
  `hourly` (Stunde × Börse × Coin), `daily` (UTC-Tag × Börse × Coin), `hourly_ex` (Stunde × Börse), `meta`
  (`since` = Beginn der Aufzeichnung). Eine alte `liquidations.jsonl` wird beim Start einmalig übernommen.
  Endpoints `/api/liq-overview?window=1h|4h|12h|24h` (Summen aller Fenster, Coins und Börsen des Fensters,
  größte Order, Stundenwerte 24 h, Status der Verbindungen), `/api/liq-feed?coin=&exchange=&min=&limit=` und
  `/api/liq-history?days=7|30|90|365&coin=&exchange=` (Stundenwerte, Coins, Börsen, größte Orders aus SQLite,
  Cache 60 s; Abschnitt „Verlauf“ auf der Seite, Balken je Tag bzw. 4 h in Ortszeit, gebildet im Browser).
  Test im Terminal: `python3 liquidations.py --seconds 60`
- `alerts.py` – Alarme: Regeln je Coin (`KINDS`: Score/Kurs über/unter, Kurs kreuzt MA200, Funding über/unter
  pro 8 h, Liquidationen in 1 h über X, auch für alle Coins `*`), SQLite `data/alerts.db` (`rules`, `fired`, `settings`).
  `Checker` prüft alle 5 Min. im Server (erste Prüfung 1 Min. nach Start) mit denselben Coin-Daten wie die Watchlist-Karten
  (`get_watch_coins`, CoinGecko gebündelt) und `Store.snapshot` der Liquidationen. Auslösen nur auf Flanke (Bedingung wird
  wahr), höchstens einmal je `COOLDOWN` (1 h); ohne ntfy-Kanal wird nichts ausgelöst (Meldung bliebe sonst verbraucht).
  Versand per ntfy als JSON-POST an den Server-Root (`send_ntfy`), Texte `TEXTS` de/en nach `settings.lang`.
  Seite `web/alerts.html` (Navigation „Alarme“): Kanal, Test, Regeln, Verlauf. Endpoint `GET /api/alerts`, Änderungen über
  `POST /api/alerts` mit JSON `{action: add|delete|enable|settings|test|check}` (`POST_ROUTES`, `do_POST`; nur
  `application/json`, damit fremde Formulare nichts ändern können). Regeln gelten für alle, die das Dashboard öffnen können.
- `server.py` – HTTP-Server mit Cache (5 Min.) und den Endpoints `/api/sentiment`,
  `/api/coin?symbol=…&id=…`, `/api/markets?page=…` (oder `?category=…`: größte 100 einer Sparte),
  `/api/categories` (alle CoinGecko-Sparten, eine Abfrage), `/api/defi` (TVL je Kategorie und
  Chain von DefiLlama, aus `/protocols` summiert, Chain-Stände aus `/v2/chains`, Cache 10 Min.), `/api/liquidations?symbol=…`
  (Futures-Kerzen und OI je Stunde, ~21 Tage, für die Liquidations-Heatmap; das Modell selbst
  rechnet `buildLiqModel` in `web/coin-liqmap.js`), `/api/fees?symbol=…&id=…` (Gebühren, Einnahmen und
  Weitergabe an Token-Halter von DefiLlama; Coin → Protokoll/Chain über `get_llama_index`,
  Vorrang Dachprotokoll > Chain > Einzelprotokoll, Cache 30 Min., Index 1 Tag), `/api/klines?symbol=…&interval=1d|4h|1h|15m|5m|3m|1m`
  (Binance-Kerzen, Cache 60 s), `/api/derivatives?symbol=…` (Funding 90 T, Open Interest 30 T
  von Binance Futures, Cache 5 Min.), `/api/score-history?symbol=…` (Score je Tag, rückwirkend
  aus Tageswerten mit `score()` berechnet, 1 Jahr), `/api/news?symbol=…&name=…` (deutschsprachige Schlagzeilen aus den RSS-Feeds `NEWS_FEEDS`, nach Symbol/Name
  gefiltert in `match_news`, Feeds gemeinsam 15 Min. gecacht; Anzeige im Kopf der Detailseite),
  `/api/watchlist?ids=…` (Marktdaten der Watchlist-Coins, eine CoinGecko-Abfrage `coins/markets?ids=` für alle,
  höchstens `WATCHLIST_MAX` IDs, Cache 5 Min. je ID-Satz), `/api/watch-coins?coins=BTC:bitcoin,…` (Karten des
  Dashboards: je Coin wie `/api/coin` und im selben `_coin_cache`, aber CoinGecko nur einmal für alle abgelaufenen Coins
  über `get_coingecko_coins`, an `collect_symbol(market=…)` durchgereicht; Binance je Coin parallel, `WATCH_WORKERS`;
  erster Abruf vieler neuer Coins dauert ~20 s), `/api/search?q=…` (CoinGecko-Suche plus
  Marktdaten der Treffer, 2 Abfragen je Suchbegriff, Cache 5 Min.), `/api/coin-info?symbol=…&id=…`
  (Profil für `about.html`: eine CoinGecko-Abfrage `coins/{id}`, Cache 1 Tag; Kategorien deutsch über
  `CATEGORY_DE`, Art über `coin_type`, regelbasierte Risikostufe in `coin_assessment`); liefert `web/` statisch aus
- `web/liquidations.html` – Liquidations-Übersicht im Stil von CoinGlass (Heatmap je Coin/Börse als
  Treemap, Summen 1h–24h, Long/Short, Anteil je Börse, größte Order, Stunden mit BTC-Kurs, Live-Feed)
- `web/nav.js` (direkt nach `i18n.js` laden) – gemeinsame Navigationsleiste oben in `<main>` für alle Seiten
  (Liste `NAV_PAGES`, aktive Seite farbig gefüllt in `--accent` mit `aria-current="page"`, Unterseiten über `section` mit
  `aria-current="true"` – `coin.html`/`about.html` gehören zu „Märkte“; die `<h1>` jeder Hauptseite heißt wie ihr Menüpunkt, Sprachumschalter); bis 640 px klappen die Seiten in ein Menü „☰“.
  Seitenlinks gehören dorthin, nicht in den `<header>` der Seiten (dort nur seitenspezifische Knöpfe).
  Detailseiten setzen darunter Breadcrumbs über `setBreadcrumb(items)` (`.crumbs`): `coin.html` Märkte → Coin → Reiter
  (`setCoinCrumbs` in `coin-tabs.js`), `about.html` Märkte → Coin → Profil.
- `web/index.html` – Dashboard, `web/markets.html` – Rangliste (Top 1000, Suche, Sparten-Filter),
  `web/sectors.html` – Sparten: Rotation der Hauptsparten in 24 h (Liste `MAIN` mit deutschen
  Namen und CoinGecko-IDs), DeFi-TVL (Kategorien/Chains, 24 h/7 T) und Tabelle aller Sparten
  (TVL-Spalte über `CG_TO_DEFI`),
  `web/coin.html?symbol=…&id=…` – Detailseite je Coin (Button „Details“ auf jeder Kachel)
  mit Kennzahlen (Allzeithoch, Menge, FDV …; Felder `market_stats` aus der ohnehin laufenden
  CoinGecko-Abfrage `coins/markets`, keine eigene Abfrage), Candlestick-Chart (Menü „Indikatoren“, gemerkt in `localStorage.chartShow`: MA50/MA200, Bollinger 20/2,
  Bull Market Support Band 20W-SMA/21W-EMA – Perioden je Intervall umgerechnet, nur 1T/4h –, Kursmarken ATH/52W/Monats- und
  Wochen-Open als Price-Lines aus `dailyCandles`/`statsCoin` von `coin-head.js`; unteres Fenster RSI oder MACD 12/26/9 als zweites,
  synchronisiertes Chart; alles im Browser aus den Kerzen in `calcIndicators`, keine eigene Abfrage),
  Funding-/OI-Charts und Zusammensetzung des Scores (Balken je Signal; Fear & Greed steht
  bewusst nur dort und im Tacho, nicht in der Signalliste der Kacheln). `coin.html` enthält nur HTML und
  den Start; Styles in `coin.css`, Code je Abschnitt in `coin-base.js` (gemeinsame Konstanten, `themeOptions`,
  `miniChart`, Kacheln), `coin-head.js` (Kopf, Lagebild, News, Kennzahlen, Score-Balken), `coin-chart.js`
  (Kurs-Chart, RSI, Live-Kurs), `coin-derivs.js`, `coin-score.js`, `coin-liqmap.js`, `coin-fees.js`, `coin-book.js`, `coin-onchain.js`,
  `coin-tabs.js` (Reiter `TABS`: Übersicht, Chart & Technik, Derivate, On-Chain, Liquidationen, Fundamentaldaten,
  Score & Historie; je Reiter ein `.tab-panel` in `coin.html`, neue Karten dort einsortieren; inaktive Reiter nur
  unsichtbar statt `display:none`, damit Charts ihre Breite behalten; Reiter im Hash, `#<Abschnitts-ID>` öffnet den
  passenden Reiter; Reiter ohne sichtbare Abschnitte gedimmt mit Hinweis) –
  normale Skripte ohne Module/Build, alle im selben globalen Bereich (gleiche Namen auf oberster Ebene
  in zwei Dateien → SyntaxError, das ganze zweite Skript fällt aus),
  `web/about.html?symbol=…&id=…` – Profil je Coin (Link „Was ist …?“ oben im Kopf der Detailseite):
  Art und Kategorien mit Erklärung, Risikostufe mit Stärken/Risiken, aktuelle Marktlage, Beschreibung
  (CoinGecko meist nur englisch, Link zum Google Übersetzer), Fakten und Links, `web/base.css` – gemeinsame Styles und Farbvariablen (hell/dunkel),
  `web/tips.js` – gemeinsame Tooltip-Texte und -Logik, `web/watchlist.js` – Watchlist nur im Browser
  (`localStorage.watchlist`, Einträge `{id, symbol, name}`, CoinGecko-ID Pflicht; `watchStar(coin)` liefert den
  Stern-Button, Klick in der Capture-Phase, Ereignis `watchlist` auf `document` bei jeder Änderung, auch aus anderen
  Tabs). BTC, ETH, SOL fest (`WATCH_FIXED`, nicht entfernbar, nicht gespeichert). Das Dashboard zeigt je
  Watchlist-Coin eine Karte (letzter Stand in `localStorage.lastWatchCoins`); Trending/Top 20 sind nur noch eine
  Leiste zum Hinzufügen/Entfernen. Stern auch in der Rangliste und auf der Detailseite; Tabelle `markets.html?watch=1`. Die Coin-ID kommt dafür als `data.coin_id` aus der ohnehin laufenden CoinGecko-Abfrage mit; `web/format.js` – gemeinsame Hilfsfunktionen
  (`$`, `esc`, Zahlenformate `pct`, `change`, `usd`, `usd6` …) für alle Seiten; Formate dort ändern,
  nicht in den Seiten
- Seitenaufbau für Übersicht: oben je Seite eine Kurzfassung „Auf einen Blick“ (`<section class="card brief">`,
  `renderBrief(el, {title, text, action, tone})` in `format.js`, Ton buy/hold/sell/wait/neutral), im Browser aus den ohnehin
  geladenen Daten regelbasiert formuliert: Dashboard (`renderDashBrief`: Fear & Greed, Watchlist-Scores, Empfehlung aus dem
  Lagebild von BTC), Märkte (`renderMarketBrief`: Breite der angezeigten Coins), Sparten (`renderSectorBrief`: Rotation
  gegen Layer 1), Liquidationen (`renderSummary`: Long-/Short-Anteil, Tempo gegen 24-h-Schnitt), Detailseite
  (`renderCoinBrief` aus `situation`, über den Reitern). „Warum wichtig?“-Knöpfe: `whyTip(key)` mit Texten `WHY`/`WHY_EN`
  in `tips.js` (Tooltip-Schlüssel `why:<key>`); in statischem HTML als `<span class="why-slot" data-why="…">`, ersetzt beim
  Start. **Sonst nichts eingeklappt**: alle Karten und Details sind offen (ausdrücklicher Wunsch). Einzige Ausnahme, ebenfalls
  auf Wunsch: Dashboard-Karten ab Funding (`details.more`, offener Zustand je Karte in `localStorage.openCards`).
- `web/methodology.html` (Navigation „Methodik“): Ziel und Grenzen, Berechnung, Signale mit Skalen und Gewichten,
  fehlende/veraltete Daten, Datenquellen und Intervalle, Börsen-Einschränkungen, Rückblick, Änderungsprotokoll. Zahlen
  kommen aus `/api/methodology` (`api_methodology` in `server.py`: `SIGNAL_SCALES`, `SIGNAL_WEIGHTS`, Schwellen,
  Cache-Intervalle), Texte je Signal in `SIG` auf der Seite. **Score-Methodik geändert (Signale, Skalen, Gewichte,
  Schwellen, Einordnung) → `METHODOLOGY_VERSION` erhöhen und Eintrag oben in `METHODOLOGY_CHANGES` (`sentiment.py`)**;
  neues Signal auch in `SIGNAL_SCALES` und `SIG` eintragen.
- Liquidationen „Nur Krypto“ (Standard, `localStorage.liqCryptoOnly`, Parameter `crypto=1` für overview/feed/history):
  Aktien/ETF/Rohstoff-Kontrakte nach Kennzeichnung der Börsen (`load_tradfi` in `liquidations.py`: Binance
  `underlyingType` ≠ COIN, Bybit `symbolType`, OKX `instCategory` ≠ 1), Paare (Börse, Coin), alle 6 h erneuert.
- Marktübersicht ohne eigene Spaltenauswahl: `fitColumns` blendet `opt-wide`, dann `opt-mid` aus, bis die Tabelle passt.
- Binance-Futures für billige Coins heißen `1000PEPEUSDT`, `1MBABYDOGEUSDT`, `1000000MOGUSDT`: alle Futures-Abfragen über
  `futures_pair(symbol)` / `fpair` (`sentiment.py`, Zuordnung aus `exchangeInfo`, täglich); Kurse daraus durch den Faktor teilen
  (OI-Kurs, Liquidations-Modell), USD-Werte, Funding und Long/Short unverändert. Nie mehr `f"{symbol}USDT"` für Futures.
- Einheitliche Wörter: RSI über `rsi_zone`/`rsi_word` (gerundeter Wert, überverkauft < 30, überkauft > 70; Chart in
  `coin-chart.js` gleich), MVRV über `mvrv_word` (Stufen auch in `coin-onchain.js`). Score-Verlauf: heutiger Punkt =
  aktueller Score (`syncTodayScore` in `coin-score.js`).
- Reiter der Detailseite im Hash als `#tab=<key>` (reines `#chart` ließe den Browser zu `div#chart` springen).
  Detailseite/Profil gehören zum Bereich, aus dem man kam (`navOrigin` in `nav.js`, `sessionStorage.navFrom`).
- Sparten: Änderung über ±30 % in 24 h (`JUMP`) gilt als Datensprung (Coin neu einsortiert), nicht in Skala/Kurzfassung.
- Zahlen: `usd6` rundet auf 4 signifikante Stellen (Name aus Kompatibilität), Preisachsen ab 1.000 $ ohne Cent.
  Datenstatus mit `quiet: true` zeigt bei live/aktuell nur Punkt und Quelle.
- Filter der Marktübersicht (`markets.html`, Menü „Filter“, `localStorage.marketFilter`, Chips über der Tabelle): Sortierung
  (Gewinner/Verlierer 24 h/7 T, Volumen, Umschlag, Abstand MA200), Assetklassen, Mindest-Marktkap./-Volumen, Trend zu MA50/MA200,
  Stablecoins ausblenden, nur vollständiger Score, nur Watchlist, Liquiditätswarnungen ausblenden. Aktiver Filter → alle Top 1000
  von `/api/screener` im Browser gefiltert (100 Zeilen je „Mehr anzeigen“, keine Seiten). `get_screener` in `server.py`:
  Top 1000 in `SCREENER_PAGES` × 250 (CoinGecko, 5 Min.), Assetklassen aus CoinGecko-Kategorien `ASSET_CLASSES` (je die 250
  größten, einmal am Tag im Hintergrund, `CLASSES_PAUSE` zwischen den Abfragen, wartet Limit-Pausen ab), MA50/MA200 und
  USDT-Perpetual aus Binance (stündlich im Hintergrund, Kürzel → `<SYMBOL>USDT`, verworfen bei Kursabweichung über
  `MA_PRICE_TOLERANCE`); beides wird je Anfrage frisch dazugemischt (`classes_ready`, `ma_ready`). „Vollständiger Score“ =
  USDT-Perpetual + 200 Tageskerzen. Spalten Vol./MK, Abst. MA50/MA200 (aus, über „Spalten“). Liquiditätswarnung ⚠ im Browser
  (`liqWarnText`: Volumen < 250.000 $, Umschlag < 0,2 % oder > 100 %), Tooltips `liqWarn`, `assetClass`.
- Datenstatus je Karte/Kennzahl (`web/datastatus.js`, nach `format.js` laden): `dataStatus({src, kind, at|day, every, lag,
  connected, note})` bzw. `setDataStatus(id, …)` zeigt „Quelle · Zustand · Stand“; alle `.ds` werden jede Sekunde neu bewertet.
  Zustände live/aktuell/verzögert/veraltet/nicht verfügbar; Arten `live` (WebSocket, `at` = letzte Meldung), `poll`
  (`at` = Abrufzeit, `every` = Takt in s; aktuell bis 2× Takt + 60 s, verzögert bis 4×), `daily` (`day` = letzter Tageswert,
  `lag` = übliche Verzögerung in Tagen). Abrufzeit kommt vom Server: `TTLCache.set` hängt an jedes Dict `fetched_at`
  (Epoch-Sekunden) an, ein gescheitertes Neuladen behält den alten Wert und damit das echte Alter. Fear & Greed hat `time`,
  MVRV `time` (Tagesbeginn UTC). Score-Signale: Quelle je Signal in `SIGNAL_SRC` (`coin-head.js`). Tooltip `TIPS.ds`.
  Alle `updated`-Zeitstempel des Servers tragen die Zeitzone (sonst rechnet der Browser bei anderer Zeitzone falsch).
- Mobil (bis 640 px): Märkte als Karten (`#mtable.cards`, Umschalter Tabelle/Karten `localStorage.marketView`, ohne Wahl
  automatisch nach Breite) und Spaltenauswahl (`MCOLS`, `localStorage.marketCols`, gilt für beide Ansichten; ohne Auswahl
  Tabelle wie bisher über `opt-wide`/`opt-mid`, Karten `CARD_DEFAULT`; Spalten mit `off: true` sind ohne Auswahl aus).
  Liquidations-Feed als Karten. Score-Zusammensetzung: Name | Wert, Balken darunter.
- Live-Kurs im Chart der Detailseite: `coin-chart.js` verbindet sich direkt per WebSocket mit Binance
  (`connectLive`, Spot oder Futures je nach Quelle der Kerzen) und zieht die laufende Kerze samt
  MA/RSI und Kurs im Kopf nach; der Server ist nicht beteiligt. Rückfall ist `refreshChart` (60 s).
- Orderbuch auf der Detailseite (`coin-book.js`, Karte `#book`): ebenfalls direkt im Browser von Binance
  (CORS erlaubt, Gewicht zählt für die IP des Nutzers). Lokales Buch nach Binance-Vorgabe: Diff-Stream
  (`@depth@1000ms` Spot, `@depth@500ms` Futures über `fstream…/public/ws/`) puffern, REST-Momentaufnahme
  (Spot 5.000, Futures 1.000 Stufen je Seite), Update-IDs prüfen (Spot `U == lastU+1`, Futures `pu == lastU`),
  bei Lücke neu synchronisieren; Momentaufnahme alle 5 Min. erneuert. Außerhalb der Momentaufnahme
  (`book.floor`/`book.ceil`) wird nichts gezählt. Spot zuerst; fehlt das Paar oder ist es leer (delistet),
  Futures. Tiefenchart als eigenes Canvas (keine Chart-Bibliothek), Kennzahlen über `statTile`, Tooltips
  `orderbook`, `bookSpread`, `bookImbalance`, `bookWall`.
- On-Chain-Karte (`coin-onchain.js`, Endpoint `/api/onchain?symbol=…`, `get_onchain` in `sentiment.py`, Cache 1 h):
  Coin Metrics Community-API ohne Key. Börsenzu-/abflüsse und Bestand auf Börsen nur BTC/ETH, MVRV für viele
  größere Coins; 2 Jahre Tageswerte. Erst alle Kennzahlen, bei HTTP 400 nur MVRV. **Coin Metrics antwortet auf
  Abfragen, die es länger nicht gab, manchmal erst nach > 40 s** (danach < 1 s): Zeitgrenze `COINMETRICS_TIMEOUT`
  60 s, Caches mit `stale=True`, Server lädt On-Chain und Score-Historie der Haupt-Coins beim Start vor.
- Das Chart nutzt TradingView Lightweight Charts **4.2.0** per CDN (unpkg, feste Version).
  API von v4 (`addCandlestickSeries`); v5 hat eine andere API.

## Sprachen (deutsch/englisch)

- `web/i18n.js` als erstes Skript jeder Seite (im `<head>`): `LANG` aus `?lang=`, `localStorage.lang` oder
  Browsersprache; setzt das Cookie `lang` für den Server. Der Umschalter DE/EN sitzt in der Navigationsleiste (`nav.js`).
- Texte im Code: `L("deutsch", "english")`; Zahlen über `LOCALE`, `DEC`, `PCT` (`format.js`), nie fest `"de-DE"`,
  `","` oder `" %"`. Statisches HTML übersetzt `translateStatic` beim Laden über `addStatic({ "deutscher Text": "…" })`
  im Skript der jeweiligen Seite (Schlüssel = Text mit zusammengefassten Leerzeichen, auch `title`, `placeholder`,
  `aria-label`). Neue Texte also immer in beiden Sprachen anlegen.
- Tooltips: `TIPS` (deutsch) und `TIPS_EN` in `tips.js`, gleiche Schlüssel; Skalen in beiden mitpflegen.
  Signalnamen bleiben deutsche Schlüssel (`SIGNAL_WEIGHTS`, `SIGNAL_TIPS`), angezeigt über `signalName()`.
- Server: `Query.lang` (Parameter oder Cookie). Caches halten die deutsche Fassung; `localize_coin` formuliert
  Begründungen, Einschätzung und Lagebild aus denselben Daten neu (`score`, `verdict`, `situation` mit `lang`,
  englisch in `SITUATIONS_EN`, `ACTIONS_EN`, `ACTIONS_EXTREME_EN`). Profil: `get_coin_profile` baut beide Sprachen
  aus einer Abfrage (`CATEGORY_EN`, `coin_type`/`coin_assessment` mit `lang`). News: `NEWS_FEEDS_EN` für Englisch.
  Browser-Caches mit Servertexten (`lastPayload_…`, `lastWatchCoins_…`) sind je Sprache getrennt.

## Score-Logik

- Jedes Signal rechnet seinen Messwert über Ankerpunkte (`*_SCALE` oben in `sentiment.py`)
  linear in −100 … +100 um; der Score ist der **gewichtete Durchschnitt** aller vorhandenen Signale
  (`SIGNAL_WEIGHTS`: MA200 und DVOL 1,5, Fear & Greed und RSI 0,5, Rest 1), damit Coins mit unterschiedlich
  vielen Signalen vergleichbar bleiben. Die Gewichte stammen aus einem Backtest (BTC/ETH/SOL, 04/2024–11/2026,
  Score gegen Kursänderung 30/90 Tage danach); Ergebnis: ab +40 deutlich schlechtere Folgerenditen, extreme
  Angst allein kein verlässliches Kaufsignal – daran sind `verdict`, `ACTIONS` und `ACTIONS_EXTREME` ausgerichtet.
- Trefferquote: `/api/score-history` rechnet `SCORE_STATS_DAYS` (960) Tage nach, liefert im Chart nur das
  letzte Jahr und dazu `track_record` (`score_track_record`: je Zone Anteil höher und Median nach 30/90 Tagen,
  Vergleich alle Tage). Anzeige unter dem Score im Kopf der Detailseite (`renderTrack` in `coin-score.js`,
  Zonen wie `score_zone`). Gewichte oder Zonen ändern → beide Stellen und Tooltips `score`/`trackRecord` anpassen.
- DVOL gibt es nur für BTC und ETH.
- MVRV (`MVRV_SCALE`, Gewicht 1) kommt von Coin Metrics Community (`get_mvrv`, letzter Tageswert = gestern),
  nicht für alle Coins (z. B. nicht SOL). `get_mvrv` wartet nie lange: bekannter Wert sofort (Erneuerung stündlich
  im Hintergrund), nur der erste Abruf je Coin wartet bis `MVRV_WAIT` (8 s), sonst fehlt MVRV im Score vorerst. Skala aus BTC vor 2018, geprüft ab 2018 auf 7 Coins. Der Börsenbestand
  wurde ebenfalls getestet, ist aber seit 2022 in der Richtung umgedreht → nur Anzeige, nicht im Score.
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
- **Caches in `server.py` laufen über `TTLCache`** (Lock je Schlüssel, Größenlimit, Lader liefert None →
  letzter Wert bleibt, `Expires(wert, s)` für kurz gültige Teilergebnisse). `stale=True` liefert Abgelaufenes
  sofort und lädt im Hintergrund nach; nicht für Kerzen, `/api/coin`, Suche und Liquidations-Verlauf.
  Neue Endpunkte: einen `TTLCache` anlegen statt eigenem Dict und Lock.
- **CoinGecko ohne API-Key ist stark limitiert (HTTP 429).** Abfragen bündeln statt pro Coin
  einzeln abzufragen; der Server behält bei Fehlern den letzten erfolgreichen Wert (bei 429
  wiederholt `fetch_json` nur für die anderen Quellen, für CoinGecko siehe unten). Neue Features nicht mit zusätzlichen CoinGecko-Abfragen pro
  Seitenaufruf bauen.
- **Skalen stehen an zwei Stellen (plus Methodik-Version):** Ändert sich ein Ankerpunkt in `sentiment.py`, auch den
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
  Zwei Ansichten (Umschalter, gemerkt in `localStorage.liqView`): „Standard“ (halbtransparent in `--hot`,
  140 Stufen) und „Farbverlauf“ (immer dunkel, Farbskala `LIQ_LUT` wie „magma“, 320 Stufen, `LIQ_DARK_GAMMA`,
  Infofeld an der Maus). Beide aus `buildLiqModel`, nur andere Auflösung; die Cluster-Liste nutzt immer „Standard“.
- **Liquidationen (`liquidations.py`):** Binance liefert `!forceOrder@arr` nur noch über
  `wss://fstream.binance.com/market/ws/` (die alte Adresse `/ws/` verbindet, schickt aber nichts) und mischt
  dort COIN-M-Kontrakte (`BTCUSD_PERP`) bei: deren Menge sind Kontrakte zu 100 $ (BTC) bzw. 10 $ (Rest).
  Binance meldet je Symbol höchstens eine Liquidation pro Sekunde. Bybit `allLiquidation`: `S = Buy` heißt
  Long liquidiert. OKX rechnet Kontrakte über `ctVal` in USD um und schickt beim Verbinden ältere Meldungen
  erneut (der `Store` sortiert ein und verwirft Duplikate). `data/` ist in `.gitignore` und wird von
  `deploy.sh` nicht überschrieben.
- Es gibt kein Node.js; JS-Syntax lässt sich mit `osascript -l JavaScript` prüfen.

## Arbeitsweise

- Oberfläche zweisprachig (siehe „Sprachen“), Code-Kommentare auf Deutsch, Commit-Nachrichten auf Englisch.
- Änderungen erst im Browser prüfen lassen, **committen nur auf ausdrückliche Anweisung**.

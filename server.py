#!/usr/bin/env python3
"""Lokaler Webserver für das Crypto-Sentiment-Dashboard.

Nutzt die Datenabfragen und den Score aus sentiment.py und zeigt
BTC, ETH und SOL im Browser an.

Aufruf:
  python3 server.py             # http://localhost:8000
  python3 server.py --port 8080
"""

import argparse
import json
import os
import re
import signal
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import alerts
import liquidations
import sentiment

SYMBOLS = ["BTC", "ETH", "SOL"]
CACHE_SECONDS = 300
MARKETS_PER_PAGE = 50
MARKETS_MAX_PAGE = 20     # Top 1000, wie bei Cryptobubbles
KLINE_INTERVALS = {"1d", "4h", "1h", "15m", "5m", "3m", "1m"}   # wie INTERVAL_LABEL in coin.html
KLINE_CACHE_SECONDS = 60  # Binance ist großzügig limitiert; die letzte Kerze soll aktuell sein
STATIC_DIR = Path(__file__).resolve().parent / "web"
# Liquidationen der letzten 24 h (liquidations.py); auf dem Server setzt systemd STATE_DIRECTORY
DATA_DIR = Path(os.environ.get("STATE_DIRECTORY") or Path(__file__).resolve().parent / "data")
LIQ_EXCHANGES = set(liquidations.EXCHANGES)
_collector: Optional[liquidations.Collector] = None   # startet in main()
_alerts: Optional[alerts.Checker] = None              # startet in main()
POST_MAX_BYTES = 10_000   # Anfragen der Alarmseite sind klein

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,15}$")
COIN_ID_RE = re.compile(r"^[a-z0-9-]{1,100}$")  # CoinGecko-ID, z. B. "bitcoin"

DEFI_CACHE_SECONDS = 600  # DefiLlama aktualisiert TVL ohnehin nur grob stündlich; die Antwort ist ~10 MB
LLAMA_INDEX_SECONDS = 24 * 3600   # Zuordnung Coin → Protokoll ändert sich selten
FEES_CACHE_SECONDS = 1800         # DefiLlama aktualisiert Gebühren täglich
WATCH_WORKERS = 8                 # Watchlist-Karten: so viele Coins gleichzeitig laden
RETRY_SECONDS = 30                # unvollständige Ergebnisse (Abfrage fehlgeschlagen) nur so lange cachen
CATEGORY_PER_PAGE = 100   # Coins einer Sparte: eine Seite, die größten 100
CATEGORY_RE = re.compile(r"^[a-z0-9-]{1,100}$")
SEARCH_MAX_LEN = 40
NEWS_CACHE_SECONDS = 900          # RSS-Feeds: alle 15 Min. reicht
NAME_RE = re.compile(r"^[\w .&'+-]{1,40}$")   # Coin-Name für die News-Suche, z. B. "Bitcoin Cash"
PROFILE_CACHE_SECONDS = 24 * 3600   # Beschreibung, Kategorien und Angebot ändern sich selten
LIQ_HISTORY_DAYS = (7, 30, 90, 365)
LIQ_HISTORY_CACHE_SECONDS = 60


class Expires:
    """Rückgabe eines Laders, wenn der Wert kürzer als üblich gelten soll (z. B. Teilergebnis)."""

    def __init__(self, value: Any, seconds: float) -> None:
        self.value, self.seconds = value, seconds


class TTLCache:
    """Zwischenspeicher mit Ablaufzeit für alle API-Daten des Servers.

    - Lock je Schlüssel: Anfragen für verschiedene Coins warten nicht aufeinander,
      gleichzeitige Anfragen für denselben Schlüssel laden nur einmal.
    - Liefert der Lader None, bleibt der letzte erfolgreiche Wert stehen (auch abgelaufen).
      Ausnahmen des Laders werden durchgereicht (außer beim Laden im Hintergrund).
    - stale=True: ein abgelaufener Wert wird sofort geliefert und im Hintergrund erneuert
      (höchstens ein Thread je Schlüssel). force lädt immer sofort und wartet.
    - Höchstens max_entries Einträge; der am längsten nicht neu geladene fällt heraus.
    """

    def __init__(self, name: str, ttl: float, max_entries: int = 200, stale: bool = False) -> None:
        self.name, self.ttl, self.max_entries, self.stale = name, ttl, max_entries, stale
        self._entries: Dict[Any, Tuple[float, Any]] = {}   # Schlüssel → (gültig bis, Wert), älteste zuerst
        self._locks: Dict[Any, threading.Lock] = {}
        self._refreshing: set = set()
        self._guard = threading.Lock()   # nur kurz gehalten (für die Dicts oben), nie während des Ladens

    def peek(self, key: Any) -> Any:
        """Letzter Wert ohne Laden (auch abgelaufen), sonst None."""
        with self._guard:
            entry = self._entries.get(key)
        return entry[1] if entry else None

    def fresh(self, key: Any) -> bool:
        """Liegt ein noch gültiger Wert vor?"""
        with self._guard:
            entry = self._entries.get(key)
        return bool(entry) and time.time() < entry[0]

    def set(self, key: Any, value: Any, seconds: Optional[float] = None) -> Any:
        # Abrufzeit für die Statusanzeige im Browser (Quelle · Stand · aktuell/verzögert/veraltet);
        # bleibt beim letzten Wert stehen, wenn ein Neuladen scheitert, und zeigt dann das echte Alter
        if isinstance(value, dict) and "fetched_at" not in value:
            value = {**value, "fetched_at": round(time.time())}
        with self._guard:
            self._entries.pop(key, None)
            self._entries[key] = (time.time() + (self.ttl if seconds is None else seconds), value)
            while len(self._entries) > self.max_entries:
                old = next(iter(self._entries))
                del self._entries[old]
                lock = self._locks.get(old)
                if lock is not None and not lock.locked():
                    del self._locks[old]
        return value

    def get(self, key: Any, loader: Callable[[Any], Any], force: bool = False) -> Any:
        """Wert zum Schlüssel; loader(letzter Wert oder None) lädt neu, wenn nötig."""
        start_refresh = False
        with self._guard:
            entry = self._entries.get(key)
            if entry and not force:
                if time.time() < entry[0]:
                    return entry[1]
                if self.stale:
                    start_refresh = key not in self._refreshing
                    self._refreshing.add(key)
            lock = self._locks.setdefault(key, threading.Lock())
        if entry and not force and self.stale:
            if start_refresh:
                threading.Thread(target=self._refresh, args=(key, loader), daemon=True,
                                 name=f"cache-{self.name}").start()
            return entry[1]
        with lock:
            if not force:
                with self._guard:
                    entry = self._entries.get(key)
                if entry and time.time() < entry[0]:
                    return entry[1]   # inzwischen von einer anderen Anfrage geladen
            return self._load(key, loader)

    def refresh(self, key: Any, loader: Callable[[Any], Any]) -> None:
        """Laden, falls der Wert fehlt oder abgelaufen ist (z. B. beim Start im eigenen Thread)."""
        with self._guard:
            if key in self._refreshing:
                return
            self._refreshing.add(key)
        self._refresh(key, loader)

    def _load(self, key: Any, loader: Callable[[Any], Any]) -> Any:
        previous = self.peek(key)
        result = loader(previous)
        seconds = None
        if isinstance(result, Expires):
            result, seconds = result.value, result.seconds
        if result is None:
            return previous
        return self.set(key, result, seconds)

    def _refresh(self, key: Any, loader: Callable[[Any], Any]) -> None:
        try:
            with self._guard:
                lock = self._locks.setdefault(key, threading.Lock())
            with lock:
                with self._guard:
                    entry = self._entries.get(key)
                if not entry or time.time() >= entry[0]:
                    self._load(key, loader)
        except Exception as exc:   # nächster Aufruf versucht es erneut
            print(f"  [warn] Aktualisierung im Hintergrund ({self.name}) fehlgeschlagen: {exc}")
        finally:
            with self._guard:
                self._refreshing.discard(key)


# stale=True: abgelaufene Werte sofort liefern und im Hintergrund erneuern (Grund jeweils dahinter)
_payload_cache = TTLCache("sentiment", CACHE_SECONDS, max_entries=1, stale=True)   # Dashboard
_coin_cache = TTLCache("coin", CACHE_SECONDS)   # nicht stale: Score soll aktuell sein; Teilergebnis nach RETRY_SECONDS neu
_markets_cache = TTLCache("markets", CACHE_SECONDS, max_entries=100, stale=True)   # Seite oder "<sparte>:1"
_categories_cache = TTLCache("categories", CACHE_SECONDS, max_entries=1, stale=True)
_watchlist_cache = TTLCache("watchlist", CACHE_SECONDS, stale=True)   # Schlüssel: sortierte Coin-IDs
_search_cache = TTLCache("search", CACHE_SECONDS, max_entries=100)   # nicht stale: Suchbegriffe sind kurzlebig
_klines_cache = TTLCache("klines", KLINE_CACHE_SECONDS, max_entries=300)   # nicht stale: letzte Kerze soll aktuell sein
_derivs_cache = TTLCache("derivatives", CACHE_SECONDS, stale=True)   # Funding/OI: Stunden- bis Tageswerte
_defi_cache = TTLCache("defi", DEFI_CACHE_SECONDS, max_entries=1, stale=True)
_llama_index_cache = TTLCache("llama-index", LLAMA_INDEX_SECONDS, max_entries=1, stale=True)
_fees_cache = TTLCache("fees", FEES_CACHE_SECONDS, stale=True)   # Schlüssel: "<art>:<slug>"
_news_cache = TTLCache("news", NEWS_CACHE_SECONDS, max_entries=len(sentiment.LANGS), stale=True)   # alle Feeds je Sprache
_liq_cache = TTLCache("liquidation-inputs", CACHE_SECONDS, stale=True)   # Stundenkerzen und OI
_history_cache = TTLCache("score-history", CACHE_SECONDS, stale=True)   # Score je Tag
_onchain_cache = TTLCache("onchain", 3600, max_entries=300, stale=True)   # Coin Metrics, Tageswerte
_profile_cache = TTLCache("profile", PROFILE_CACHE_SECONDS, max_entries=300, stale=True)   # Schlüssel: CoinGecko-ID
_profile_ids = TTLCache("profile-ids", 30 * 86400, max_entries=500)   # Symbol → CoinGecko-ID (ohne ID-Parameter)
for _symbol, _coin_id in {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}.items():
    _profile_ids.set(_symbol, _coin_id, 10 * 365 * 86400)
_liq_history_cache = TTLCache("liq-history", LIQ_HISTORY_CACHE_SECONDS)   # lokale SQLite-Abfrage, schnell


def build_coin(symbol: str, global_data: Dict[str, Any], symbol_data: Dict[str, Any]) -> Dict[str, Any]:
    data = {"fear_greed": global_data["fear_greed"], **symbol_data}
    signals = sentiment.score(data)
    total = sentiment.total_score(signals)
    return {
        "symbol": symbol,
        "data": symbol_data,
        "signals": signals,
        "score": total,
        "verdict": sentiment.verdict(total, len(signals)),
        "situation": sentiment.situation(data, signals, total),   # Lagebild für den Kopf der Detailseite
        "fear_greed": global_data["fear_greed"],
    }


def build_payload(previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with ThreadPoolExecutor(max_workers=len(SYMBOLS) + 2) as pool:
        global_future = pool.submit(sentiment.collect_global)
        mcap_future = pool.submit(sentiment.safe, sentiment.get_market_caps, SYMBOLS)
        symbol_futures = {s: pool.submit(sentiment.collect_symbol, s) for s in SYMBOLS}
        global_data = global_future.result()
        mcaps = mcap_future.result() or {}
        symbol_data = {s: {**f.result(), "market_cap": (mcaps.get(s) or {}).get("market_cap"),
                           "market_stats": (mcaps.get(s) or {}).get("market_stats"),
                           "name": (mcaps.get(s) or {}).get("name"),
                           "coin_id": (mcaps.get(s) or {}).get("coin_id")}
                       for s, f in symbol_futures.items()}

    # Fehlgeschlagene Abfragen (z. B. Rate-Limit) durch den letzten erfolgreichen Wert ersetzen
    if previous:
        for key, value in global_data.items():
            if value is None:
                global_data[key] = previous["global"].get(key)
        old_coins = {c["symbol"]: c["data"] for c in previous["coins"]}
        for s in SYMBOLS:
            for key, value in symbol_data[s].items():
                if value is None:
                    symbol_data[s][key] = old_coins.get(s, {}).get(key)

    coins = [build_coin(s, global_data, symbol_data[s]) for s in SYMBOLS]

    return {
        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),  # mit Zeitzone, Browser rechnet das Alter
        "global": global_data,
        "coins": coins,
    }


def _refresh_in_background() -> None:
    """Dashboard-Daten laden, falls sie fehlen oder abgelaufen sind (beim Start aus main())."""
    _payload_cache.refresh("payload", build_payload)


def get_liq_history(days: int, coin: Optional[str], exchange: Optional[str], crypto_only: bool = False) -> Dict[str, Any]:
    """Liquidationen über Wochen/Monate aus SQLite (Stundensummen), 60 s gecacht."""
    return _liq_history_cache.get((days, coin, exchange, crypto_only),
                                  lambda _: _collector.history(days, coin, exchange, crypto_only))


def get_payload(force: bool = False) -> Dict[str, Any]:
    """Abgelaufener Stand wird sofort geliefert und im Hintergrund erneuert,
    damit niemand beim Öffnen der Seite auf alle Abfragen warten muss."""
    return _payload_cache.get("payload", build_payload, force=force)


def get_coin(symbol: str, coin_id: Optional[str] = None, force: bool = False,
             market: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Einzelner Coin (Detailseite, Watchlist), eigener Cache je Coin.

    market: CoinGecko-Daten aus der gebündelten Abfrage der Watchlist (leer = fehlgeschlagen), sonst eigene Abfrage."""
    global_data = get_payload()["global"]

    def load(previous: Optional[Dict[str, Any]]) -> Any:
        symbol_data = sentiment.collect_symbol(symbol, coin_id, with_market=True, market=market)
        # CoinGecko-Abfrage fehlgeschlagen (z. B. Limit-Pause): Lücken aus dem letzten Stand
        # füllen und bald erneut versuchen, statt das unvollständige Ergebnis 5 Min. zu behalten
        incomplete = symbol_data.get("market_stats") is None
        if previous:
            for k, old in previous["data"].items():
                if symbol_data.get(k) is None:
                    symbol_data[k] = old
        coin = build_coin(symbol, global_data, symbol_data)
        return Expires(coin, RETRY_SECONDS) if incomplete else coin

    return _coin_cache.get(f"{symbol}:{coin_id or ''}", load, force=force)


def get_watch_coins(entries: List[Tuple[str, str]], force: bool = False) -> Dict[str, Any]:
    """Karten aller Watchlist-Coins fürs Dashboard: Score und Kennzahlen je Coin (Cache wie /api/coin).

    CoinGecko nur einmal für alle Coins, deren Cache abgelaufen ist; Binance und Coin Metrics je Coin parallel."""
    due = [cid for sym, cid in entries if force or not _coin_cache.fresh(f"{sym}:{cid}")]
    markets = (sentiment.safe(sentiment.get_coingecko_coins, due) or {}) if due else {}
    with ThreadPoolExecutor(max_workers=min(WATCH_WORKERS, len(entries))) as pool:
        coins = list(pool.map(lambda e: get_coin(e[0], e[1], force=force, market=markets.get(e[1], {})), entries))
    return {"updated": datetime.now().astimezone().isoformat(timespec="seconds"), "coins": coins}


def get_markets(page: int, category: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Eine Seite der Rangliste (oder die größten Coins einer Sparte);
    bei Fehlern die zuletzt erfolgreich geladene Fassung."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        if category:
            coins = sentiment.safe(sentiment.get_markets, 1, CATEGORY_PER_PAGE, category)
        else:
            coins = sentiment.safe(sentiment.get_markets, page, MARKETS_PER_PAGE)
        if coins is None:
            return None
        return {
            "page": 1 if category else page,
            "per_page": CATEGORY_PER_PAGE if category else MARKETS_PER_PAGE,
            "max_page": 1 if category else MARKETS_MAX_PAGE,
            "category": category,
            "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
            "coins": coins,
        }

    return _markets_cache.get(f"{category}:1" if category else str(page), load)


# --- Screener für die Marktübersicht: Top 1000 mit Assetklasse, MA50/MA200 und Futures-Paar ----------------
# Die Liste kommt in SCREENER_PAGES CoinGecko-Abfragen à 250 (statt 20 Seiten à 50). Assetklassen aus CoinGecko-
# Kategorien (je Kategorie die größten 250, einmal am Tag im Hintergrund), MA-Abstände und Futures aus Binance
# (stündlich im Hintergrund, nur Coins mit USDT-Paar). Klassen und MA werden bei jeder Anfrage frisch dazugemischt,
# damit sie erscheinen, sobald sie fertig sind.
SCREENER_PAGES = 4
SCREENER_PER_PAGE = 250
ASSET_CLASSES = {   # Klasse → CoinGecko-Kategorien; "crypto" = alles, was keine der NON_CRYPTO-Klassen ist
    "stable": ["stablecoins"], "defi": ["decentralized-finance-defi"], "l1": ["layer-1"], "l2": ["layer-2"],
    "meme": ["meme-token"], "privacy": ["privacy-coins"], "rwa": ["real-world-assets-rwa"],
    "fund": ["tokenized-treasuries", "tokenized-money-market-fund-mmfs", "tokenized-exchange-traded-funds-etfs"],
    "gold": ["tokenized-gold"], "wrapped": ["wrapped-tokens", "bridged-tokens"],
}
NON_CRYPTO = {"stable", "fund", "gold", "wrapped"}
MA_FAST, MA_SLOW = 50, sentiment.MA_PERIOD
MA_PRICE_TOLERANCE = 0.15   # Binance-Kurs weicht mehr ab → anderes Projekt mit gleichem Kürzel, nicht verwenden
SCREENER_MA_WORKERS = 8
CLASSES_PAUSE = 30          # s zusätzlich zwischen den Kategorie-Abfragen (15 Stück, einmal am Tag, ~9 Min.; lässt Platz für die Seiten)
_screener_cache = TTLCache("screener", CACHE_SECONDS, max_entries=1, stale=True)
_classes_cache = TTLCache("asset-classes", 24 * 3600, max_entries=1, stale=True)
_ma_cache = TTLCache("screener-ma", 3600, max_entries=1, stale=True)


def _in_background(cache: TTLCache, key: str, loader: Callable[[Any], Any]) -> None:
    """Laden im eigenen Thread, wenn der Wert fehlt oder abgelaufen ist (höchstens ein Thread je Schlüssel)."""
    if not cache.fresh(key):
        threading.Thread(target=cache.refresh, args=(key, loader), daemon=True, name=f"bg-{cache.name}").start()


def load_asset_classes(_: Any) -> Optional[Dict[str, Any]]:
    ids: Dict[str, List[str]] = {}
    ok = 0
    for cls, cats in ASSET_CLASSES.items():
        for cat in cats:
            coins = None
            for _attempt in range(3):
                time.sleep(CLASSES_PAUSE)   # läuft im Hintergrund: CoinGecko Luft für die Seitenabfragen lassen
                coins = sentiment.safe(sentiment.get_markets, 1, SCREENER_PER_PAGE, cat)
                wait = sentiment.coingecko_retry_in()
                if coins is not None or wait <= sentiment.COINGECKO_MIN_INTERVAL:
                    break
                time.sleep(wait + 1)   # Limit-Pause abwarten, dann dieselbe Kategorie erneut
            if coins is None:
                continue
            ok += 1
            for c in coins:
                ids.setdefault(c["id"], [])
                if cls not in ids[c["id"]]:
                    ids[c["id"]].append(cls)
    total = sum(len(c) for c in ASSET_CLASSES.values())
    if not ok:
        return None
    result = {"ids": ids, "complete": ok == total}
    return result if ok == total else Expires(result, 600)   # Lücken: in 10 Min. erneut versuchen


def _binance_symbols(url: str) -> set:
    data = sentiment.safe(sentiment.fetch_json, url) or []
    return {d["symbol"] for d in data if isinstance(d, dict) and d.get("symbol", "").endswith("USDT")}


def load_screener_ma(symbols: List[str]) -> Callable[[Any], Any]:
    def load(_: Any) -> Optional[Dict[str, Any]]:
        spot = _binance_symbols("https://api.binance.com/api/v3/ticker/price")
        fut = _binance_symbols("https://fapi.binance.com/fapi/v1/ticker/price")
        if not spot and not fut:
            return None

        def one(sym: str) -> Optional[Tuple[str, Dict[str, Any]]]:
            pair = sym + "USDT"
            base = ("https://api.binance.com/api/v3/klines" if pair in spot
                    else "https://fapi.binance.com/fapi/v1/klines" if pair in fut else None)
            if not base:
                return None
            rows = sentiment.safe(sentiment.fetch_json, f"{base}?symbol={pair}&interval=1d&limit={MA_SLOW + 1}")
            if not rows:
                return None
            closes = [float(r[4]) for r in rows]
            last = closes[-1]
            dist = lambda n: (last / (sum(closes[-n:]) / n) - 1) if len(closes) >= n else None
            return sym, {"close": last, "days": len(closes), "ma50": dist(MA_FAST), "ma200": dist(MA_SLOW),
                         "futures": pair in fut or sentiment.fpair(sym) in fut}   # auch 1000PEPE usw.

        with ThreadPoolExecutor(max_workers=SCREENER_MA_WORKERS) as pool:
            got = [r for r in pool.map(one, symbols) if r]
        return {"symbols": dict(got)}
    return load


def get_screener() -> Optional[Dict[str, Any]]:
    def load(_: Any) -> Optional[Dict[str, Any]]:
        coins: List[Dict[str, Any]] = []
        for page in range(1, SCREENER_PAGES + 1):
            part = sentiment.safe(sentiment.get_markets, page, SCREENER_PER_PAGE)
            if part is None:
                return None if not coins else Expires({"coins": coins, "partial": True}, RETRY_SECONDS)
            coins += part
        return {"coins": coins, "updated": datetime.now().astimezone().isoformat(timespec="seconds")}

    base = _screener_cache.get("top", load)
    if not base:
        return None
    _in_background(_classes_cache, "all", load_asset_classes)
    symbols = sorted({c["symbol"] for c in base["coins"] if SYMBOL_RE.match(c["symbol"])})
    _in_background(_ma_cache, "all", load_screener_ma(symbols))
    classes, ma = _classes_cache.peek("all"), _ma_cache.peek("all")
    cls_ids = classes["ids"] if classes else {}
    ma_sym = ma["symbols"] if ma else {}
    rows = []
    for c in base["coins"]:
        row = dict(c)
        if classes:
            cl = list(cls_ids.get(c["id"], []))
            row["classes"] = (["crypto"] if not NON_CRYPTO.intersection(cl) else []) + cl
        m = ma_sym.get(c["symbol"])
        if m and c.get("price") and abs(m["close"] / c["price"] - 1) > MA_PRICE_TOLERANCE:
            m = None   # gleiches Kürzel, anderer Coin
        if ma:
            row["ma50"] = m["ma50"] if m else None
            row["ma200"] = m["ma200"] if m else None
            # vollständiger Score = alle Binance-Signale möglich: USDT-Perpetual und mindestens 200 Tageskerzen
            row["complete"] = bool(m and m["futures"] and m["days"] >= MA_SLOW)
        rows.append(row)
    return {"updated": base.get("updated"), "fetched_at": base.get("fetched_at"), "partial": bool(base.get("partial")),
            "coins": rows, "classes_ready": bool(classes), "classes_at": classes and classes.get("fetched_at"),
            "ma_ready": bool(ma), "ma_at": ma and ma.get("fetched_at")}


def get_categories() -> Optional[Dict[str, Any]]:
    """Alle Sparten; eine CoinGecko-Abfrage, 5 Min. gecacht."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        cats = sentiment.safe(sentiment.get_categories)
        return {"updated": datetime.now().astimezone().isoformat(timespec="seconds"), "categories": cats} if cats else None

    return _categories_cache.get("all", load)


def get_watchlist(ids: List[str]) -> Optional[Dict[str, Any]]:
    """Marktdaten der Watchlist-Coins (im Browser gespeichert); eine CoinGecko-Abfrage je ID-Satz, 5 Min. gecacht."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        coins = sentiment.safe(sentiment.get_markets_by_ids, ids)
        if coins is None:
            return None
        return {"updated": datetime.now().astimezone().isoformat(timespec="seconds"), "coins": coins}

    return _watchlist_cache.get(",".join(ids), load)


def search_markets(query: str) -> Optional[Dict[str, Any]]:
    """Suchtreffer mit Marktdaten; je Suchbegriff 5 Min. gecacht (zwei CoinGecko-Abfragen)."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        coins = sentiment.safe(sentiment.search_markets, query)
        if coins is None:
            return None
        return {"query": query, "updated": datetime.now().astimezone().isoformat(timespec="seconds"), "coins": coins}

    return _search_cache.get(query.lower(), load)


def get_klines(symbol: str, interval: str) -> Optional[Dict[str, Any]]:
    """Kerzen für das Chart (Spot, sonst Futures); None, wenn Binance kein USDT-Paar hat."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        result = sentiment.safe(sentiment.get_klines, symbol, interval)
        if not (result and result["candles"]):
            return None
        return {"symbol": symbol, "interval": interval, "source": result["source"], "candles": result["candles"]}

    return _klines_cache.get(f"{symbol}:{interval}", load)


def get_derivatives(symbol: str) -> Optional[Dict[str, Any]]:
    """Funding- und Open-Interest-Verlauf (Binance Futures); None ohne USDT-Perpetual."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        funding = sentiment.safe(sentiment.get_funding_history, symbol)
        oi = sentiment.safe(sentiment.get_open_interest_history, symbol)
        if not (funding or oi):
            return None
        return {"symbol": symbol, "funding": funding or [], "open_interest": oi or []}

    return _derivs_cache.get(symbol, load)


def get_defi_tvl() -> Optional[Dict[str, Any]]:
    """TVL je Kategorie und Chain (DefiLlama); bei Fehlern der letzte erfolgreiche Stand."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        result = sentiment.safe(sentiment.get_defi_tvl)
        return {"updated": datetime.now().astimezone().isoformat(timespec="seconds"), **result} if result else None

    return _defi_cache.get("all", load)


def get_fees(symbol: str, coin_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Gebühren und Einnahmen (DefiLlama) eines Coins; zugeordnet über CoinGecko-ID, sonst Symbol."""
    idx = _llama_index_cache.get("all", lambda _: sentiment.safe(sentiment.get_llama_index) or None)
    if not idx:
        return None
    entry = (idx["gecko"].get(coin_id) if coin_id else None) or idx["symbol"].get(symbol)
    if not entry:
        return None

    def load(_: Any) -> Any:
        try:
            result = sentiment.get_fees(entry)
        except ValueError as exc:   # DefiLlama nicht erreichbar: letzten Stand behalten, nichts cachen
            print(f"  [warn] get_fees: {exc}")
            return None
        # Teilergebnis (eine Datenart fehlgeschlagen) nur kurz cachen
        incomplete = bool(result and result.pop("incomplete", False))
        return Expires(result, RETRY_SECONDS) if incomplete else result

    return _fees_cache.get(entry["kind"] + ":" + entry["slug"], load)


def get_news(symbol: str, name: Optional[str], lang: str = "de") -> Optional[Dict[str, Any]]:
    """Meldungen zum Coin aus den RSS-Feeds der Sprache; die Feeds werden je Sprache gemeinsam 15 Min.
    gecacht, gefiltert wird je Abruf. Bei Fehlern der letzte erfolgreiche Stand."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        items = sentiment.safe(sentiment.get_news, lang)
        return {"items": items, "updated": datetime.now().astimezone().isoformat(timespec="seconds")} if items else None

    feeds = _news_cache.get(lang, load)
    if not feeds:
        return None
    sources = sentiment.NEWS_FEEDS_EN if lang == "en" else sentiment.NEWS_FEEDS
    return {"symbol": symbol, "name": name, "updated": feeds["updated"],
            "sources": [s for s, _ in sources], **sentiment.match_news(feeds["items"], symbol, name)}


def get_liquidation_inputs(symbol: str) -> Optional[Dict[str, Any]]:
    """Kerzen und OI je Stunde (Binance Futures) für die Liquidations-Heatmap; 5 Min. gecacht."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        result = sentiment.safe(sentiment.get_liquidation_inputs, symbol)
        if not (result and result["candles"] and result["open_interest"]):
            return None
        return {"symbol": symbol, **result}

    return _liq_cache.get(symbol, load)


def get_score_history(symbol: str) -> Optional[Dict[str, Any]]:
    """Rückwirkend berechneter Score je Tag; None ohne Binance-Kerzen und -Futures."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        # lange Historie für die Trefferquote je Score-Zone, im Chart nur das letzte Jahr
        result = sentiment.safe(sentiment.get_score_history, symbol, sentiment.SCORE_STATS_DAYS)
        if not result or not result["days"]:
            return None
        days = result["days"]
        return {"symbol": symbol, **result, "days": days[-sentiment.SCORE_HISTORY_DAYS:],
                "track_record": sentiment.score_track_record(days)}

    return _history_cache.get(symbol, load)


def get_coin_profile(symbol: str, coin_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Profil eines Coins (Seite „Über den Coin“): eine CoinGecko-Abfrage, einen Tag gecacht.
    Ohne ID wird sie einmal über das Symbol ermittelt (eine Abfrage mehr). Bei Fehlern der letzte Stand."""
    if not coin_id:
        coin_id = _profile_ids.get(symbol, lambda _: sentiment.safe(sentiment.resolve_coin_id, symbol) or None)
        if not coin_id:
            return None

    def load(_: Any) -> Optional[Dict[str, Any]]:
        profile = sentiment.safe(sentiment.get_coin_profile, coin_id)   # {"de": …, "en": …}
        return {"updated": datetime.now().astimezone().isoformat(timespec="seconds"), **profile} if profile else None

    return _profile_cache.get(coin_id, load)


def localize_coin(coin: Dict[str, Any], lang: str) -> Dict[str, Any]:
    """Texte eines Coins (Begründungen, Einschätzung, Lagebild) in der Sprache; Zahlen bleiben gleich.
    Die Caches halten die deutsche Fassung, für andere Sprachen wird aus denselben Daten neu formuliert."""
    if lang == "de":
        return coin
    data = {"fear_greed": coin.get("fear_greed"), **coin["data"]}
    signals = sentiment.score(data, lang)
    total = sentiment.total_score(signals)
    return {**coin, "signals": signals, "score": total,
            "verdict": sentiment.verdict(total, len(signals), lang),
            "situation": sentiment.situation(data, signals, total, lang)}


def coingecko_error() -> Dict[str, Any]:
    """Fehlerantwort für CoinGecko-Ausfälle, mit Wartezeit, wenn gerade eine Limit-Pause läuft."""
    # Nur eine echte Limit-Pause (nach HTTP 429) melden, nicht den normalen Abstand zwischen zwei Abfragen;
    # sonst erscheint z. B. eine unbekannte Coin-ID als Limit, und die Seite versucht es endlos erneut
    wait = sentiment.coingecko_retry_in()
    if wait > sentiment.COINGECKO_MIN_INTERVAL:
        return {"error": f"CoinGecko-Limit erreicht (kostenlose API), neuer Versuch in {round(wait)} s",
                "retry_in": round(wait) + 1}
    return {"error": "CoinGecko nicht erreichbar"}


class ApiError(Exception):
    """Fehlerantwort eines Endpunkts: Statuscode und JSON-Inhalt."""

    def __init__(self, status: int, payload: Dict[str, Any]) -> None:
        super().__init__(status)
        self.status, self.payload = status, payload


class Query:
    """Parameter einer Anfrage; `q("symbol")` liefert den ersten Wert oder den Standardwert."""

    def __init__(self, query: str, cookie: str = "") -> None:
        self.params = parse_qs(query)
        self.force = self.params.get("refresh") == ["1"]
        # Sprache der Texte: ?lang=… oder Cookie "lang" (setzt web/i18n.js), sonst deutsch
        m = re.search(r"(?:^|;\s*)lang=(\w+)", cookie or "")
        lang = self("lang") or (m.group(1) if m else "")
        self.lang = lang if lang in sentiment.LANGS else "de"

    def __call__(self, name: str, default: str = "") -> str:
        return (self.params.get(name) or [default])[0]

    def symbol(self, name: str = "symbol") -> str:
        return self(name).upper()

    def coin_id(self) -> Optional[str]:
        return self("id") or None

    def text(self, name: str) -> str:
        """Leerzeichen zusammenfassen (Suchbegriff, Coin-Name)."""
        return " ".join(self(name).split())


def require(ok: Any, message: str) -> None:
    """Ungültige Parameter → 400."""
    if not ok:
        raise ApiError(400, {"error": message})


def found(payload: Optional[Dict[str, Any]], message: str, status: int = 404) -> Dict[str, Any]:
    """Keine Daten → 404 (bzw. 502, wenn die Quelle nicht erreichbar ist)."""
    if payload is None:
        raise ApiError(status, {"error": message})
    return payload


def from_coingecko(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Keine Daten von CoinGecko → 502, bei Limit-Pause mit retry_in."""
    if payload is None:
        raise ApiError(502, coingecko_error())
    return payload


def collector() -> liquidations.Collector:
    if _collector is None:
        raise ApiError(503, {"error": "Liquidations-Collector läuft nicht"})
    return _collector


def valid_symbol_and_id(q: Query) -> Tuple[str, Optional[str]]:
    symbol, coin_id = q.symbol(), q.coin_id()
    require(SYMBOL_RE.match(symbol) and not (coin_id and not COIN_ID_RE.match(coin_id)),
            "ungültiges Symbol oder ungültige Coin-ID")
    return symbol, coin_id


def valid_symbol(q: Query) -> str:
    symbol = q.symbol()
    require(SYMBOL_RE.match(symbol), "ungültiges Symbol")
    return symbol


# --- Endpunkte: je eine Funktion (Query → JSON), Fehler über ApiError

def api_sentiment(q: Query) -> Dict[str, Any]:
    payload = get_payload(force=q.force)
    if q.lang == "de":
        return payload
    return {**payload, "coins": [localize_coin(c, q.lang) for c in payload["coins"]]}


def api_markets(q: Query) -> Dict[str, Any]:
    page, category = q("page", "1"), q("category") or None
    require(page.isdigit() and 1 <= int(page) <= MARKETS_MAX_PAGE, "ungültige Seite")
    require(not category or CATEGORY_RE.match(category), "ungültige Sparte")
    return from_coingecko(get_markets(int(page), category))


def api_screener(q: Query) -> Dict[str, Any]:
    return from_coingecko(get_screener())


def api_categories(q: Query) -> Dict[str, Any]:
    return from_coingecko(get_categories())


def api_search(q: Query) -> Dict[str, Any]:
    term = q.text("q")
    require(1 <= len(term) <= SEARCH_MAX_LEN, "Suchbegriff fehlt oder ist zu lang")
    return from_coingecko(search_markets(term))


def api_watchlist(q: Query) -> Dict[str, Any]:
    ids = sorted({i for i in q("ids").split(",") if i})
    require(ids and len(ids) <= sentiment.WATCHLIST_MAX and all(COIN_ID_RE.match(i) for i in ids),
            "ungültige oder zu viele Coin-IDs")
    return from_coingecko(get_watchlist(ids))


def api_watch_coins(q: Query) -> Dict[str, Any]:
    """?coins=BTC:bitcoin,LINK:chainlink – Reihenfolge bleibt erhalten."""
    entries: List[Tuple[str, str]] = []
    for part in q("coins").split(","):
        sym, _, cid = part.partition(":")
        sym = sym.upper()
        require(SYMBOL_RE.match(sym) and COIN_ID_RE.match(cid), "ungültiger Coin: " + part[:40])
        if (sym, cid) not in entries:
            entries.append((sym, cid))
    require(len(entries) <= sentiment.WATCHLIST_MAX, "zu viele Coins")
    result = get_watch_coins(entries, force=q.force)
    return {**result, "coins": [localize_coin(c, q.lang) for c in result["coins"]]}


def api_coin(q: Query) -> Dict[str, Any]:
    symbol = valid_symbol(q)
    coin_id = q.coin_id()
    require(not coin_id or COIN_ID_RE.match(coin_id), "ungültige Coin-ID")
    return localize_coin(get_coin(symbol, coin_id, force=q.force), q.lang)


def api_coin_info(q: Query) -> Dict[str, Any]:
    profile = from_coingecko(get_coin_profile(*valid_symbol_and_id(q)))
    return {"updated": profile["updated"], **profile[q.lang]}


def api_klines(q: Query) -> Dict[str, Any]:
    symbol, interval = q.symbol(), q("interval", "1d")
    require(SYMBOL_RE.match(symbol) and interval in KLINE_INTERVALS, "ungültiges Symbol oder Intervall")
    return found(get_klines(symbol, interval), f"Keine Binance-Kursdaten für {symbol}/USDT")


def api_score_history(q: Query) -> Dict[str, Any]:
    symbol = valid_symbol(q)
    return found(get_score_history(symbol), f"Kein Score-Verlauf für {symbol} (keine Binance-Daten)")


def get_onchain(symbol: str) -> Optional[Dict[str, Any]]:
    """Börsenflüsse, Börsenbestand und MVRV je Tag (Coin Metrics); None, wenn der Coin dort fehlt.
    „Nicht vorhanden“ wird mitgecacht (Coin Metrics braucht manchmal > 20 s für diese Antwort)."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        try:
            return sentiment.get_onchain(symbol) or {"available": False}
        except Exception as exc:   # Netzfehler: letzten Stand behalten, bald erneut versuchen
            print(f"  [warn] get_onchain: {exc}")
            return None
    result = _onchain_cache.get(symbol, load)
    return result if result and result.get("available", True) else None


def api_onchain(q: Query) -> Dict[str, Any]:
    symbol = valid_symbol(q)
    return found(get_onchain(symbol), f"Keine On-Chain-Daten für {symbol} bei Coin Metrics")


def api_defi(q: Query) -> Dict[str, Any]:
    return found(get_defi_tvl(), "DefiLlama nicht erreichbar", 502)


def api_fees(q: Query) -> Dict[str, Any]:
    symbol, coin_id = valid_symbol_and_id(q)
    return found(get_fees(symbol, coin_id), f"Keine Gebührendaten für {symbol} bei DefiLlama")


def api_news(q: Query) -> Dict[str, Any]:
    symbol, name = q.symbol(), q.text("name") or None
    require(SYMBOL_RE.match(symbol) and not (name and not NAME_RE.match(name)),
            "ungültiges Symbol oder ungültiger Name")
    return found(get_news(symbol, name, q.lang), "News-Feeds nicht erreichbar", 502)


def api_liquidations(q: Query) -> Dict[str, Any]:
    symbol = valid_symbol(q)
    return found(get_liquidation_inputs(symbol), f"Keine Binance-Futures für {symbol}/USDT")


def api_derivatives(q: Query) -> Dict[str, Any]:
    symbol = valid_symbol(q)
    return found(get_derivatives(symbol), f"Keine Binance-Futures für {symbol}/USDT")


def api_liq_history(q: Query) -> Dict[str, Any]:
    collector()
    coin, exchange = q.symbol("coin") or None, q("exchange") or None
    try:
        days = int(q("days", "30"))
    except ValueError:
        days = 0
    require(days in LIQ_HISTORY_DAYS and not (coin and not SYMBOL_RE.match(coin))
            and not (exchange and exchange not in LIQ_EXCHANGES), "ungültiger Zeitraum, Coin oder Börse")
    return get_liq_history(days, coin, exchange, q("crypto") == "1")


def api_liq_overview(q: Query) -> Dict[str, Any]:
    col = collector()
    window = q("window", "24h")
    require(window in liquidations.WINDOWS, "ungültiges Zeitfenster")
    return col.overview(window, q("crypto") == "1")


def api_liq_feed(q: Query) -> Dict[str, Any]:
    col = collector()
    coin, exchange = q.symbol("coin") or None, q("exchange") or None
    try:
        min_usd = max(0.0, float(q("min", "0")))
        limit = min(200, max(1, int(q("limit", "60"))))
    except ValueError:
        raise ApiError(400, {"error": "ungültige Zahl"})
    require(not (coin and not SYMBOL_RE.match(coin)) and not (exchange and exchange not in LIQ_EXCHANGES),
            "ungültiger Coin oder ungültige Börse")
    return col.feed(coin, exchange, min_usd, limit, q("crypto") == "1")


def api_methodology(q: Query) -> Dict[str, Any]:
    """Offenlegung der Score-Methodik aus den echten Konstanten (Seite methodology.html), damit Seite und Code
    nicht auseinanderlaufen."""
    S = sentiment
    return {
        "version": S.METHODOLOGY_VERSION,
        "changes": [{"version": c["version"], "date": c["date"], "text": c.get(q.lang) or c["de"]} for c in S.METHODOLOGY_CHANGES],
        "signals": [{"name": name, "scale": anchors, "weight": S.SIGNAL_WEIGHTS.get(name, 1.0)}
                    for name, anchors in S.SIGNAL_SCALES.items()],
        "thresholds": {"hot": S.VERDICT_HOT, "warm": S.VERDICT_WARM, "strong": S.SIGNAL_STRONG},
        "params": {"rsi_period": S.RSI_PERIOD, "ma_period": S.MA_PERIOD, "dvol_window": S.DVOL_WINDOW,
                   "funding_base_hours": S.FUNDING_BASE_HOURS, "score_history_days": S.SCORE_HISTORY_DAYS,
                   "score_stats_days": S.SCORE_STATS_DAYS, "mvrv_wait": S.MVRV_WAIT, "mvrv_max_age": S.MVRV_MAX_AGE,
                   "dvol_symbols": sorted(S.DVOL_SYMBOLS)},
        "intervals": {"cache": CACHE_SECONDS, "klines": KLINE_CACHE_SECONDS, "defi": DEFI_CACHE_SECONDS,
                      "fees": FEES_CACHE_SECONDS, "news": NEWS_CACHE_SECONDS, "profile": PROFILE_CACHE_SECONDS,
                      "onchain": 3600, "retry": RETRY_SECONDS, "liq_history": LIQ_HISTORY_CACHE_SECONDS,
                      "alerts": alerts.CHECK_SECONDS},
    }


# --- Alarme (alerts.py): GET liefert alles für die Seite, POST ändert Regeln und Einstellungen

def alert_checker() -> alerts.Checker:
    if _alerts is None:
        raise ApiError(503, {"error": "Alarme laufen nicht"})
    return _alerts


def alerts_state() -> Dict[str, Any]:
    chk = alert_checker()
    return {
        "settings": chk.store.settings(),
        "rules": chk.store.rules(),
        "fired": chk.store.fired(),
        "last_run": chk.last_run,
        "last_error": chk.last_error,
        "check_seconds": alerts.CHECK_SECONDS,
        "cooldown": alerts.COOLDOWN,
    }


def api_alerts(q: Query) -> Dict[str, Any]:
    return alerts_state()


def api_alerts_post(body: Dict[str, Any]) -> Dict[str, Any]:
    """{"action": "add" | "delete" | "enable" | "settings" | "test" | "check", …}"""
    chk = alert_checker()
    action = body.get("action")
    if action == "add":
        kind, symbol, coin_id = body.get("kind"), str(body.get("symbol") or "").upper(), body.get("coin_id") or None
        require(kind in alerts.KINDS, "unbekannte Bedingung")
        if kind == "liq_1h":
            require(symbol == alerts.ALL_COINS or SYMBOL_RE.match(symbol), "ungültiger Coin")
            coin_id = None
        else:
            require(SYMBOL_RE.match(symbol) and isinstance(coin_id, str) and COIN_ID_RE.match(coin_id),
                    "ungültiger Coin")
        threshold = None
        if alerts.KINDS[kind]:
            try:
                threshold = float(body.get("threshold"))
            except (TypeError, ValueError):
                raise ApiError(400, {"error": "ungültige Schwelle"})
            require(threshold == threshold and abs(threshold) < 1e15, "ungültige Schwelle")
            require(kind != "liq_1h" and not kind.startswith("price") or threshold > 0, "Schwelle muss über 0 liegen")
        try:
            chk.store.add_rule(symbol, coin_id, kind, threshold)
        except ValueError as e:
            raise ApiError(400, {"error": str(e)})
    elif action in ("delete", "enable"):
        require(isinstance(body.get("id"), int), "ungültige Regel")
        if action == "delete":
            chk.store.delete_rule(body["id"])
        else:
            chk.store.set_enabled(body["id"], bool(body.get("enabled")))
    elif action == "settings":
        topic = str(body.get("topic") or "").strip()
        server = str(body.get("server") or alerts.NTFY_DEFAULT).strip().rstrip("/")
        link = str(body.get("link") or "").strip().rstrip("/")
        lang = body.get("lang") if body.get("lang") in sentiment.LANGS else "de"
        require(not topic or alerts.TOPIC_RE.match(topic), "Kanalname: 8–64 Zeichen, nur Buchstaben, Ziffern, - und _")
        require(alerts.URL_RE.match(server), "ungültige ntfy-Adresse")
        require(not link or alerts.URL_RE.match(link), "ungültige Adresse des Dashboards")
        chk.store.save_settings({"topic": topic, "server": server, "link": link, "lang": lang})
    elif action == "test":
        try:
            chk.send_test()
        except Exception as e:
            raise ApiError(502, {"error": f"Senden fehlgeschlagen: {e}"})
    elif action == "check":
        chk.check()   # sofort prüfen (Button „Jetzt prüfen“)
    else:
        raise ApiError(400, {"error": "unbekannte Aktion"})
    return alerts_state()


POST_ROUTES: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "/api/alerts": api_alerts_post,
}


ROUTES: Dict[str, Callable[[Query], Dict[str, Any]]] = {
    "/api/sentiment": api_sentiment,
    "/api/markets": api_markets,
    "/api/categories": api_categories,
    "/api/search": api_search,
    "/api/watchlist": api_watchlist,
    "/api/watch-coins": api_watch_coins,
    "/api/coin": api_coin,
    "/api/coin-info": api_coin_info,
    "/api/methodology": api_methodology,
    "/api/screener": api_screener,
    "/api/klines": api_klines,
    "/api/score-history": api_score_history,
    "/api/onchain": api_onchain,
    "/api/defi": api_defi,
    "/api/fees": api_fees,
    "/api/news": api_news,
    "/api/liquidations": api_liquidations,
    "/api/derivatives": api_derivatives,
    "/api/liq-history": api_liq_history,
    "/api/liq-overview": api_liq_overview,
    "/api/liq-feed": api_liq_feed,
    "/api/alerts": api_alerts,
}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self) -> None:
        # Statische Dateien bei jedem Laden neu prüfen, damit nach Änderungen
        # kein alter Frontend-Code mit neuer API zusammentrifft
        if not self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        route = ROUTES.get(url.path)
        if route is None:
            super().do_GET()   # statische Dateien aus web/ (unbekannte /api/-Pfade → 404)
            return
        try:
            payload = route(Query(url.query, self.headers.get("Cookie", "")))
        except ApiError as e:
            self.send_json(e.payload, e.status)
            return
        self.send_json(payload)

    def do_POST(self) -> None:
        route = POST_ROUTES.get(urlparse(self.path).path)
        if route is None:
            self.send_json({"error": "nicht gefunden"}, 404)
            return
        # Nur JSON annehmen: ein fremdes Formular kann kein application/json ohne CORS-Vorabfrage schicken
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self.send_json({"error": "JSON erwartet"}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 < length <= POST_MAX_BYTES:
            self.send_json({"error": "ungültige Länge"}, 400)
            return
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
            payload = route(body)
        except ValueError:
            self.send_json({"error": "ungültiges JSON"}, 400)
            return
        except ApiError as e:
            self.send_json(e.payload, e.status)
            return
        self.send_json(payload)

    def log_message(self, fmt: str, *args) -> None:
        if "/api/" in (str(args[0]) if args else ""):
            super().log_message(fmt, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto-Sentiment-Dashboard")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="Browser nicht öffnen")
    args = parser.parse_args()

    # Echte Liquidationen von Binance, Bybit und OKX im Hintergrund mitschneiden
    global _collector
    _collector = liquidations.Collector(DATA_DIR).start()

    # On-Chain und Score-Historie (mit MVRV) für die Haupt-Coins vorladen: Coin Metrics antwortet
    # auf eine Abfrage, die es länger nicht gab, manchmal erst nach über 40 s
    def warm_onchain() -> None:
        for sym in SYMBOLS:
            get_onchain(sym)
            get_score_history(sym)
    threading.Thread(target=warm_onchain, daemon=True, name="onchain-warmup").start()

    # Alarme: Regeln prüfen alle 5 Min. mit denselben Coin-Daten wie die Watchlist-Karten (CoinGecko gebündelt)
    global _alerts
    _alerts = alerts.Checker(alerts.AlertStore(DATA_DIR / "alerts.db"),
                             lambda entries: get_watch_coins(entries)["coins"],
                             _collector.store.snapshot).start()

    # Dashboard-Daten schon beim Start laden, damit der erste Seitenaufruf nicht warten muss
    threading.Thread(target=_refresh_in_background, daemon=True, name="sentiment-warmup").start()

    # Nur lokal erreichbar
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"Dashboard läuft auf {url}  (Strg+C zum Beenden)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    # systemd beendet mit SIGTERM: wie Strg+C behandeln, damit die letzten Liquidationen gespeichert werden
    def on_sigterm(*_: Any) -> None:
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _collector.store.flush()
        print("\nBeendet.")


if __name__ == "__main__":
    main()

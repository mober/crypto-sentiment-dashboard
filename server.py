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
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

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

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,15}$")
COIN_ID_RE = re.compile(r"^[a-z0-9-]{1,100}$")  # CoinGecko-ID, z. B. "bitcoin"

DEFI_CACHE_SECONDS = 600  # DefiLlama aktualisiert TVL ohnehin nur grob stündlich; die Antwort ist ~10 MB
LLAMA_INDEX_SECONDS = 24 * 3600   # Zuordnung Coin → Protokoll ändert sich selten
FEES_CACHE_SECONDS = 1800         # DefiLlama aktualisiert Gebühren täglich
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

    def set(self, key: Any, value: Any, seconds: Optional[float] = None) -> None:
        with self._guard:
            self._entries.pop(key, None)
            self._entries[key] = (time.time() + (self.ttl if seconds is None else seconds), value)
            while len(self._entries) > self.max_entries:
                old = next(iter(self._entries))
                del self._entries[old]
                lock = self._locks.get(old)
                if lock is not None and not lock.locked():
                    del self._locks[old]

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
        self.set(key, result, seconds)
        return result

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
_search_cache = TTLCache("search", CACHE_SECONDS, max_entries=100)   # nicht stale: Suchbegriffe sind kurzlebig
_klines_cache = TTLCache("klines", KLINE_CACHE_SECONDS, max_entries=300)   # nicht stale: letzte Kerze soll aktuell sein
_derivs_cache = TTLCache("derivatives", CACHE_SECONDS, stale=True)   # Funding/OI: Stunden- bis Tageswerte
_defi_cache = TTLCache("defi", DEFI_CACHE_SECONDS, max_entries=1, stale=True)
_llama_index_cache = TTLCache("llama-index", LLAMA_INDEX_SECONDS, max_entries=1, stale=True)
_fees_cache = TTLCache("fees", FEES_CACHE_SECONDS, stale=True)   # Schlüssel: "<art>:<slug>"
_news_cache = TTLCache("news", NEWS_CACHE_SECONDS, max_entries=1, stale=True)   # alle Feeds gemeinsam
_liq_cache = TTLCache("liquidation-inputs", CACHE_SECONDS, stale=True)   # Stundenkerzen und OI
_history_cache = TTLCache("score-history", CACHE_SECONDS, stale=True)   # Score je Tag
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
                           "name": (mcaps.get(s) or {}).get("name")}
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


def get_liq_history(days: int, coin: Optional[str], exchange: Optional[str]) -> Dict[str, Any]:
    """Liquidationen über Wochen/Monate aus SQLite (Stundensummen), 60 s gecacht."""
    return _liq_history_cache.get((days, coin, exchange), lambda _: _collector.history(days, coin, exchange))


def get_payload(force: bool = False) -> Dict[str, Any]:
    """Abgelaufener Stand wird sofort geliefert und im Hintergrund erneuert,
    damit niemand beim Öffnen der Seite auf alle Abfragen warten muss."""
    return _payload_cache.get("payload", build_payload, force=force)


def get_coin(symbol: str, coin_id: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """Einzelner Coin (aus Trending oder Marktübersicht), eigener Cache je Coin."""
    global_data = get_payload()["global"]

    def load(previous: Optional[Dict[str, Any]]) -> Any:
        symbol_data = sentiment.collect_symbol(symbol, coin_id, with_market=True)
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
            "updated": datetime.now().isoformat(timespec="seconds"),
            "coins": coins,
        }

    return _markets_cache.get(f"{category}:1" if category else str(page), load)


def get_categories() -> Optional[Dict[str, Any]]:
    """Alle Sparten; eine CoinGecko-Abfrage, 5 Min. gecacht."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        cats = sentiment.safe(sentiment.get_categories)
        return {"updated": datetime.now().isoformat(timespec="seconds"), "categories": cats} if cats else None

    return _categories_cache.get("all", load)


def search_markets(query: str) -> Optional[Dict[str, Any]]:
    """Suchtreffer mit Marktdaten; je Suchbegriff 5 Min. gecacht (zwei CoinGecko-Abfragen)."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        coins = sentiment.safe(sentiment.search_markets, query)
        if coins is None:
            return None
        return {"query": query, "updated": datetime.now().isoformat(timespec="seconds"), "coins": coins}

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
        return {"updated": datetime.now().isoformat(timespec="seconds"), **result} if result else None

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


def get_news(symbol: str, name: Optional[str]) -> Optional[Dict[str, Any]]:
    """Deutschsprachige Meldungen zum Coin (RSS); die Feeds werden gemeinsam 15 Min. gecacht,
    gefiltert wird je Abruf. Bei Fehlern der letzte erfolgreiche Stand."""
    def load(_: Any) -> Optional[Dict[str, Any]]:
        items = sentiment.safe(sentiment.get_news)
        return {"items": items, "updated": datetime.now().isoformat(timespec="seconds")} if items else None

    feeds = _news_cache.get("all", load)
    if not feeds:
        return None
    return {"symbol": symbol, "name": name, "updated": feeds["updated"],
            "sources": [s for s, _ in sentiment.NEWS_FEEDS], **sentiment.match_news(feeds["items"], symbol, name)}


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
        result = sentiment.safe(sentiment.get_score_history, symbol)
        return {"symbol": symbol, **result} if result and result["days"] else None

    return _history_cache.get(symbol, load)


def get_coin_profile(symbol: str, coin_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Profil eines Coins (Seite „Über den Coin“): eine CoinGecko-Abfrage, einen Tag gecacht.
    Ohne ID wird sie einmal über das Symbol ermittelt (eine Abfrage mehr). Bei Fehlern der letzte Stand."""
    if not coin_id:
        coin_id = _profile_ids.get(symbol, lambda _: sentiment.safe(sentiment.resolve_coin_id, symbol) or None)
        if not coin_id:
            return None

    def load(_: Any) -> Optional[Dict[str, Any]]:
        profile = sentiment.safe(sentiment.get_coin_profile, coin_id)
        return {"updated": datetime.now().isoformat(timespec="seconds"), **profile} if profile else None

    return _profile_cache.get(coin_id, load)


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

    def __init__(self, query: str) -> None:
        self.params = parse_qs(query)
        self.force = self.params.get("refresh") == ["1"]

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
    return get_payload(force=q.force)


def api_markets(q: Query) -> Dict[str, Any]:
    page, category = q("page", "1"), q("category") or None
    require(page.isdigit() and 1 <= int(page) <= MARKETS_MAX_PAGE, "ungültige Seite")
    require(not category or CATEGORY_RE.match(category), "ungültige Sparte")
    return from_coingecko(get_markets(int(page), category))


def api_categories(q: Query) -> Dict[str, Any]:
    return from_coingecko(get_categories())


def api_search(q: Query) -> Dict[str, Any]:
    term = q.text("q")
    require(1 <= len(term) <= SEARCH_MAX_LEN, "Suchbegriff fehlt oder ist zu lang")
    return from_coingecko(search_markets(term))


def api_coin(q: Query) -> Dict[str, Any]:
    symbol = valid_symbol(q)
    coin_id = q.coin_id()
    require(not coin_id or COIN_ID_RE.match(coin_id), "ungültige Coin-ID")
    return get_coin(symbol, coin_id, force=q.force)


def api_coin_info(q: Query) -> Dict[str, Any]:
    return from_coingecko(get_coin_profile(*valid_symbol_and_id(q)))


def api_klines(q: Query) -> Dict[str, Any]:
    symbol, interval = q.symbol(), q("interval", "1d")
    require(SYMBOL_RE.match(symbol) and interval in KLINE_INTERVALS, "ungültiges Symbol oder Intervall")
    return found(get_klines(symbol, interval), f"Keine Binance-Kursdaten für {symbol}/USDT")


def api_score_history(q: Query) -> Dict[str, Any]:
    symbol = valid_symbol(q)
    return found(get_score_history(symbol), f"Kein Score-Verlauf für {symbol} (keine Binance-Daten)")


def api_defi(q: Query) -> Dict[str, Any]:
    return found(get_defi_tvl(), "DefiLlama nicht erreichbar", 502)


def api_fees(q: Query) -> Dict[str, Any]:
    symbol, coin_id = valid_symbol_and_id(q)
    return found(get_fees(symbol, coin_id), f"Keine Gebührendaten für {symbol} bei DefiLlama")


def api_news(q: Query) -> Dict[str, Any]:
    symbol, name = q.symbol(), q.text("name") or None
    require(SYMBOL_RE.match(symbol) and not (name and not NAME_RE.match(name)),
            "ungültiges Symbol oder ungültiger Name")
    return found(get_news(symbol, name), "News-Feeds nicht erreichbar", 502)


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
    return get_liq_history(days, coin, exchange)


def api_liq_overview(q: Query) -> Dict[str, Any]:
    col = collector()
    window = q("window", "24h")
    require(window in liquidations.WINDOWS, "ungültiges Zeitfenster")
    return col.overview(window)


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
    return col.feed(coin, exchange, min_usd, limit)


ROUTES: Dict[str, Callable[[Query], Dict[str, Any]]] = {
    "/api/sentiment": api_sentiment,
    "/api/markets": api_markets,
    "/api/categories": api_categories,
    "/api/search": api_search,
    "/api/coin": api_coin,
    "/api/coin-info": api_coin_info,
    "/api/klines": api_klines,
    "/api/score-history": api_score_history,
    "/api/defi": api_defi,
    "/api/fees": api_fees,
    "/api/news": api_news,
    "/api/liquidations": api_liquidations,
    "/api/derivatives": api_derivatives,
    "/api/liq-history": api_liq_history,
    "/api/liq-overview": api_liq_overview,
    "/api/liq-feed": api_liq_feed,
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
            payload = route(Query(url.query))
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

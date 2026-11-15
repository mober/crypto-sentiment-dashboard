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
import re
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

import sentiment

SYMBOLS = ["BTC", "ETH", "SOL"]
CACHE_SECONDS = 300
MARKETS_PER_PAGE = 50
MARKETS_MAX_PAGE = 20     # Top 1000, wie bei Cryptobubbles
KLINE_INTERVALS = {"1d", "4h", "1h"}
KLINE_CACHE_SECONDS = 60  # Binance ist großzügig limitiert; die letzte Kerze soll aktuell sein
STATIC_DIR = Path(__file__).resolve().parent / "web"

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,15}$")
COIN_ID_RE = re.compile(r"^[a-z0-9-]{1,100}$")  # CoinGecko-ID, z. B. "bitcoin"

_cache: Dict[str, Any] = {"time": 0.0, "payload": None}
_lock = threading.Lock()
_coin_cache: Dict[str, Dict[str, Any]] = {}
_coin_lock = threading.Lock()
_markets_cache: Dict[str, Dict[str, Any]] = {}   # Schlüssel: Seite oder "<sparte>:1"
_markets_lock = threading.Lock()
_klines_cache: Dict[str, Dict[str, Any]] = {}
_klines_lock = threading.Lock()
_derivs_cache: Dict[str, Dict[str, Any]] = {}
_derivs_lock = threading.Lock()
_defi_cache: Dict[str, Any] = {"time": 0.0, "payload": None}
_defi_lock = threading.Lock()
DEFI_CACHE_SECONDS = 600  # DefiLlama aktualisiert TVL ohnehin nur grob stündlich; die Antwort ist ~10 MB
_llama_index: Dict[str, Any] = {"time": 0.0, "payload": None}
_llama_index_lock = threading.Lock()
LLAMA_INDEX_SECONDS = 24 * 3600   # Zuordnung Coin → Protokoll ändert sich selten
_fees_cache: Dict[str, Dict[str, Any]] = {}
_fees_lock = threading.Lock()
FEES_CACHE_SECONDS = 1800         # DefiLlama aktualisiert Gebühren täglich
RETRY_SECONDS = 30                # unvollständige Ergebnisse (Abfrage fehlgeschlagen) nur so lange cachen
_liq_cache: Dict[str, Dict[str, Any]] = {}
_liq_lock = threading.Lock()
_history_cache: Dict[str, Dict[str, Any]] = {}
_history_lock = threading.Lock()
_categories_cache: Dict[str, Any] = {"time": 0.0, "payload": None}
_categories_lock = threading.Lock()
CATEGORY_PER_PAGE = 100   # Coins einer Sparte: eine Seite, die größten 100
CATEGORY_RE = re.compile(r"^[a-z0-9-]{1,100}$")
_search_cache: Dict[str, Dict[str, Any]] = {}
_search_lock = threading.Lock()
SEARCH_MAX_LEN = 40


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
    }


def build_payload() -> Dict[str, Any]:
    with ThreadPoolExecutor(max_workers=len(SYMBOLS) + 2) as pool:
        global_future = pool.submit(sentiment.collect_global)
        mcap_future = pool.submit(sentiment.safe, sentiment.get_market_caps, SYMBOLS)
        symbol_futures = {s: pool.submit(sentiment.collect_symbol, s) for s in SYMBOLS}
        global_data = global_future.result()
        mcaps = mcap_future.result() or {}
        symbol_data = {s: {**f.result(), "market_cap": (mcaps.get(s) or {}).get("market_cap"),
                           "market_stats": (mcaps.get(s) or {}).get("market_stats")}
                       for s, f in symbol_futures.items()}

    # Fehlgeschlagene Abfragen (z. B. Rate-Limit) durch den letzten erfolgreichen Wert ersetzen
    previous = _cache["payload"]
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
        "updated": datetime.now().isoformat(timespec="seconds"),
        "global": global_data,
        "coins": coins,
    }


def get_payload(force: bool = False) -> Dict[str, Any]:
    with _lock:
        if force or _cache["payload"] is None or time.time() - _cache["time"] > CACHE_SECONDS:
            _cache["payload"] = build_payload()
            _cache["time"] = time.time()
        return _cache["payload"]


def get_coin(symbol: str, coin_id: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    """Einzelner Coin (aus Trending oder Marktübersicht), eigener Cache je Coin."""
    global_data = get_payload()["global"]
    key = f"{symbol}:{coin_id or ''}"
    with _coin_lock:
        entry = _coin_cache.get(key)
        if force or entry is None or time.time() - entry["time"] > CACHE_SECONDS:
            symbol_data = sentiment.collect_symbol(symbol, coin_id, with_market=True)
            # CoinGecko-Abfrage fehlgeschlagen (z. B. Limit-Pause): Lücken aus dem letzten Stand
            # füllen und bald erneut versuchen, statt das unvollständige Ergebnis 5 Min. zu behalten
            incomplete = symbol_data.get("market_stats") is None
            if entry:
                for k, old in entry["coin"]["data"].items():
                    if symbol_data.get(k) is None:
                        symbol_data[k] = old
            cached_at = time.time() - (CACHE_SECONDS - RETRY_SECONDS if incomplete else 0)
            entry = {"time": cached_at, "coin": build_coin(symbol, global_data, symbol_data)}
            _coin_cache[key] = entry
        return entry["coin"]


def get_markets(page: int, category: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Eine Seite der Rangliste (oder die größten Coins einer Sparte);
    bei Fehlern die zuletzt erfolgreich geladene Fassung."""
    key = f"{category}:1" if category else str(page)
    with _markets_lock:
        entry = _markets_cache.get(key)
        if entry is None or time.time() - entry["time"] > CACHE_SECONDS:
            if category:
                coins = sentiment.safe(sentiment.get_markets, 1, CATEGORY_PER_PAGE, category)
            else:
                coins = sentiment.safe(sentiment.get_markets, page, MARKETS_PER_PAGE)
            if coins is not None:
                entry = {"time": time.time(), "payload": {
                    "page": 1 if category else page,
                    "per_page": CATEGORY_PER_PAGE if category else MARKETS_PER_PAGE,
                    "max_page": 1 if category else MARKETS_MAX_PAGE,
                    "category": category,
                    "updated": datetime.now().isoformat(timespec="seconds"),
                    "coins": coins,
                }}
                _markets_cache[key] = entry
        return entry["payload"] if entry else None


def get_categories() -> Optional[Dict[str, Any]]:
    """Alle Sparten; eine CoinGecko-Abfrage, 5 Min. gecacht."""
    with _categories_lock:
        if _categories_cache["payload"] is None or time.time() - _categories_cache["time"] > CACHE_SECONDS:
            cats = sentiment.safe(sentiment.get_categories)
            if cats:
                _categories_cache.update(time=time.time(), payload={
                    "updated": datetime.now().isoformat(timespec="seconds"),
                    "categories": cats,
                })
        return _categories_cache["payload"]


def search_markets(query: str) -> Optional[Dict[str, Any]]:
    """Suchtreffer mit Marktdaten; je Suchbegriff 5 Min. gecacht (zwei CoinGecko-Abfragen)."""
    key = query.lower()
    with _search_lock:
        entry = _search_cache.get(key)
        if entry is None or time.time() - entry["time"] > CACHE_SECONDS:
            coins = sentiment.safe(sentiment.search_markets, query)
            if coins is not None:
                entry = {"time": time.time(), "payload": {
                    "query": query,
                    "updated": datetime.now().isoformat(timespec="seconds"),
                    "coins": coins,
                }}
                _search_cache[key] = entry
                # Cache klein halten: abgelaufene Einträge entfernen
                for k in [k for k, v in _search_cache.items() if time.time() - v["time"] > CACHE_SECONDS]:
                    del _search_cache[k]
        return entry["payload"] if entry else None


def get_klines(symbol: str, interval: str) -> Optional[Dict[str, Any]]:
    """Kerzen für das Chart (Spot, sonst Futures); None, wenn Binance kein USDT-Paar hat."""
    key = f"{symbol}:{interval}"
    with _klines_lock:
        entry = _klines_cache.get(key)
        if entry is None or time.time() - entry["time"] > KLINE_CACHE_SECONDS:
            result = sentiment.safe(sentiment.get_klines, symbol, interval)
            if result and result["candles"]:
                entry = {"time": time.time(), "payload": {
                    "symbol": symbol,
                    "interval": interval,
                    "source": result["source"],
                    "candles": result["candles"],
                }}
                _klines_cache[key] = entry
        return entry["payload"] if entry else None


def get_derivatives(symbol: str) -> Optional[Dict[str, Any]]:
    """Funding- und Open-Interest-Verlauf (Binance Futures); None ohne USDT-Perpetual."""
    with _derivs_lock:
        entry = _derivs_cache.get(symbol)
        if entry is None or time.time() - entry["time"] > CACHE_SECONDS:
            funding = sentiment.safe(sentiment.get_funding_history, symbol)
            oi = sentiment.safe(sentiment.get_open_interest_history, symbol)
            if funding or oi:
                entry = {"time": time.time(), "payload": {
                    "symbol": symbol,
                    "funding": funding or [],
                    "open_interest": oi or [],
                }}
                _derivs_cache[symbol] = entry
        return entry["payload"] if entry else None


def get_defi_tvl() -> Optional[Dict[str, Any]]:
    """TVL je Kategorie und Chain (DefiLlama); bei Fehlern der letzte erfolgreiche Stand."""
    with _defi_lock:
        if _defi_cache["payload"] is None or time.time() - _defi_cache["time"] > DEFI_CACHE_SECONDS:
            result = sentiment.safe(sentiment.get_defi_tvl)
            if result:
                _defi_cache.update(time=time.time(), payload={
                    "updated": datetime.now().isoformat(timespec="seconds"), **result})
        return _defi_cache["payload"]


def get_fees(symbol: str, coin_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Gebühren und Einnahmen (DefiLlama) eines Coins; zugeordnet über CoinGecko-ID, sonst Symbol."""
    with _llama_index_lock:
        if _llama_index["payload"] is None or time.time() - _llama_index["time"] > LLAMA_INDEX_SECONDS:
            idx = sentiment.safe(sentiment.get_llama_index)
            if idx:
                _llama_index.update(time=time.time(), payload=idx)
        idx = _llama_index["payload"]
    if not idx:
        return None
    entry = (idx["gecko"].get(coin_id) if coin_id else None) or idx["symbol"].get(symbol)
    if not entry:
        return None
    key = entry["kind"] + ":" + entry["slug"]
    with _fees_lock:
        cached = _fees_cache.get(key)
        if cached is None or time.time() - cached["time"] > FEES_CACHE_SECONDS:
            try:
                result = sentiment.get_fees(entry)
            except ValueError as exc:   # DefiLlama nicht erreichbar: letzten Stand behalten, nichts cachen
                print(f"  [warn] get_fees: {exc}")
                return cached["payload"] if cached else None
            # Teilergebnis (eine Datenart fehlgeschlagen) nur kurz cachen
            incomplete = bool(result and result.pop("incomplete", False))
            cached = {"time": time.time() - (FEES_CACHE_SECONDS - RETRY_SECONDS if incomplete else 0), "payload": result}
            _fees_cache[key] = cached
        return cached["payload"]


def get_liquidation_inputs(symbol: str) -> Optional[Dict[str, Any]]:
    """Kerzen und OI je Stunde (Binance Futures) für die Liquidations-Heatmap; 5 Min. gecacht."""
    with _liq_lock:
        entry = _liq_cache.get(symbol)
        if entry is None or time.time() - entry["time"] > CACHE_SECONDS:
            result = sentiment.safe(sentiment.get_liquidation_inputs, symbol)
            if result and result["candles"] and result["open_interest"]:
                entry = {"time": time.time(), "payload": {"symbol": symbol, **result}}
                _liq_cache[symbol] = entry
        return entry["payload"] if entry else None


def get_score_history(symbol: str) -> Optional[Dict[str, Any]]:
    """Rückwirkend berechneter Score je Tag; None ohne Binance-Kerzen und -Futures."""
    with _history_lock:
        entry = _history_cache.get(symbol)
        if entry is None or time.time() - entry["time"] > CACHE_SECONDS:
            result = sentiment.safe(sentiment.get_score_history, symbol)
            if result and result["days"]:
                entry = {"time": time.time(), "payload": {"symbol": symbol, **result}}
                _history_cache[symbol] = entry
        return entry["payload"] if entry else None


def coingecko_error() -> Dict[str, Any]:
    """Fehlerantwort für CoinGecko-Ausfälle, mit Wartezeit, wenn gerade eine Limit-Pause läuft."""
    wait = sentiment.coingecko_retry_in()
    if wait > 0:
        return {"error": f"CoinGecko-Limit erreicht (kostenlose API), neuer Versuch in {round(wait)} s",
                "retry_in": round(wait) + 1}
    return {"error": "CoinGecko nicht erreichbar"}


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
        query = parse_qs(url.query)
        force = query.get("refresh") == ["1"]
        if url.path == "/api/sentiment":
            self.send_json(get_payload(force=force))
            return
        if url.path == "/api/markets":
            page = (query.get("page") or ["1"])[0]
            category = (query.get("category") or [""])[0] or None
            if not page.isdigit() or not 1 <= int(page) <= MARKETS_MAX_PAGE:
                self.send_json({"error": "ungültige Seite"}, 400)
                return
            if category and not CATEGORY_RE.match(category):
                self.send_json({"error": "ungültige Sparte"}, 400)
                return
            payload = get_markets(int(page), category)
            if payload is None:
                self.send_json(coingecko_error(), 502)
                return
            self.send_json(payload)
            return
        if url.path == "/api/categories":
            payload = get_categories()
            if payload is None:
                self.send_json(coingecko_error(), 502)
                return
            self.send_json(payload)
            return
        if url.path == "/api/search":
            q = " ".join((query.get("q") or [""])[0].split())
            if not 1 <= len(q) <= SEARCH_MAX_LEN:
                self.send_json({"error": "Suchbegriff fehlt oder ist zu lang"}, 400)
                return
            payload = search_markets(q)
            if payload is None:
                self.send_json(coingecko_error(), 502)
                return
            self.send_json(payload)
            return
        if url.path == "/api/coin":
            symbol = (query.get("symbol") or [""])[0].upper()
            if not SYMBOL_RE.match(symbol):
                self.send_json({"error": "ungültiges Symbol"}, 400)
                return
            coin_id = (query.get("id") or [""])[0] or None
            if coin_id and not COIN_ID_RE.match(coin_id):
                self.send_json({"error": "ungültige Coin-ID"}, 400)
                return
            self.send_json(get_coin(symbol, coin_id, force=force))
            return
        if url.path == "/api/klines":
            symbol = (query.get("symbol") or [""])[0].upper()
            interval = (query.get("interval") or ["1d"])[0]
            if not SYMBOL_RE.match(symbol) or interval not in KLINE_INTERVALS:
                self.send_json({"error": "ungültiges Symbol oder Intervall"}, 400)
                return
            payload = get_klines(symbol, interval)
            if payload is None:
                self.send_json({"error": f"Keine Binance-Kursdaten für {symbol}/USDT"}, 404)
                return
            self.send_json(payload)
            return
        if url.path == "/api/score-history":
            symbol = (query.get("symbol") or [""])[0].upper()
            if not SYMBOL_RE.match(symbol):
                self.send_json({"error": "ungültiges Symbol"}, 400)
                return
            payload = get_score_history(symbol)
            if payload is None:
                self.send_json({"error": f"Kein Score-Verlauf für {symbol} (keine Binance-Daten)"}, 404)
                return
            self.send_json(payload)
            return
        if url.path == "/api/defi":
            payload = get_defi_tvl()
            if payload is None:
                self.send_json({"error": "DefiLlama nicht erreichbar"}, 502)
                return
            self.send_json(payload)
            return
        if url.path == "/api/fees":
            symbol = (query.get("symbol") or [""])[0].upper()
            coin_id = (query.get("id") or [""])[0] or None
            if not SYMBOL_RE.match(symbol) or (coin_id and not COIN_ID_RE.match(coin_id)):
                self.send_json({"error": "ungültiges Symbol oder ungültige Coin-ID"}, 400)
                return
            payload = get_fees(symbol, coin_id)
            if payload is None:
                self.send_json({"error": f"Keine Gebührendaten für {symbol} bei DefiLlama"}, 404)
                return
            self.send_json(payload)
            return
        if url.path == "/api/liquidations":
            symbol = (query.get("symbol") or [""])[0].upper()
            if not SYMBOL_RE.match(symbol):
                self.send_json({"error": "ungültiges Symbol"}, 400)
                return
            payload = get_liquidation_inputs(symbol)
            if payload is None:
                self.send_json({"error": f"Keine Binance-Futures für {symbol}/USDT"}, 404)
                return
            self.send_json(payload)
            return
        if url.path == "/api/derivatives":
            symbol = (query.get("symbol") or [""])[0].upper()
            if not SYMBOL_RE.match(symbol):
                self.send_json({"error": "ungültiges Symbol"}, 400)
                return
            payload = get_derivatives(symbol)
            if payload is None:
                self.send_json({"error": f"Keine Binance-Futures für {symbol}/USDT"}, 404)
                return
            self.send_json(payload)
            return
        super().do_GET()

    def log_message(self, fmt: str, *args) -> None:
        if "/api/" in (str(args[0]) if args else ""):
            super().log_message(fmt, *args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto-Sentiment-Dashboard")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="Browser nicht öffnen")
    args = parser.parse_args()

    # Nur lokal erreichbar
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"Dashboard läuft auf {url}  (Strg+C zum Beenden)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")


if __name__ == "__main__":
    main()

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
from typing import Any, Dict
from urllib.parse import parse_qs, urlparse

import sentiment

SYMBOLS = ["BTC", "ETH", "SOL"]
CACHE_SECONDS = 300
STATIC_DIR = Path(__file__).resolve().parent / "web"

SYMBOL_RE = re.compile(r"^[A-Z0-9]{1,15}$")

_cache: Dict[str, Any] = {"time": 0.0, "payload": None}
_lock = threading.Lock()
_coin_cache: Dict[str, Dict[str, Any]] = {}
_coin_lock = threading.Lock()


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
        symbol_data = {s: {**f.result(), "market_cap": mcaps.get(s)} for s, f in symbol_futures.items()}

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


def get_coin(symbol: str, force: bool = False) -> Dict[str, Any]:
    """Einzelner Coin (z. B. aus den Trending Coins), eigener Cache je Symbol."""
    global_data = get_payload()["global"]
    with _coin_lock:
        entry = _coin_cache.get(symbol)
        if force or entry is None or time.time() - entry["time"] > CACHE_SECONDS:
            symbol_data = sentiment.collect_symbol(symbol)
            # Marktkapitalisierung aus den Trending-Daten, spart eine CoinGecko-Abfrage
            trend = next((t for t in global_data["trending"] or [] if t["symbol"] == symbol), None)
            symbol_data["market_cap"] = (
                {"usd": trend["market_cap"], "rank": trend["market_cap_rank"]}
                if trend and trend.get("market_cap") else None
            )
            entry = {"time": time.time(), "coin": build_coin(symbol, global_data, symbol_data)}
            _coin_cache[symbol] = entry
        return entry["coin"]


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
        if url.path == "/api/coin":
            symbol = (query.get("symbol") or [""])[0].upper()
            if not SYMBOL_RE.match(symbol):
                self.send_json({"error": "ungültiges Symbol"}, 400)
                return
            self.send_json(get_coin(symbol, force=force))
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

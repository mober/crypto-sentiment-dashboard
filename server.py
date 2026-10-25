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
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

import sentiment

SYMBOLS = ["BTC", "ETH", "SOL"]
CACHE_SECONDS = 300
STATIC_DIR = Path(__file__).resolve().parent / "web"

_cache: Dict[str, Any] = {"time": 0.0, "payload": None}
_lock = threading.Lock()


def build_payload() -> Dict[str, Any]:
    with ThreadPoolExecutor(max_workers=len(SYMBOLS) + 1) as pool:
        global_future = pool.submit(sentiment.collect_global)
        symbol_futures = {s: pool.submit(sentiment.collect_symbol, s) for s in SYMBOLS}
        global_data = global_future.result()
        symbol_data = {s: f.result() for s, f in symbol_futures.items()}

    coins = []
    for symbol in SYMBOLS:
        data = {"fear_greed": global_data["fear_greed"], **symbol_data[symbol]}
        signals = sentiment.score(data)
        total = sum(s["points"] for s in signals)
        coins.append({
            "symbol": symbol,
            "data": symbol_data[symbol],
            "signals": signals,
            "score": total,
            "max_score": len(signals),
            "verdict": sentiment.verdict(total, len(signals)),
        })

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


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def do_GET(self) -> None:
        if self.path.startswith("/api/sentiment"):
            payload = get_payload(force="refresh=1" in self.path)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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

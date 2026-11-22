#!/usr/bin/env python3
"""Echte Liquidationen von Binance, Bybit und OKX, live über deren öffentliche WebSockets.

Jede Börse läuft in einem eigenen Thread und meldet Zwangsschließungen an einen gemeinsamen
Speicher (24 h im Arbeitsspeicher für die Live-Ansicht, dauerhaft in SQLite data/liquidations.db
mit Stundensummen für lange Zeiträume). Nur Python-Standardbibliothek; der
WebSocket-Client unten kann genau das, was die drei Streams brauchen (TLS, Text-Frames,
Ping/Pong, Fragmente).

Einschränkungen (auf der Seite erklärt):
- Binance meldet je Symbol höchstens die größte Liquidation pro Sekunde, die Summen sind dort
  also eher zu niedrig.
- Hyperliquid und kleinere Börsen fehlen; die Summen liegen deshalb unter denen von CoinGlass.

Test im Terminal:
  python3 liquidations.py --seconds 60
"""

import base64
import json
import os
import re
import socket
import sqlite3
import ssl
import struct
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

KEEP_SECONDS = 24 * 3600
WINDOWS = {"1h": 3600, "4h": 4 * 3600, "12h": 12 * 3600, "24h": 24 * 3600}
EXCHANGES = ["Binance", "Bybit", "OKX"]
USER_AGENT = "crypto-dashboard/1.0"

# Ereignis: (Zeit in s, Börse, Coin, "long"/"short", Betrag in USD, Preis, Symbol der Börse)
Event = Tuple[float, str, str, str, float, float, str]

_QUOTES = ("USDT", "USDC", "BUSD", "FDUSD", "USD")
_MULTIPLIER = re.compile(r"^(1000000|100000|10000|1000|100|1M)(?=[A-Z])")


def base_coin(symbol: str) -> str:
    """BTCUSDT → BTC, 1000PEPEUSDT → PEPE, BTC-USDT → BTC, BTCUSD_PERP → BTC."""
    s = symbol.upper().split("-")[0].split("_")[0]
    for q in _QUOTES:
        if s.endswith(q) and len(s) > len(q):
            s = s[: -len(q)]
            break
    return _MULTIPLIER.sub("", s) or s


# ---------------------------------------------------------------- WebSocket-Client

class WebSocket:
    """Minimaler WebSocket-Client (RFC 6455) über TLS."""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        u = urllib.parse.urlparse(url)
        host, port = u.hostname or "", u.port or 443
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        raw = socket.create_connection((host, port), timeout=15)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: {USER_AGENT}\r\n\r\n"
        ).encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("Verbindung beim Handshake geschlossen")
            buf += chunk
        head, self.buf = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0]
        if b" 101" not in status:
            raise ConnectionError(f"Handshake abgelehnt: {status.decode(errors='replace')}")
        self.sock.settimeout(timeout)
        self._send_lock = threading.Lock()
        self._fragments: List[bytes] = []
        self.last_send = time.time()

    def send(self, data: Any, opcode: int = 1) -> None:
        payload = data.encode() if isinstance(data, str) else bytes(data)
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 65536:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._send_lock:
            self.sock.sendall(header + mask + masked)
            self.last_send = time.time()

    def _frame(self) -> Optional[Tuple[bool, int, bytes]]:
        """Nimmt einen vollständigen Frame aus dem Puffer, sonst None (Puffer bleibt unverändert)."""
        b = self.buf
        if len(b) < 2:
            return None
        fin, op, masked, n = b[0] & 0x80, b[0] & 0x0F, b[1] & 0x80, b[1] & 0x7F
        pos = 2
        if n == 126:
            if len(b) < 4:
                return None
            n, pos = struct.unpack("!H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, pos = struct.unpack("!Q", b[2:10])[0], 10
        mask = b""
        if masked:
            if len(b) < pos + 4:
                return None
            mask, pos = b[pos:pos + 4], pos + 4
        if len(b) < pos + n:
            return None
        data = b[pos:pos + n]
        if mask:
            data = bytes(x ^ mask[i % 4] for i, x in enumerate(data))
        self.buf = b[pos + n:]
        return bool(fin), op, data

    def recv(self) -> Optional[str]:
        """Nächste Textnachricht; None, wenn innerhalb des Timeouts nichts kam."""
        while True:
            frame = self._frame()
            if frame is None:
                try:
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    return None
                if not chunk:
                    raise ConnectionError("Verbindung geschlossen")
                self.buf += chunk
                continue
            fin, op, data = frame
            if op == 9:          # Ping → Pong
                self.send(data, 10)
            elif op == 10:       # Pong
                pass
            elif op == 8:
                raise ConnectionError("vom Server geschlossen")
            else:                # Text, Binär oder Fortsetzung
                self._fragments.append(data)
                if fin:
                    msg = b"".join(self._fragments)
                    self._fragments = []
                    return msg.decode("utf-8", "replace")

    def close(self) -> None:
        try:
            self.send(b"", 8)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _get_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- Speicher

class Store:
    """Die letzten 24 h im Arbeitsspeicher (für Übersicht und Feed), alles zusätzlich in SQLite.

    SQLite-Tabellen:
      events  – jede einzelne Liquidation (bleibt dauerhaft)
      hourly  – Summen je Stunde, Börse und Coin (für lange Zeiträume, schnell abfragbar)
      meta    – z. B. "since" = Beginn der Aufzeichnung
    Geschrieben wird gesammelt alle paar Sekunden (flush) aus einem eigenen Thread.
    """

    def __init__(self, path: Optional[Path]) -> None:
        self.path = path
        self.events: Deque[Event] = deque()
        self.lock = threading.Lock()
        self.started = time.time()
        self.first_seen = self.started   # Beginn der Aufzeichnung (für die 24-h-Abdeckung)
        self.recorded_since = self.started   # Beginn der dauerhaften Aufzeichnung
        self._db: Optional[sqlite3.Connection] = None
        self._db_lock = threading.Lock()
        self._pending: List[Event] = []
        # Schon gesehene Ereignisse: OKX schickt beim (Neu-)Verbinden die letzten Liquidationen erneut
        self._keys: set = set()
        self._key_order: Deque[tuple] = deque()
        if path:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._open()
                self._import_jsonl()
                self._load()
            except (OSError, sqlite3.Error) as exc:
                # Kein beschreibbares Verzeichnis: nur im Speicher sammeln, der Server läuft trotzdem
                print(f"Liquidationen werden nicht gespeichert ({exc}); nur im Arbeitsspeicher.")
                self.path, self._db = None, None

    # --- Datenbank

    def _open(self) -> None:
        db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        db.execute("PRAGMA journal_mode=WAL")      # Lesen (History-Abfragen) blockiert das Schreiben nicht
        db.execute("PRAGMA synchronous=NORMAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS events (
                t REAL NOT NULL, exchange TEXT NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL,
                usd REAL NOT NULL, price REAL, symbol TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_t ON events (t);
            CREATE INDEX IF NOT EXISTS events_usd ON events (usd);
            CREATE TABLE IF NOT EXISTS hourly (
                hour INTEGER NOT NULL, exchange TEXT NOT NULL, coin TEXT NOT NULL,
                long_usd REAL NOT NULL DEFAULT 0, short_usd REAL NOT NULL DEFAULT 0,
                long_n INTEGER NOT NULL DEFAULT 0, short_n INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (hour, exchange, coin));
            CREATE INDEX IF NOT EXISTS hourly_coin ON hourly (coin, hour);
            CREATE TABLE IF NOT EXISTS daily (
                day INTEGER NOT NULL, exchange TEXT NOT NULL, coin TEXT NOT NULL,
                long_usd REAL NOT NULL DEFAULT 0, short_usd REAL NOT NULL DEFAULT 0,
                long_n INTEGER NOT NULL DEFAULT 0, short_n INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (day, exchange, coin));
            CREATE INDEX IF NOT EXISTS daily_coin ON daily (coin, day);
            CREATE TABLE IF NOT EXISTS hourly_ex (
                hour INTEGER NOT NULL, exchange TEXT NOT NULL,
                long_usd REAL NOT NULL DEFAULT 0, short_usd REAL NOT NULL DEFAULT 0,
                long_n INTEGER NOT NULL DEFAULT 0, short_n INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (hour, exchange));
            CREATE INDEX IF NOT EXISTS events_coin_usd ON events (coin, usd);
            CREATE INDEX IF NOT EXISTS events_exchange_usd ON events (exchange, usd);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        row = db.execute("SELECT value FROM meta WHERE key = 'since'").fetchone()
        if row:
            self.recorded_since = float(row[0])
        else:
            db.execute("INSERT INTO meta VALUES ('since', ?)", (str(self.started),))
        db.commit()
        self._db = db

    # Summentabellen: Name → (Schlüsselspalten, Schlüssel aus einem Ereignis)
    _ROLLUPS = {
        "hourly": ("hour, exchange, coin", lambda e: (int(e[0] // 3600) * 3600, e[1], e[2])),
        "daily": ("day, exchange, coin", lambda e: (int(e[0] // 86400) * 86400, e[1], e[2])),   # UTC-Tage
        "hourly_ex": ("hour, exchange", lambda e: (int(e[0] // 3600) * 3600, e[1])),
    }

    def _write(self, events: List[Event]) -> None:
        if not events or not self._db:
            return
        with self._db_lock:
            self._db.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)", events)
            for table, (cols, key) in self._ROLLUPS.items():
                sums: Dict[tuple, List[float]] = {}
                for e in events:
                    v = sums.setdefault(key(e), [0.0, 0.0, 0, 0])
                    if e[3] == "long":
                        v[0] += e[4]; v[2] += 1
                    else:
                        v[1] += e[4]; v[3] += 1
                marks = ", ".join("?" * (cols.count(",") + 5))
                self._db.executemany(f"""
                    INSERT INTO {table} VALUES ({marks})
                    ON CONFLICT ({cols}) DO UPDATE SET
                      long_usd = long_usd + excluded.long_usd, short_usd = short_usd + excluded.short_usd,
                      long_n = long_n + excluded.long_n, short_n = short_n + excluded.short_n
                """, [(*k, *v) for k, v in sums.items()])
            self._db.commit()

    def flush(self) -> None:
        """Gesammelte Ereignisse in die Datenbank schreiben."""
        with self.lock:
            pending, self._pending = self._pending, []
        try:
            self._write(pending)
        except sqlite3.Error as exc:
            print(f"Liquidationen konnten nicht gespeichert werden: {exc}")
            with self.lock:
                self._pending[:0] = pending   # beim nächsten Mal erneut versuchen

    def _import_jsonl(self) -> None:
        """Einmalig: bisherige JSON-Zeilen-Datei (Version vor SQLite) übernehmen."""
        old = self.path.with_name("liquidations.jsonl")
        if not old.exists():
            return
        events, seen = [], set()
        with open(old, encoding="utf-8") as f:
            for line in f:
                try:
                    e = tuple(json.loads(line))
                except ValueError:
                    continue
                key = (round(e[0], 3), e[1], e[6], round(e[4], 2)) if len(e) == 7 else None
                if key and key not in seen:
                    seen.add(key)
                    events.append(e)
        events.sort()
        newest = self._db.execute("SELECT MAX(t) FROM events").fetchone()[0] or 0
        self._write([e for e in events if e[0] > newest])
        since_file = old.with_suffix(".since")
        try:
            since = float(since_file.read_text())
        except (OSError, ValueError):
            since = self.recorded_since
        if events:
            since = min(since, events[0][0])
        if since < self.recorded_since:
            self.recorded_since = since
            with self._db_lock:
                self._db.execute("UPDATE meta SET value = ? WHERE key = 'since'", (str(since),))
                self._db.commit()
        old.rename(old.with_suffix(".jsonl.imported"))
        try:
            since_file.unlink()
        except OSError:
            pass
        print(f"{len(events)} Liquidationen aus {old.name} in die Datenbank übernommen.")

    def _load(self) -> None:
        cutoff = time.time() - KEEP_SECONDS
        with self._db_lock:
            rows = self._db.execute("SELECT * FROM events WHERE t >= ? ORDER BY t", (cutoff,)).fetchall()
        for e in rows:
            if self._remember(e):
                self.events.append(e)
        # Abdeckung der 24 h: seit Beginn der Aufzeichnung (Ausfälle dazwischen werden nicht erkannt)
        self.first_seen = max(self.recorded_since, cutoff)

    def history(self, days: int, coin: Optional[str], exchange: Optional[str]) -> Dict[str, Any]:
        """Summen je Stunde für lange Zeiträume, dazu Coins, Börsen und größte Liquidationen im Zeitraum.

        Für Tempo bei einem Jahr: Zeitreihe ohne Coin-Filter aus hourly_ex (wenige Zeilen je Stunde),
        Summen je Coin/Börse aus daily für ganze Tage plus hourly für den angebrochenen ersten Tag."""
        if not self.path:
            return {"hours": [], "coins": [], "exchanges": [], "largest": [], "since": self.recorded_since}
        self.flush()
        since = time.time() - days * 86400
        h0 = int(since // 3600) * 3600
        d0 = -(-h0 // 86400) * 86400          # erster ganzer (UTC-)Tag im Zeitraum
        flt, fargs = "", []
        if coin:
            flt += " AND coin = ?"; fargs.append(coin)
        if exchange:
            flt += " AND exchange = ?"; fargs.append(exchange)
        sums = "SUM(long_usd), SUM(short_usd), SUM(long_n + short_n)"
        # Eigene Verbindung nur zum Lesen: läuft parallel zum Schreiben (WAL)
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=30)
        try:
            series_table = "hourly" if coin else "hourly_ex"
            hours = db.execute(f"SELECT hour, {sums} FROM {series_table} WHERE hour >= ?{flt} GROUP BY hour ORDER BY hour",
                               [h0, *fargs]).fetchall()

            def grouped(col: str) -> List[tuple]:
                # ganze Tage aus daily, Rest des ersten Tages aus hourly
                return db.execute(f"""
                    SELECT {col}, SUM(l), SUM(s), SUM(n) FROM (
                      SELECT {col}, SUM(long_usd) l, SUM(short_usd) s, SUM(long_n + short_n) n
                        FROM daily WHERE day >= ?{flt} GROUP BY {col}
                      UNION ALL
                      SELECT {col}, SUM(long_usd), SUM(short_usd), SUM(long_n + short_n)
                        FROM hourly WHERE hour >= ? AND hour < ?{flt} GROUP BY {col})
                    GROUP BY {col} ORDER BY SUM(l) + SUM(s) DESC""", [d0, *fargs, h0, d0, *fargs]).fetchall()

            coins = grouped("coin")
            exchanges = grouped("exchange")
            largest = db.execute(f"SELECT * FROM events WHERE t >= ?{flt} ORDER BY usd DESC LIMIT 10",
                                 [since, *fargs]).fetchall()
        finally:
            db.close()
        return {
            "hours": [{"t": h, "long": l, "short": s, "count": n} for h, l, s, n in hours],
            "coins": [{"coin": c, "long": l, "short": s, "count": n, "total": l + s} for c, l, s, n in coins[:15]],
            "coin_count": len(coins),
            "exchanges": [{"name": x, "long": l, "short": s, "count": n, "total": l + s} for x, l, s, n in exchanges],
            "largest": [_event_json(e) for e in largest],
            "since": self.recorded_since,
        }

    # --- Arbeitsspeicher

    def _remember(self, e: Event) -> bool:
        """False, wenn das Ereignis schon gespeichert ist."""
        key = (round(e[0], 3), e[1], e[6], round(e[4], 2))
        if key in self._keys:
            return False
        self._keys.add(key)
        self._key_order.append(key)
        if len(self._key_order) > 100000:
            self._keys.discard(self._key_order.popleft())
        return True

    def add(self, e: Event) -> None:
        with self.lock:
            if not self._remember(e):
                return
            if not self.events or e[0] >= self.events[-1][0]:
                self.events.append(e)
            else:
                # Nachzügler (z. B. ältere OKX-Meldungen nach dem Verbinden) an die richtige Stelle
                i = len(self.events)
                while i > 0 and self.events[i - 1][0] > e[0]:
                    i -= 1
                self.events.insert(i, e)
            if self.path:
                self._pending.append(e)

    def prune(self) -> None:
        cutoff = time.time() - KEEP_SECONDS
        with self.lock:
            while self.events and self.events[0][0] < cutoff:
                self.events.popleft()
            self.first_seen = max(self.first_seen, cutoff)

    def snapshot(self, since: float) -> List[Event]:
        with self.lock:
            # Die Liste ist nach Zeit sortiert (add): von hinten lesen, bis die Zeit unterschritten ist
            out = []
            for e in reversed(self.events):
                if e[0] < since:
                    break
                out.append(e)
            out.reverse()
            return out


# ---------------------------------------------------------------- Börsen

class Source(threading.Thread):
    name_: str = ""
    url: str = ""
    heartbeat_every = 20.0

    def __init__(self, store: Store) -> None:
        super().__init__(daemon=True, name=f"liq-{self.name_}")
        self.store = store
        self.connected = False
        self.last_event = 0.0
        self.count = 0
        self.error: Optional[str] = None
        self.connected_since: Optional[float] = None

    def subscribe(self, ws: WebSocket) -> None:
        pass

    def heartbeat(self, ws: WebSocket) -> None:
        pass

    def handle(self, msg: str) -> List[Event]:
        raise NotImplementedError

    def emit(self, events: List[Event]) -> None:
        for e in events:
            if e[4] > 0:
                self.store.add(e)
                self.count += 1
                self.last_event = time.time()

    def run(self) -> None:
        backoff = 2.0
        while True:
            ws = None
            try:
                ws = WebSocket(self.url)
                self.subscribe(ws)
                self.connected, self.error, self.connected_since = True, None, time.time()
                backoff = 2.0
                while True:
                    msg = ws.recv()
                    if msg is not None:
                        try:
                            self.emit(self.handle(msg))
                        except (ValueError, KeyError, TypeError):
                            pass   # unbekanntes Format: Nachricht überspringen
                    if time.time() - ws.last_send >= self.heartbeat_every:
                        self.heartbeat(ws)
            except Exception as exc:   # Netzwerkfehler aller Art: neu verbinden
                self.error = f"{type(exc).__name__}: {exc}"[:200]
            finally:
                self.connected = False
                if ws:
                    ws.close()
            time.sleep(backoff)
            backoff = min(backoff * 2, 120)


class Binance(Source):
    """Alle USDⓈ-M-Perpetuals in einem Stream. SELL = Long-Position zwangsverkauft.
    Binance hat die Futures-Streams auf getrennte Pfade verteilt: Liquidationen kommen nur noch
    über /market/; die alte Adresse /ws/!forceOrder@arr verbindet, liefert aber nichts."""
    name_ = "Binance"
    url = "wss://fstream.binance.com/market/ws/!forceOrder@arr"
    heartbeat_every = 1e9   # Binance pingt selbst, wir antworten mit Pong

    def handle(self, msg: str) -> List[Event]:
        d = json.loads(msg)
        if d.get("e") != "forceOrder":
            return []
        o = d["o"]
        sym = o["s"]
        price = float(o.get("ap") or 0) or float(o["p"])
        qty = float(o.get("z") or 0) or float(o["q"])
        coin = base_coin(sym)
        if "_" in sym and sym.split("_")[0].endswith("USD"):
            # COIN-M (z. B. BTCUSD_PERP): Menge in Kontrakten, BTC zu 100 $, alle anderen zu 10 $
            usd = qty * (100 if coin == "BTC" else 10)
        else:
            usd = price * qty
        side = "long" if o["S"] == "SELL" else "short"
        return [(o["T"] / 1000, self.name_, coin, side, usd, price, sym)]


class Bybit(Source):
    """Bybit v5, Kanal allLiquidation.{Symbol} für alle USDT-Perpetuals.
    Laut Doku bedeutet S = "Buy", dass eine Long-Position liquidiert wurde."""
    name_ = "Bybit"
    url = "wss://stream.bybit.com/v5/public/linear"

    def subscribe(self, ws: WebSocket) -> None:
        tickers = _get_json("https://api.bybit.com/v5/market/tickers?category=linear")["result"]["list"]
        symbols = [t["symbol"] for t in tickers if t["symbol"].endswith("USDT")]
        for i in range(0, len(symbols), 10):
            ws.send(json.dumps({"op": "subscribe", "args": [f"allLiquidation.{s}" for s in symbols[i:i + 10]]}))

    def heartbeat(self, ws: WebSocket) -> None:
        ws.send('{"op":"ping"}')

    def handle(self, msg: str) -> List[Event]:
        d = json.loads(msg)
        if not str(d.get("topic", "")).startswith("allLiquidation."):
            return []
        out = []
        for x in d.get("data", []):
            price, qty = float(x["p"]), float(x["v"])
            side = "long" if x["S"] == "Buy" else "short"
            out.append((x["T"] / 1000, self.name_, base_coin(x["s"]), side, price * qty, price, x["s"]))
        return out


class OKX(Source):
    """OKX, Kanal liquidation-orders für alle Perpetual-Swaps. Kontrakte → USD über ctVal."""
    name_ = "OKX"
    url = "wss://ws.okx.com:8443/ws/v5/public"

    def __init__(self, store: Store) -> None:
        super().__init__(store)
        self.contracts: Dict[str, Tuple[float, str]] = {}

    def subscribe(self, ws: WebSocket) -> None:
        inst = _get_json("https://www.okx.com/api/v5/public/instruments?instType=SWAP")["data"]
        self.contracts = {i["instId"]: (float(i["ctVal"] or 0), i["ctType"]) for i in inst}
        ws.send(json.dumps({"op": "subscribe", "args": [{"channel": "liquidation-orders", "instType": "SWAP"}]}))

    def heartbeat(self, ws: WebSocket) -> None:
        ws.send("ping")

    def handle(self, msg: str) -> List[Event]:
        if msg == "pong":
            return []
        d = json.loads(msg)
        out = []
        for item in d.get("data", []):
            inst = item["instId"]
            ct_val, ct_type = self.contracts.get(inst, (0.0, "linear"))
            if not ct_val:
                continue
            for x in item.get("details", []):
                price, size = float(x["bkPx"]), float(x["sz"])
                usd = size * ct_val if ct_type == "inverse" else size * ct_val * price
                pos = x.get("posSide")
                side = pos if pos in ("long", "short") else ("long" if x.get("side") == "sell" else "short")
                out.append((int(x["ts"]) / 1000, self.name_, base_coin(inst), side, usd, price, inst))
        return out


# ---------------------------------------------------------------- Auswertung

class Collector:
    def __init__(self, data_dir: Optional[Path]) -> None:
        self.store = Store(data_dir / "liquidations.db" if data_dir else None)
        self.sources = [Binance(self.store), Bybit(self.store), OKX(self.store)]

    def start(self) -> "Collector":
        for s in self.sources:
            s.start()
        threading.Thread(target=self._maintain, daemon=True, name="liq-maintain").start()
        return self

    def _maintain(self) -> None:
        last_prune = time.time()
        while True:
            time.sleep(5)
            self.store.flush()
            if time.time() - last_prune > 30:
                self.store.prune()
                last_prune = time.time()

    def history(self, days: int, coin: Optional[str] = None, exchange: Optional[str] = None) -> Dict[str, Any]:
        return {"updated": time.time(), "days": days, **self.store.history(days, coin, exchange)}

    def status(self) -> Dict[str, Any]:
        now = time.time()
        return {
            "since": self.store.first_seen,
            "coverage_hours": round((now - self.store.first_seen) / 3600, 2),
            "sources": [{"name": s.name_, "connected": s.connected, "events": s.count,
                         "last_event": s.last_event or None, "error": None if s.connected else s.error}
                        for s in self.sources],
        }

    def overview(self, window: str) -> Dict[str, Any]:
        now = time.time()
        events = self.store.snapshot(now - KEEP_SECONDS)
        totals = {}
        for key, sec in WINDOWS.items():
            t = {"total": 0.0, "long": 0.0, "short": 0.0, "count": 0}
            for e in events:
                if e[0] >= now - sec:
                    t[e[3]] += e[4]
                    t["total"] += e[4]
                    t["count"] += 1
            totals[key] = t

        sel = [e for e in events if e[0] >= now - WINDOWS[window]]
        coins: Dict[str, Dict[str, float]] = {}
        exchanges: Dict[str, Dict[str, float]] = {x: {"total": 0.0, "long": 0.0, "short": 0.0, "count": 0} for x in EXCHANGES}
        largest: Optional[Event] = None
        for e in sel:
            c = coins.setdefault(e[2], {"total": 0.0, "long": 0.0, "short": 0.0, "count": 0})
            x = exchanges.setdefault(e[1], {"total": 0.0, "long": 0.0, "short": 0.0, "count": 0})
            for agg in (c, x):
                agg["total"] += e[4]
                agg[e[3]] += e[4]
                agg["count"] += 1
            if largest is None or e[4] > largest[4]:
                largest = e

        # Stündliche Summen der letzten 24 h (für das Balkendiagramm)
        hour_start = int(now // 3600) * 3600
        hours = [{"t": hour_start - 3600 * i, "long": 0.0, "short": 0.0} for i in range(23, -1, -1)]
        first = hours[0]["t"]
        for e in events:
            i = int((e[0] - first) // 3600)
            if 0 <= i < 24:
                hours[i][e[3]] += e[4]

        coin_list = sorted(({"coin": k, **v} for k, v in coins.items()), key=lambda c: -c["total"])
        return {
            "updated": now,
            "window": window,
            "totals": totals,
            "coins": coin_list[:80],
            "coins_other": sum(c["total"] for c in coin_list[80:]),
            "coin_count": len(coin_list),
            "exchanges": sorted(({"name": k, **v} for k, v in exchanges.items()), key=lambda x: -x["total"]),
            "largest": _event_json(largest) if largest else None,
            "hours": hours,
            "status": self.status(),
        }

    def feed(self, coin: Optional[str], exchange: Optional[str], min_usd: float, limit: int) -> Dict[str, Any]:
        events = self.store.snapshot(time.time() - KEEP_SECONDS)
        out = []
        for e in reversed(events):
            if coin and e[2] != coin:
                continue
            if exchange and e[1] != exchange:
                continue
            if e[4] < min_usd:
                continue
            out.append(_event_json(e))
            if len(out) >= limit:
                break
        return {"updated": time.time(), "orders": out, "status": self.status()}


def _event_json(e: Event) -> Dict[str, Any]:
    return {"time": e[0], "exchange": e[1], "coin": e[2], "side": e[3], "usd": round(e[4], 2),
            "price": e[5], "symbol": e[6]}


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Liquidationen live mitschneiden (Test)")
    p.add_argument("--seconds", type=int, default=60)
    a = p.parse_args()
    col = Collector(None).start()
    t0 = time.time()
    while time.time() - t0 < a.seconds:
        time.sleep(10)
        st = col.status()
        print(f"{int(time.time() - t0):>4}s  " + "  ".join(
            f"{s['name']}: {'✓' if s['connected'] else '✗'} {s['events']}" + (f" ({s['error']})" if s['error'] else "")
            for s in st["sources"]))
    ov = col.overview("1h")
    t = ov["totals"]["1h"]
    print(f"\nSumme: ${t['total']:,.0f}  Long ${t['long']:,.0f}  Short ${t['short']:,.0f}  ({t['count']} Liquidationen)")
    for c in ov["coins"][:10]:
        print(f"  {c['coin']:<10} ${c['total']:>12,.0f}  Long {c['long'] / c['total']:.0%}")
    if ov["largest"]:
        print("Größte:", ov["largest"])

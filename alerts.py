#!/usr/bin/env python3
"""Alarme: Regeln je Coin, Prüfung alle 5 Minuten im Server, Benachrichtigung per ntfy.

- Regeln, Einstellungen und ausgelöste Alarme liegen in SQLite (data/alerts.db).
- Die Prüfung nutzt die Coin-Daten, die der Server ohnehin berechnet (Score, Kurs, MA200, Funding),
  und die mitgeschnittenen Liquidationen. Keine eigenen Abfragen: der Server reicht Ladefunktionen herein.
- Eine Regel löst aus, wenn ihre Bedingung wahr wird (Flanke), höchstens einmal je COOLDOWN.
  Ist die Bedingung beim Anlegen schon erfüllt, kommt die erste Meldung bei der nächsten Prüfung.
  „Kurs kreuzt MA200“ meldet jeden Wechsel der Seite, nicht den Ausgangszustand.
- ntfy: https://ntfy.sh (oder eigener Server), Kanalname = Geheimnis; die App abonniert ihn.
"""

import json
import re
import sqlite3
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

CHECK_SECONDS = 300            # wie CACHE_SECONDS im Server: öfter ändern sich die Werte nicht
FIRST_CHECK_SECONDS = 60       # erste Prüfung nach dem Start, wenn die Caches warm sind
COOLDOWN = 3600                # dieselbe Regel höchstens einmal pro Stunde
LIQ_WINDOW = 3600              # Liquidationen: Summe der letzten Stunde
MAX_RULES = 100
FIRED_KEEP = 500               # so viele ausgelöste Alarme bleiben gespeichert
NTFY_DEFAULT = "https://ntfy.sh"
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
URL_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d+)?(/[\w./-]*)?$")
USER_AGENT = "crypto-dashboard/1.0"

# Bedingungen: Kennung → braucht eine Schwelle
KINDS = {
    "score_above": True, "score_below": True,
    "price_above": True, "price_below": True,
    "ma200_cross": False,
    "funding_above": True, "funding_below": True,
    "liq_1h": True,
}
ALL_COINS = "*"                # Liquidations-Regel über alle Coins

TEXTS = {
    "de": {
        "score_above": "Score {sym} über {thr}: jetzt {val}",
        "score_below": "Score {sym} unter {thr}: jetzt {val}",
        "price_above": "{sym} über {thr}: jetzt {val}",
        "price_below": "{sym} unter {thr}: jetzt {val}",
        "ma200_up": "{sym} kreuzt den MA200 nach oben ({val} über dem MA200)",
        "ma200_down": "{sym} kreuzt den MA200 nach unten ({val} unter dem MA200)",
        "funding_above": "Funding {sym} über {thr} pro 8 h: jetzt {val}",
        "funding_below": "Funding {sym} unter {thr} pro 8 h: jetzt {val}",
        "liq_1h": "Liquidationen {sym} in 1 h über {thr}: {val} (Long {long}, Short {short})",
        "all": "alle Coins",
        "title": "Alarm: {sym}",
        "test_title": "Test vom Crypto-Dashboard",
        "test": "Benachrichtigungen kommen an.",
    },
    "en": {
        "score_above": "{sym} score above {thr}: now {val}",
        "score_below": "{sym} score below {thr}: now {val}",
        "price_above": "{sym} above {thr}: now {val}",
        "price_below": "{sym} below {thr}: now {val}",
        "ma200_up": "{sym} crosses above the MA200 ({val} above the MA200)",
        "ma200_down": "{sym} crosses below the MA200 ({val} below the MA200)",
        "funding_above": "{sym} funding above {thr} per 8 h: now {val}",
        "funding_below": "{sym} funding below {thr} per 8 h: now {val}",
        "liq_1h": "{sym} liquidations in 1 h above {thr}: {val} (long {long}, short {short})",
        "all": "all coins",
        "title": "Alert: {sym}",
        "test_title": "Test from the crypto dashboard",
        "test": "Notifications are arriving.",
    },
}


def fmt_usd(v: float) -> str:
    """Kurs/Betrag kurz: $1,23 Mio. → $1.23M (Meldungen sind kurz, daher ohne Tausenderpunkte je Sprache)."""
    a = abs(v)
    if a >= 1e9:
        return f"${v / 1e9:.2f}B"
    if a >= 1e6:
        return f"${v / 1e6:.2f}M"
    if a >= 1e3:
        return f"${v:,.0f}"
    if a >= 1:
        return f"${v:.2f}"
    return f"${v:.6g}"


def localize(text: str, lang: str) -> str:
    """Deutsch: Tausenderpunkt und Dezimalkomma (die Formate oben sind englisch)."""
    return text.translate(str.maketrans(",.", ".,")) if lang == "de" else text


def fmt_value(kind: str, v: float) -> str:
    if kind.startswith("score"):
        return f"{v:+.0f}"
    if kind.startswith("funding"):
        return f"{v * 100:+.4f}%"
    if kind == "ma200_cross":
        return f"{abs(v) * 100:.1f}%"
    return fmt_usd(v)


class AlertStore:
    """SQLite: Regeln, Einstellungen, ausgelöste Alarme. Alle Zugriffe unter einem Lock."""

    def __init__(self, path: Optional[Path]) -> None:
        self.lock = threading.Lock()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path) if path else ":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS rules (
                    id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, coin_id TEXT, kind TEXT NOT NULL,
                    threshold REAL, enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
                    state INTEGER, last_value REAL, last_checked REAL, last_fired REAL);
                CREATE TABLE IF NOT EXISTS fired (
                    id INTEGER PRIMARY KEY, rule_id INTEGER, time REAL NOT NULL, symbol TEXT, kind TEXT,
                    text TEXT NOT NULL, sent INTEGER NOT NULL, error TEXT);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
            """)

    # --- Einstellungen

    def settings(self) -> Dict[str, str]:
        with self.lock:
            rows = self.db.execute("SELECT key, value FROM settings").fetchall()
        s = {"topic": "", "server": NTFY_DEFAULT, "lang": "de", "link": ""}
        s.update({r["key"]: r["value"] for r in rows})
        return s

    def save_settings(self, values: Dict[str, str]) -> None:
        with self.lock, self.db:
            self.db.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", list(values.items()))

    # --- Regeln

    def rules(self) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM rules ORDER BY id")]

    def add_rule(self, symbol: str, coin_id: Optional[str], kind: str, threshold: Optional[float]) -> int:
        with self.lock, self.db:
            if self.db.execute("SELECT COUNT(*) FROM rules").fetchone()[0] >= MAX_RULES:
                raise ValueError("zu viele Regeln")
            cur = self.db.execute(
                "INSERT INTO rules (symbol, coin_id, kind, threshold, created) VALUES (?, ?, ?, ?, ?)",
                (symbol, coin_id, kind, threshold, time.time()))
            return int(cur.lastrowid)

    def delete_rule(self, rule_id: int) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))

    def set_enabled(self, rule_id: int, enabled: bool) -> None:
        # Beim Einschalten neu beginnen: Zustand vergessen, damit eine erfüllte Bedingung wieder meldet
        with self.lock, self.db:
            self.db.execute("UPDATE rules SET enabled = ?, state = NULL WHERE id = ?", (int(enabled), rule_id))

    def update_rule(self, rule_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.lock, self.db:
            self.db.execute(f"UPDATE rules SET {cols} WHERE id = ?", (*fields.values(), rule_id))

    # --- Ausgelöste Alarme

    def add_fired(self, rule: Optional[Dict[str, Any]], text: str, sent: bool, error: Optional[str]) -> None:
        with self.lock, self.db:
            self.db.execute(
                "INSERT INTO fired (rule_id, time, symbol, kind, text, sent, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rule and rule["id"], time.time(), rule and rule["symbol"], rule and rule["kind"], text, int(sent), error))
            self.db.execute("DELETE FROM fired WHERE id <= (SELECT MAX(id) FROM fired) - ?", (FIRED_KEEP,))

    def fired(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM fired ORDER BY id DESC LIMIT ?", (limit,))]


def send_ntfy(settings: Dict[str, str], title: str, message: str, click: str = "", tags: str = "chart_with_upwards_trend") -> None:
    """Nachricht an den ntfy-Kanal; Fehler als Exception."""
    topic = settings.get("topic") or ""
    if not TOPIC_RE.match(topic):
        raise ValueError("kein gültiger ntfy-Kanal eingestellt")
    server = (settings.get("server") or NTFY_DEFAULT).rstrip("/")
    # Als JSON veröffentlichen statt mit Headern: Titel mit Umlauten gehen in HTTP-Headern nicht sauber
    body = json.dumps({"topic": topic, "title": title, "message": message, "tags": tags.split(","),
                       **({"click": click} if click else {})}).encode("utf-8")
    req = urllib.request.Request(server + "/", data=body, method="POST",
                                 headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as res:
        res.read()


class Checker:
    """Prüft alle Regeln regelmäßig im Hintergrund.

    load_coins: [(Symbol, Coin-ID)] → Coins wie /api/coin (eine gebündelte Abfrage)
    liq_events: Zeitpunkt → Liquidationen seitdem (Event-Tupel aus liquidations.py)"""

    def __init__(self, store: AlertStore,
                 load_coins: Callable[[List[Tuple[str, str]]], List[Dict[str, Any]]],
                 liq_events: Callable[[float], List[tuple]]) -> None:
        self.store, self.load_coins, self.liq_events = store, load_coins, liq_events
        self.lock = threading.Lock()   # nie zwei Prüfungen gleichzeitig (Hintergrund und „Jetzt prüfen“)
        self.last_run: Optional[float] = None
        self.last_error: Optional[str] = None

    def start(self) -> "Checker":
        threading.Thread(target=self._loop, daemon=True, name="alerts").start()
        return self

    def _loop(self) -> None:
        time.sleep(FIRST_CHECK_SECONDS)
        while True:
            try:
                self.check()
            except Exception as e:  # die Schleife darf nie sterben
                self.last_error = str(e)
            time.sleep(CHECK_SECONDS)

    # Messwert einer Regel aus den Coin-Daten (None = nicht verfügbar)
    @staticmethod
    def value(kind: str, coin: Dict[str, Any]) -> Optional[float]:
        d = coin.get("data") or {}
        if kind.startswith("score"):
            return coin.get("score")
        if kind.startswith("price"):
            ma = d.get("price_ma200")
            return d.get("price") if d.get("price") is not None else (ma or {}).get("price")
        if kind == "ma200_cross":
            return (d.get("price_ma200") or {}).get("distance")
        if kind.startswith("funding"):
            f = d.get("funding")
            # letzte Zahlung auf 8 h umgerechnet wie FUNDING_SCALE (manche Coins zahlen alle 1 oder 4 h)
            return f["last"] * 8 / f["interval_h"] if f and f.get("interval_h") else None
        return None

    def check(self) -> None:
        with self.lock:
            self._check()

    def _check(self) -> None:
        rules = [r for r in self.store.rules() if r["enabled"]]
        settings = self.store.settings()
        now = time.time()
        coins: Dict[str, Dict[str, Any]] = {}
        entries = sorted({(r["symbol"], r["coin_id"]) for r in rules if r["kind"] != "liq_1h" and r["coin_id"]})
        if entries:
            for (sym, cid), coin in zip(entries, self.load_coins(list(entries))):
                coins[f"{sym}:{cid}"] = coin
        liq = self.liq_events(now - LIQ_WINDOW) if any(r["kind"] == "liq_1h" for r in rules) else []
        # Ohne Kanal nur Werte festhalten, nichts auslösen: sonst wäre die Meldung verbraucht, bevor ntfy eingerichtet ist
        can_send = bool(TOPIC_RE.match(settings.get("topic") or ""))

        for r in rules:
            kind, thr, extra = r["kind"], r["threshold"], {}
            if kind == "liq_1h":
                ev = [e for e in liq if r["symbol"] == ALL_COINS or e[2] == r["symbol"]]
                val = sum(e[4] for e in ev)
                extra = {"long": fmt_usd(sum(e[4] for e in ev if e[3] == "long")),
                         "short": fmt_usd(sum(e[4] for e in ev if e[3] == "short"))}
            else:
                coin = coins.get(f"{r['symbol']}:{r['coin_id']}")
                val = self.value(kind, coin) if coin else None
            if val is None:
                self.store.update_rule(r["id"], last_checked=now)
                continue

            if kind == "ma200_cross":
                side = 1 if val >= 0 else 0
                fire = r["state"] is not None and side != r["state"]
                new_state = side
                key = "ma200_up" if side else "ma200_down"
            else:
                cond = val > thr if kind.endswith("above") or kind == "liq_1h" else val < thr
                cooled = r["last_fired"] is None or now - r["last_fired"] >= COOLDOWN
                fire = cond and not r["state"] and cooled
                # Zustand bleibt „aus“, solange die Sperrzeit läuft: danach meldet eine weiter erfüllte Bedingung
                new_state = 1 if (fire or (cond and r["state"])) else 0
                key = kind
            fields: Dict[str, Any] = {"last_value": val, "last_checked": now}
            if not can_send and kind != "ma200_cross":
                self.store.update_rule(r["id"], **fields)
                continue
            fields["state"] = new_state   # MA200: Seite immer merken, gemeldet wird nur mit Kanal
            if fire and can_send:
                self._notify(r, key, val, thr, extra, settings)
                fields["last_fired"] = now
            self.store.update_rule(r["id"], **fields)
        self.last_run, self.last_error = now, None

    def _notify(self, rule: Dict[str, Any], key: str, val: float, thr: Optional[float],
                extra: Dict[str, str], settings: Dict[str, str]) -> None:
        t = TEXTS.get(settings.get("lang"), TEXTS["de"])
        sym = t["all"] if rule["symbol"] == ALL_COINS else rule["symbol"]
        kind = rule["kind"]
        lang = settings.get("lang") or "de"
        loc = {k: localize(v, lang) for k, v in extra.items()}
        text = t[key].format(sym=sym, val=localize(fmt_value(kind, val), lang),
                             thr=localize(fmt_value(kind, thr), lang) if thr is not None else "", **loc)
        click = ""
        if settings.get("link") and rule["coin_id"]:
            click = f"{settings['link'].rstrip('/')}/coin.html?symbol={rule['symbol']}&id={rule['coin_id']}"
        elif settings.get("link") and kind == "liq_1h":
            click = settings["link"].rstrip("/") + "/liquidations.html"
        sent, error = True, None
        try:
            send_ntfy(settings, t["title"].format(sym=sym), text, click,
                      "rotating_light" if kind == "liq_1h" else "chart_with_upwards_trend")
        except Exception as e:
            sent, error = False, str(e)[:200]
        self.store.add_fired(rule, text, sent, error)

    def send_test(self) -> None:
        settings = self.store.settings()
        t = TEXTS.get(settings.get("lang"), TEXTS["de"])
        send_ntfy(settings, t["test_title"], t["test"], settings.get("link", ""), "white_check_mark")

#!/usr/bin/env python3
"""Tägliche Crypto-Sentiment-Übersicht.

Sammelt kostenlose, öffentliche Marktdaten (ohne API-Keys) und berechnet
einen Stimmungs-Score von -100 (extreme Angst) bis +100 (überhitzt).

Quellen:
  - alternative.me     Fear & Greed Index
  - Binance Futures    Funding Rate, Open Interest, Long/Short Ratio
  - Binance Spot       Tageskerzen für den 200-Tage-Durchschnitt und RSI(14)
  - Deribit            DVOL (implizite Optionsvolatilität, nur BTC und ETH)
  - DefiLlama          Stablecoin-Marktkapitalisierung
  - CoinGecko          Marktkapitalisierung, BTC-Dominanz, Trending Coins

Aufruf:
  python3 sentiment.py              # BTC
  python3 sentiment.py --symbol ETH # anderer Coin (Binance-Futures-Symbol)
  python3 sentiment.py --json       # Rohdaten als JSON

Die Schwellenwerte sind Erfahrungswerte, keine Naturgesetze.
"""

import argparse
import bisect
import html
import http.client
import json
import re
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional, Tuple

TIMEOUT = 15
RATE_LIMIT_RETRIES = 2    # erneute Versuche bei HTTP 429 (nicht für CoinGecko: dort sofortiger Abbruch, siehe unten)
USER_AGENT = "Mozilla/5.0 (sentiment-script)"
# CoinGecko ohne API-Key: wenige Abfragen pro Minute, und Bursts lösen schnell HTTP 429 aus.
# Daher alle CoinGecko-Abfragen nacheinander mit Mindestabstand, nach 429 eine längere Pause.
COINGECKO_HOST = "api.coingecko.com"
COINGECKO_MIN_INTERVAL = 4     # Sekunden zwischen zwei Abfragen (ohne Key etwa 5–15 pro Minute erlaubt)
COINGECKO_COOLDOWN = 30        # Pause nach 429 ohne Retry-After (CoinGecko zählt pro Minute)
COINGECKO_MAX_WAIT = 8         # steht eine längere Pause an (nach 429): sofort abbrechen, die Seite versucht es erneut


class RateLimited(ValueError):
    """CoinGecko pausiert gerade; retry_in = Sekunden bis zum nächsten Versuch."""
    def __init__(self, retry_in: float):
        super().__init__(f"CoinGecko-Limit erreicht, neuer Versuch in {round(retry_in)} s möglich")
        self.retry_in = retry_in

# Skalen für den Score: Ankerpunkte (Messwert, Punkte). Zwischen den Punkten
# wird linear interpoliert, außerhalb auf -100 / +100 begrenzt.
FNG_SCALE = [(20, -100), (50, 0), (80, 100)]
FUNDING_SCALE = [(-0.0001, -100), (0.0001, 0), (0.0005, 100)]  # pro 8 h; 0,01 % = Binance-Standard
OI_SCALE = [(-0.15, -100), (0.0, 0), (0.15, 100)]   # Hebelaufbau = Gier, Hebelabbau = Angst
MA200_SCALE = [(-0.20, -100), (0.10, 0), (0.35, 100)]  # +10 % = normaler Aufwärtstrend
LS_SCALE = [(0.9, -100), (2.0, 0), (3.5, 100)]      # Long- zu Short-Accounts; auf Binance meist ~2
RSI_SCALE = [(30, -100), (50, 0), (70, 100)]
DVOL_SCALE = [(-1.5, 100), (0.0, 0), (1.5, -100)]   # z-Wert: ruhig = Sorglosigkeit, hoch = Panik
# MVRV (Coin Metrics): Ankerpunkte nur aus BTC vor 2018, geprüft ab 2018 auf BTC, ETH, XRP, LTC, DOGE, ADA, LINK
MVRV_SCALE = [(0.9, -100), (1.6, 0), (3.5, 100)]
# Ersatz-Signale aus CoinGecko-Kursdaten für Coins ohne Binance-Futures oder -Kursdaten
MOMENTUM_7D_SCALE = [(-0.25, -100), (0.0, 0), (0.25, 100)]
MOMENTUM_30D_SCALE = [(-0.50, -100), (0.0, 0), (0.50, 100)]
TURNOVER_SCALE = [(0.05, 0), (0.50, 100)]           # Tagesvolumen / Marktkap.; Vorzeichen folgt dem 7T-Trend

SIGNAL_STRONG = 50        # ab hier gilt ein Einzelsignal als ausgeprägt
# Gewicht je Signal im Score (Standard 1). Aus einem Backtest BTC/ETH/SOL 04/2024–11/2026 (Score gegen
# Kursänderung 30/90 Tage danach): Trend (MA200) und Optionsvola sagten am meisten, Fear & Greed und
# RSI kaum etwas voraus. Bewusst grobe Stufen, um nicht an einen einzelnen Marktzyklus anzupassen.
SIGNAL_WEIGHTS = {"Fear & Greed": 0.5, "RSI (14)": 0.5, "RSI (14 Std.)": 0.5,
                  "Preis vs. MA200": 1.5, "Optionsvola (DVOL)": 1.5}
SCORE_STATS_DAYS = 960    # Score-Historie für die Trefferquote (Binance liefert bis 1000 Tageskerzen)
VERDICT_HOT = 40          # Gesamtscore ab +40 = überhitzt, ab -40 = extreme Angst
VERDICT_WARM = 15         # ab ±15 = leicht gierig / ängstlich

RSI_PERIOD = 14
MA_PERIOD = 200           # Tage für den gleitenden Durchschnitt
DVOL_SYMBOLS = {"BTC", "ETH"}  # Deribit bietet DVOL nur für diese Coins
DVOL_WINDOW = 90          # Tage für Mittelwert und Standardabweichung
FUNDING_HISTORY_DAYS = 90 # Funding-Verlauf auf der Detailseite
FUNDING_BASE_HOURS = 8    # FUNDING_SCALE gilt pro 8 h; kürzere Takte werden darauf umgerechnet
SCORE_HISTORY_DAYS = 365  # rückwirkend berechneter Score-Verlauf auf der Detailseite

# Versionsnummer und Änderungsprotokoll der Score-Methodik (Seite „Methodik & Datenquellen“, /api/methodology).
# Bei jeder Änderung an Signalen, Skalen, Gewichten, Schwellen oder an der Einordnung: Version erhöhen und
# Eintrag oben ergänzen. Hauptversion = Score-Werte ändern sich; Nebenversion = Ergänzung ohne Bruch.
METHODOLOGY_VERSION = "3.1"
METHODOLOGY_CHANGES = [
    {"version": "3.1", "date": "2026-11-23",
     "de": "Futures-Signale jetzt auch für Coins, die Binance als Vielfache führt (1000PEPE, 1000SHIB, 1000BONK, 1MBABYDOGE …) – "
           "deren Score war bisher unvollständig. RSI-Begriffe am angezeigten Wert (überverkauft unter 30, überkauft über 70), "
           "einheitliche MVRV-Stufen, Score-Verlauf zeigt für heute den aktuellen Score.",
     "en": "Futures signals now also for coins Binance lists as multiples (1000PEPE, 1000SHIB, 1000BONK, 1MBABYDOGE …) – "
           "their score was incomplete before. RSI wording based on the displayed value (oversold below 30, overbought above 70), "
           "consistent MVRV levels, the score history shows the current score for today."},
    {"version": "3.0", "date": "2026-11-23",
     "de": "Umbenannt in „Stimmungs-Score“ (−100 Angst … +100 Gier/Überhitzung). Open Interest aus 4-h-Werten statt "
           "Tageswerten (frischer, gleich wie im Derivate-Tab) – dadurch leicht andere Score-Werte. RSI-Begriffe einheitlich: "
           "„überverkauft/überkauft“ erst unter 30 bzw. über 70. Lagebild nennt die Signale mit dem größten Anteil am Score "
           "(Punkte × Gewicht) statt den meisten Rohpunkten.",
     "en": "Renamed to “sentiment score” (−100 fear … +100 greed/overheating). Open interest from 4 h values instead of daily "
           "values (fresher, same as the derivatives tab) – slightly different score values as a result. Consistent RSI wording: "
           "“oversold/overbought” only below 30 or above 70. The situation lists the signals with the largest share of the score "
           "(points × weight) instead of the most raw points."},
    {"version": "2.1", "date": "2026-11-23",
     "de": "Datenstatus je Kennzahl (Quelle, Stand, live/aktuell/verzögert/veraltet/nicht verfügbar); Fear & Greed mit Datum. "
           "Berechnung unverändert.",
     "en": "Data status per figure (source, as-of time, live/current/delayed/stale/not available); Fear & Greed with date. "
           "Calculation unchanged."},
    {"version": "2.0", "date": "2026-11-22",
     "de": "Score als gewichteter statt einfacher Durchschnitt (Preis vs. MA200 und DVOL ×1,5, Fear & Greed und RSI ×0,5), "
           "abgeleitet aus einem Backtest BTC/ETH/SOL. Neues Signal MVRV (Coin Metrics). Trefferquote je Score-Zone. "
           "Einordnung angepasst: extreme Angst allein gilt nicht mehr als Kaufsignal.",
     "en": "Score as a weighted instead of a simple average (price vs. MA200 and DVOL ×1.5, Fear & Greed and RSI ×0.5), "
           "derived from a backtest on BTC/ETH/SOL. New signal MVRV (Coin Metrics). Hit rate per score zone. "
           "Assessment adjusted: extreme fear alone no longer counts as a buy signal."},
    {"version": "1.2", "date": "2026-11-15",
     "de": "Lagebild (Stimmung × Trend) und Handlungsempfehlung je Coin.",
     "en": "Situation assessment (sentiment × trend) and suggested action per coin."},
    {"version": "1.1", "date": "2026-11-14",
     "de": "Funding auf 8 h umgerechnet (Coins mit 1-h- oder 4-h-Takt). Rückwirkend berechneter Score-Verlauf.",
     "en": "Funding converted to 8 h (coins paying every 1 h or 4 h). Retroactively calculated score history."},
    {"version": "1.0", "date": "2026-10-25",
     "de": "Stufenloser Score −100 … +100 als Durchschnitt aus Fear & Greed, Funding, Open Interest, Preis vs. MA200, "
           "RSI (14), Long/Short Ratio und DVOL (BTC/ETH); Ersatz-Signale aus CoinGecko für Coins ohne Binance-Daten.",
     "en": "Continuous score −100 … +100 as the average of Fear & Greed, funding, open interest, price vs. MA200, "
           "RSI (14), long/short ratio and DVOL (BTC/ETH); fallback signals from CoinGecko for coins without Binance data."},
]

# Skala je Signal (Schlüssel wie in score()), für die Offenlegung auf der Methodik-Seite
SIGNAL_SCALES = {
    "Fear & Greed": FNG_SCALE, "Funding Rate": FUNDING_SCALE, "Open Interest": OI_SCALE,
    "Preis vs. MA200": MA200_SCALE, "RSI (14)": RSI_SCALE, "Long/Short Ratio": LS_SCALE,
    "Optionsvola (DVOL)": DVOL_SCALE, "MVRV": MVRV_SCALE, "Momentum 7T": MOMENTUM_7D_SCALE,
    "Momentum 30T": MOMENTUM_30D_SCALE, "Umschlag": TURNOVER_SCALE, "RSI (14 Std.)": RSI_SCALE,
}


_cg_lock = threading.Lock()
_cg_next = 0.0   # frühester Zeitpunkt für die nächste CoinGecko-Abfrage


def _coingecko_wait() -> None:
    """Reiht CoinGecko-Abfragen ein (unter Sperre, also der Reihe nach, je COINGECKO_MIN_INTERVAL).
    Läuft gerade eine Pause nach HTTP 429, wird sofort abgebrochen statt minutenlang zu blockieren."""
    global _cg_next
    with _cg_lock:
        wait = _cg_next - time.time()
        if wait > COINGECKO_MAX_WAIT:
            raise RateLimited(wait)
        if wait > 0:
            time.sleep(wait)
        _cg_next = time.time() + COINGECKO_MIN_INTERVAL


def coingecko_retry_in() -> float:
    """Sekunden, bis CoinGecko wieder abgefragt werden kann (0 = sofort)."""
    return max(0.0, _cg_next - time.time())


def fetch_json(url: str, timeout: float = 0) -> Any:
    """JSON abfragen; timeout 0 = Standard (TIMEOUT)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    coingecko = COINGECKO_HOST in url
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        if coingecko:
            _coingecko_wait()
        try:
            with urllib.request.urlopen(req, timeout=timeout or TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code != 429:
                raise
            retry_after = exc.headers.get("Retry-After", "")
            if coingecko:
                # Pause für alle folgenden CoinGecko-Abfragen setzen und nicht selbst warten
                pause = min(int(retry_after), 60) if retry_after.isdigit() else COINGECKO_COOLDOWN
                global _cg_next
                with _cg_lock:
                    _cg_next = max(_cg_next, time.time() + pause)
                raise RateLimited(pause) from exc
            if attempt == RATE_LIMIT_RETRIES:
                raise
            time.sleep(min(int(retry_after), 10) if retry_after.isdigit() else 3 * (attempt + 1))


NETWORK_ERRORS = (OSError, http.client.HTTPException)


def safe(fn, *args) -> Optional[Any]:
    """Führt eine Abfrage aus; bei Fehlern wird None geliefert statt abzubrechen.

    OSError deckt Netzwerkfehler ab, auch Timeouts und Abbrüche mitten im Lesen der Antwort
    (die urllib nicht in URLError verpackt); die übrigen unerwartete oder lückenhafte Daten."""
    try:
        return fn(*args)
    except NETWORK_ERRORS + (ValueError, LookupError, TypeError, AttributeError, ArithmeticError) as exc:
        print(f"  [warn] {fn.__name__}: {exc}", file=sys.stderr)
        return None


# --- Datenquellen -----------------------------------------------------------

def get_fear_greed() -> Dict[str, Any]:
    data = fetch_json("https://api.alternative.me/fng/?limit=8")["data"]
    return {
        "value": int(data[0]["value"]),
        "label": data[0]["value_classification"],
        "time": int(data[0]["timestamp"]),   # Beginn des Tages (UTC), für die Statusanzeige
        "yesterday": int(data[1]["value"]),
        "week_ago": int(data[7]["value"]),
    }


# Binance führt sehr billige Coins als Vielfache: 1000PEPEUSDT (Kurs × 1000), 1MBABYDOGEUSDT, 1000000MOGUSDT.
# futures_pair liefert das Kontrakt-Kürzel und den Faktor; Kurse daraus durch den Faktor teilen.
# USD-Werte (Open Interest), Funding und Long/Short brauchen keine Umrechnung.
FUTURES_PREFIX = re.compile(r"^(1000000|1000|1M)(?=[A-Z])")
FUTURES_MAP_SECONDS = 24 * 3600
_futures_map: Dict[str, Tuple[str, float]] = {}
_futures_map_at = 0.0
_futures_map_lock = threading.Lock()


def futures_pair(symbol: str) -> Tuple[str, float]:
    global _futures_map, _futures_map_at
    with _futures_map_lock:
        if time.time() - _futures_map_at > (FUTURES_MAP_SECONDS if _futures_map else 300):
            _futures_map_at = time.time()
            info = safe(fetch_json, "https://fapi.binance.com/fapi/v1/exchangeInfo")
            if info:
                m: Dict[str, Tuple[str, float]] = {}
                coins = [x for x in info.get("symbols", []) if x.get("contractType") == "PERPETUAL"
                         and x.get("quoteAsset") == "USDT" and x.get("underlyingType") == "COIN"]
                for x in coins:
                    hit = FUTURES_PREFIX.match(x["baseAsset"])
                    if hit:
                        m.setdefault(x["baseAsset"][hit.end():], (x["symbol"], 1e3 if hit.group(1) == "1000" else 1e6))
                for x in coins:   # exakter Treffer hat Vorrang
                    m[x["baseAsset"]] = (x["symbol"], 1.0)
                _futures_map = m
        return _futures_map.get(symbol, (symbol + "USDT", 1.0))


def fpair(symbol: str) -> str:
    return futures_pair(symbol)[0]


def futures_data(url: str, symbol: str) -> List[Dict[str, Any]]:
    """Abfrage bei Binance Futures; leere Antwort = kein USDT-Perpetual für diesen Coin."""
    data = fetch_json(url)
    if not data:
        raise ValueError(f"keine Binance-Futures für {symbol}USDT")
    return data


def normalize_funding(rates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Funding-Zahlungen mit Takt und auf 8 h umgerechnetem Satz.

    Die meisten Coins zahlen alle 8 h, manche alle 4 h oder 1 h. Der Takt ergibt sich aus dem
    Abstand zur vorherigen Zahlung (bei der ersten aus dem zur nächsten); Lücken zählen als 8 h."""
    out = []
    for i, r in enumerate(rates):
        if len(rates) < 2:
            hours = FUNDING_BASE_HOURS
        else:
            j = i if i > 0 else 1
            diff_h = (rates[j]["fundingTime"] - rates[j - 1]["fundingTime"]) / 3_600_000
            hours = min(FUNDING_BASE_HOURS, max(1, round(diff_h)))
        rate = float(r["fundingRate"])
        out.append({"time": r["fundingTime"] // 1000, "rate": rate, "interval_h": hours,
                    "rate_8h": rate * FUNDING_BASE_HOURS / hours})
    return out


def funding_avg_7d(funding: List[Dict[str, Any]], until: float) -> Optional[float]:
    """Ø des 8-h-Satzes über die 7 Tage bis `until` (Sekunden); None, wenn die Daten nicht reichen."""
    if not funding or funding[0]["time"] > until - 7 * 86400 + FUNDING_BASE_HOURS * 3600:
        return None
    values = [f["rate_8h"] for f in funding if until - 7 * 86400 < f["time"] <= until]
    return sum(values) / len(values) if values else None


def get_funding(symbol: str) -> Dict[str, Any]:
    # 200 Zahlungen reichen auch bei 1-h-Takt für 7 Tage
    funding = normalize_funding(futures_data(
        f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={fpair(symbol)}&limit=200", symbol
    ))
    last = funding[-1]
    values = [f["rate_8h"] for f in funding if f["time"] > last["time"] - 7 * 86400]
    return {"last": last["rate"], "interval_h": last["interval_h"], "avg_7d": sum(values) / len(values)}


def get_open_interest(symbol: str) -> Dict[str, Any]:
    # 4-h-Werte wie die Kurve im Derivate-Tab: letzter Wert gegen den vor genau 7 Tagen (42 Schritte),
    # statt Tageswerten, die bis zu 24 h alt sein können
    hist = futures_data(
        "https://fapi.binance.com/futures/data/openInterestHist"
        f"?symbol={fpair(symbol)}&period=4h&limit=43", symbol
    )
    first = float(hist[0]["sumOpenInterestValue"])
    last = float(hist[-1]["sumOpenInterestValue"])
    return {"usd": last, "change_7d": (last - first) / first}


def get_long_short(symbol: str) -> Dict[str, Any]:
    data = futures_data(
        "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
        f"?symbol={fpair(symbol)}&period=1d&limit=1", symbol
    )
    return {"ratio": float(data[-1]["longShortRatio"])}


def get_funding_history(symbol: str, days: int = FUNDING_HISTORY_DAYS) -> List[Dict[str, Any]]:
    """Funding-Zahlungen der letzten `days` Tage (Binance liefert max. 1000 je Abfrage,
    ältere werden mit endTime nachgeladen, höchstens 4 Abfragen)."""
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={fpair(symbol)}&limit=1000"
    rates = futures_data(url, symbol)
    cutoff = rates[-1]["fundingTime"] - days * 86400 * 1000
    for _ in range(3):
        if rates[0]["fundingTime"] <= cutoff:
            break
        older = safe(fetch_json, f"{url}&endTime={rates[0]['fundingTime'] - 1}")
        if not older:
            break
        rates = older + rates
    # eine Zahlung vor dem Zeitraum behalten, damit der Takt der ersten stimmt
    funding = normalize_funding(rates)
    return [f for f in funding if f["time"] * 1000 > cutoff]


def get_open_interest_history(symbol: str) -> List[Dict[str, Any]]:
    """Open Interest in 4-h-Schritten; Binance liefert davon nur die letzten 30 Tage.
    Der Kurs ergibt sich aus Wert / Anzahl Kontrakte, ohne zusätzliche Abfrage."""
    hist = futures_data(
        "https://fapi.binance.com/futures/data/openInterestHist"
        f"?symbol={fpair(symbol)}&period=4h&limit=500", symbol
    )
    mult = futures_pair(symbol)[1]   # 1000PEPE: Menge in 1000er-Einheiten
    out = []
    for h in hist:
        value, amount = float(h["sumOpenInterestValue"]), float(h["sumOpenInterest"])
        out.append({"time": h["timestamp"] // 1000, "usd": value, "price": value / (amount * mult) if amount else None})
    return out


def get_binance_klines(symbol: str, interval: str, limit: int) -> Tuple[List[list], str]:
    """Rohkerzen vom USDT-Paar: Spot, oder Perpetual-Futures, wenn Spot fehlt oder die
    Spot-Historie kürzer ist (z. B. HYPE: Spot erst seit Kurzem, Futures deutlich länger)."""
    spot = safe(fetch_json,
                f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}")
    if spot and len(spot) >= limit:
        return spot, "Binance Spot"
    pair, mult = futures_pair(symbol)
    # Vielfache (1000PEPE) haben fast immer ein Spot-Paar; deren Futures-Kurse nicht ins Chart mischen
    futures = safe(fetch_json,
                   f"https://fapi.binance.com/fapi/v1/klines?symbol={pair}&interval={interval}&limit={limit}") if mult == 1 else None
    if futures and len(futures) > len(spot or []):
        return futures, "Binance Futures"
    if spot:
        return spot, "Binance Spot"
    raise ValueError(f"keine Binance-Kerzen für {symbol}USDT")


def get_klines(symbol: str, interval: str, limit: int = 1000) -> Dict[str, Any]:
    """Kerzen für das Chart auf der Detailseite, mit Angabe der Quelle."""
    klines, source = get_binance_klines(symbol, interval, limit)
    return {
        "source": source,
        "candles": [
            {
                "time": k[0] // 1000,  # Öffnungszeit in Sekunden (UTC)
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[7]),  # Quote-Volumen, also in USDT
            }
            for k in klines
        ],
    }


def get_daily_closes(symbol: str) -> List[float]:
    klines, _ = get_binance_klines(symbol, "1d", MA_PERIOD)
    return [float(k[4]) for k in klines]


def price_vs_ma200(closes: List[float]) -> Optional[Dict[str, Any]]:
    if len(closes) < MA_PERIOD:  # zu junge Historie: kein MA200 vortäuschen
        return None
    ma200 = sum(closes[-MA_PERIOD:]) / MA_PERIOD
    price = closes[-1]
    return {"price": price, "ma200": ma200, "distance": (price - ma200) / ma200}


def rsi(closes: List[float], period: int = RSI_PERIOD) -> Optional[Dict[str, Any]]:
    """RSI nach Wilder (geglättete Durchschnitte von Gewinnen und Verlusten)."""
    if len(closes) <= period:
        return None
    deltas = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
    if avg_loss == 0:
        return {"value": 100.0}
    return {"value": 100 - 100 / (1 + avg_gain / avg_loss)}


def get_dvol(symbol: str) -> Optional[Dict[str, Any]]:
    if symbol not in DVOL_SYMBOLS:
        return None
    end = int(time.time() * 1000)
    start = end - (DVOL_WINDOW + 2) * 86_400_000
    data = fetch_json(
        "https://www.deribit.com/api/v2/public/get_volatility_index_data"
        f"?currency={symbol}&start_timestamp={start}&end_timestamp={end}&resolution=1D"
    )["result"]["data"]
    closes = [float(c[4]) for c in data]  # [Zeit, Open, High, Low, Close]
    current = closes[-1]
    window = closes[-DVOL_WINDOW - 1:-1]
    mean = statistics.mean(window)
    return {"value": current, "mean": mean, "z": (current - mean) / statistics.stdev(window)}


def market_stats(c: Dict[str, Any]) -> Dict[str, Any]:
    """Kennzahlen für die Detailseite aus einem coins/markets-Eintrag (keine eigene Abfrage)."""
    def frac(key: str) -> Optional[float]:
        v = c.get(key)
        return v / 100 if v is not None else None

    return {
        "ath": c.get("ath"), "ath_date": c.get("ath_date"), "ath_change": frac("ath_change_percentage"),
        "atl": c.get("atl"), "atl_date": c.get("atl_date"), "atl_change": frac("atl_change_percentage"),
        "high_24h": c.get("high_24h"), "low_24h": c.get("low_24h"),
        "volume_24h": c.get("total_volume"),
        "fdv": c.get("fully_diluted_valuation"),
        "circulating_supply": c.get("circulating_supply"),
        "total_supply": c.get("total_supply"),
        "max_supply": c.get("max_supply"),
        "change": {p: frac(f"price_change_percentage_{p}_in_currency") for p in ("24h", "7d", "30d", "1y")},
    }


STATS_CHANGES = "&price_change_percentage=24h,7d,30d,1y"


def get_market_caps(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Marktkapitalisierung und Kennzahlen mehrerer Coins in einer Abfrage (CoinGecko limitiert stark)."""
    # include_tokens=top: bei mehrdeutigen Symbolen den Coin mit der höchsten Marktkapitalisierung
    coins = fetch_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&symbols={','.join(symbols).lower()}&include_tokens=top" + STATS_CHANGES
    )
    return {c["symbol"].upper(): {
        "market_cap": {"usd": float(c["market_cap"]), "rank": c.get("market_cap_rank")},
        "market_stats": market_stats(c),
        "name": c.get("name"),
        "coin_id": c.get("id"),
    } for c in coins}


def get_coingecko_coin(symbol: str, coin_id: Optional[str] = None) -> Dict[str, Any]:
    """Marktkapitalisierung, Kennzahlen und Kursmomentum eines Coins (CoinGecko).

    Mit coin_id eindeutig, sonst per Symbol (bei Mehrdeutigkeit der größte Coin)."""
    query = f"ids={coin_id}" if coin_id else f"symbols={symbol.lower()}&include_tokens=top"
    return _coingecko_coin(fetch_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&{query}&sparkline=true" + STATS_CHANGES
    )[0])


def get_coingecko_coins(ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Wie get_coingecko_coin, aber für viele Coins in einer Abfrage (Watchlist im Dashboard); Schlüssel: Coin-ID."""
    coins = fetch_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&ids={urllib.parse.quote(','.join(ids))}&per_page={len(ids)}&sparkline=true" + STATS_CHANGES
    )
    return {c["id"]: _coingecko_coin(c) for c in coins}


def _coingecko_coin(c: Dict[str, Any]) -> Dict[str, Any]:
    mcap, volume = c.get("market_cap"), c.get("total_volume")
    change_7d = c.get("price_change_percentage_7d_in_currency")
    change_30d = c.get("price_change_percentage_30d_in_currency")
    hourly = (c.get("sparkline_in_7d") or {}).get("price") or []
    rsi_hourly = rsi(hourly)
    return {
        "market_cap": {"usd": float(mcap), "rank": c.get("market_cap_rank")} if mcap else None,
        "market_stats": market_stats(c),
        "name": c.get("name"),
        "coin_id": c.get("id"),
        "momentum": {
            "change_7d": change_7d / 100 if change_7d is not None else None,
            "change_30d": change_30d / 100 if change_30d is not None else None,
            "turnover": volume / mcap if mcap and volume else None,
            "rsi_hourly": rsi_hourly["value"] if rsi_hourly else None,
        },
    }


MARKETS_FIELDS = "&price_change_percentage=1h,24h,7d,30d,1y"


def _market_row(c: Dict[str, Any]) -> Dict[str, Any]:
    def change(period: str) -> Optional[float]:
        v = c.get(f"price_change_percentage_{period}_in_currency")
        return v / 100 if v is not None else None

    return {
        "rank": c.get("market_cap_rank"),
        "id": c["id"],
        "symbol": c["symbol"].upper(),
        "name": c["name"],
        "image": c.get("image"),
        "price": c.get("current_price"),
        "market_cap": c.get("market_cap"),
        "volume": c.get("total_volume"),
        "change": {p: change(p) for p in ("1h", "24h", "7d", "30d", "1y")},
    }


def get_markets(page: int, per_page: int = 50, category: Optional[str] = None) -> List[Dict[str, Any]]:
    """Rangliste nach Marktkapitalisierung, seitenweise (CoinGecko); optional nur eine Sparte."""
    cat = f"&category={urllib.parse.quote(category)}" if category else ""
    coins = fetch_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&order=market_cap_desc&per_page={per_page}&page={page}{cat}" + MARKETS_FIELDS
    )
    return [_market_row(c) for c in coins]


WATCHLIST_MAX = 100


def get_markets_by_ids(ids: List[str]) -> List[Dict[str, Any]]:
    """Marktdaten bestimmter Coins (Watchlist) in einer Abfrage, nach Marktkapitalisierung sortiert."""
    coins = fetch_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&ids={urllib.parse.quote(','.join(ids))}&order=market_cap_desc"
        f"&per_page={len(ids)}" + MARKETS_FIELDS
    )
    return [_market_row(c) for c in coins]


def search_markets(query: str) -> List[Dict[str, Any]]:
    """Coins nach Name oder Symbol suchen (alle Coins auf CoinGecko, nicht nur die Top 1000).

    Zwei Abfragen: die Suche selbst (bis zu 25 Treffer, nach Relevanz) und die Marktdaten der
    Treffer in einem Rutsch. Treffer ohne Marktdaten erscheinen ohne Kurs."""
    found = fetch_json(
        "https://api.coingecko.com/api/v3/search?query=" + urllib.parse.quote(query)
    )["coins"][:25]
    if not found:
        return []
    ids = ",".join(c["id"] for c in found)
    markets = {c["id"]: _market_row(c) for c in fetch_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&ids={urllib.parse.quote(ids)}&per_page=25" + MARKETS_FIELDS
    )}
    empty = {p: None for p in ("1h", "24h", "7d", "30d", "1y")}
    return [markets.get(c["id"]) or {
        "rank": c.get("market_cap_rank"), "id": c["id"], "symbol": c["symbol"].upper(), "name": c["name"],
        "image": c.get("thumb"), "price": None, "market_cap": None, "volume": None, "change": empty,
    } for c in found]


def get_categories() -> List[Dict[str, Any]]:
    """Alle Sparten (Kategorien) mit Marktkap., 24-h-Änderung und Volumen – eine Abfrage (CoinGecko).

    Kategorien überschneiden sich (ein Coin steht oft in mehreren). Ohne Marktkap. ausgelassen."""
    cats = fetch_json("https://api.coingecko.com/api/v3/coins/categories")
    out = []
    for c in cats:
        mcap = c.get("market_cap")
        if not mcap:
            continue
        change = c.get("market_cap_change_24h")
        out.append({
            "id": c["id"],
            "name": c["name"],
            "market_cap": float(mcap),
            "change_24h": change / 100 if change is not None else None,
            "volume_24h": c.get("volume_24h"),
            "top_coins": [{"id": i, "image": img} for i, img in
                          zip(c.get("top_3_coins_id") or [], c.get("top_3_coins") or [])],
        })
    return out


# --- Coin-Profil (Seite „Über den Coin“) ---------------------------------------
# Eine CoinGecko-Abfrage je Coin (coins/{id}), vom Server einen Tag gecacht: Beschreibung,
# Kategorien, Links, Angebot. Die Einschätzung daraus ist regelbasiert (coin_assessment).

# Kategorien: Stichwort im CoinGecko-Namen (klein geschrieben) → deutscher Name und kurze Erklärung.
# Reihenfolge zählt: der erste Treffer gilt (z. B. „liquid restaking“ vor „restaking“).
CATEGORY_DE = [
    ("stablecoin", "Stablecoin", "An einen festen Wert gekoppelt, meist den US-Dollar. Soll nicht schwanken; "
     "das Risiko liegt in der Deckung und beim Herausgeber."),
    ("meme", "Memecoin", "Kaum oder kein technischer Nutzen. Der Kurs lebt von Aufmerksamkeit, Community und "
     "Trends – entsprechend stark schwankt er."),
    ("layer 1", "Layer 1", "Eigene Blockchain mit eigenem Konsens. Der Coin bezahlt die Gebühren im Netz und "
     "sichert es (Staking oder Mining)."),
    ("layer 2", "Layer 2", "Baut auf einer anderen Blockchain auf (meist Ethereum), bündelt Transaktionen und "
     "macht sie schneller und günstiger."),
    ("layer 0", "Layer 0", "Infrastruktur, auf der eigene Blockchains aufgesetzt und verbunden werden."),
    ("smart contract platform", "Smart-Contract-Plattform", "Auf der Chain laufen Programme (Smart Contracts), "
     "etwa für DeFi, NFTs oder Spiele. Je mehr Anwendungen, desto mehr Nachfrage nach dem Coin für Gebühren."),
    ("proof of work", "Proof of Work", "Das Netz wird durch Mining gesichert: Rechenleistung gegen neue Coins."),
    ("proof of stake", "Proof of Stake", "Das Netz wird durch hinterlegte Coins (Staking) gesichert; "
     "Staker erhalten dafür Belohnungen."),
    ("liquid restaking", "Liquid Restaking", "Quittung für erneut eingesetzte (restakte) Coins; folgt dem "
     "Kurs des zugrunde liegenden Coins, mit zusätzlichem Protokollrisiko."),
    ("liquid staking", "Liquid Staking", "Handelbare Quittung für gestakte Coins; folgt dem Kurs des "
     "zugrunde liegenden Coins plus Staking-Ertrag."),
    ("restaking", "Restaking", "Bereits gestakte Coins sichern zusätzlich weitere Dienste ab – mehr Ertrag, "
     "mehr Risiko."),
    ("wrapped", "Wrapped Token", "Abbild eines anderen Coins auf einer fremden Blockchain; folgt dessen Kurs. "
     "Risiko: die Verwahrung bzw. Brücke dahinter."),
    ("bridged", "Bridged Token", "Über eine Brücke auf eine andere Chain gebrachtes Abbild; folgt dem Original. "
     "Risiko: die Brücke."),
    ("decentralized exchange", "DEX", "Dezentrale Börse: Handel direkt aus der eigenen Wallet. Der Token "
     "profitiert meist vom Handelsvolumen (Gebühren, Stimmrechte)."),
    ("perpetual", "Perpetuals", "Handel mit gehebelten Terminkontrakten ohne Laufzeit. Einnahmen hängen am "
     "Handelsvolumen, also an der Marktaktivität."),
    ("derivative", "Derivate", "Handel mit Terminkontrakten, Optionen o. Ä. auf der Blockchain."),
    ("lending", "Kredite", "Verleihen und Leihen von Coins gegen Sicherheiten; Einnahmen aus Zinsen."),
    ("yield", "Rendite-Protokoll", "Bündelt Anlagestrategien, um Ertrag auf Coins zu erzielen."),
    ("decentralized finance", "DeFi", "Finanzdienste ohne Bank: Handel, Kredite, Zinsen – als Smart Contracts."),
    ("oracle", "Oracle", "Bringt Daten von außen (Kurse, Ereignisse) auf die Blockchain, damit Smart Contracts "
     "darauf reagieren können."),
    ("real world asset", "RWA", "Bringt reale Werte wie Anleihen, Immobilien oder Rohstoffe als Token auf die "
     "Blockchain."),
    ("rwa", "RWA", "Bringt reale Werte wie Anleihen, Immobilien oder Rohstoffe als Token auf die Blockchain."),
    ("tokenized gold", "Gold-Token", "Token, die durch physisches Gold gedeckt sind; folgen dem Goldpreis."),
    ("gold", "Gold-Token", "Token, die durch physisches Gold gedeckt sind; folgen dem Goldpreis."),
    ("ai agent", "KI-Agenten", "Software-Agenten mit künstlicher Intelligenz, die selbstständig auf der "
     "Blockchain handeln. Junges, sehr spekulatives Feld."),
    ("artificial intelligence", "KI", "Projekte rund um künstliche Intelligenz, etwa Rechenleistung, Daten oder "
     "Modelle. Stark von Trends getrieben."),
    ("gaming", "Gaming", "Spiele oder Spiele-Plattformen; der Token dient als Spielwährung oder Belohnung."),
    ("play to earn", "Play to Earn", "Spieler verdienen Token durch Spielen. Das Modell hängt am Zustrom neuer "
     "Spieler."),
    ("metaverse", "Metaverse", "Virtuelle Welten mit eigenen Grundstücken, Gegenständen und Währung."),
    ("non-fungible", "NFT", "Einzigartige digitale Objekte (Kunst, Sammlerstücke, Rechte) auf der Blockchain."),
    ("nft", "NFT", "Einzigartige digitale Objekte (Kunst, Sammlerstücke, Rechte) auf der Blockchain."),
    ("depin", "DePIN", "Dezentrale physische Infrastruktur: Nutzer stellen Hardware bereit (Funk, Speicher, "
     "Rechenleistung) und werden in Token bezahlt."),
    ("storage", "Speicher", "Dezentraler Datenspeicher, bezahlt in Token."),
    ("privacy coin", "Privacy-Coin", "Transaktionen sind (teilweise) anonym. Technisch interessant, regulatorisch "
     "heikel (Delistings an Börsen möglich)."),
    ("privacy", "Privatsphäre", "Technik für vertrauliche Transaktionen oder Daten."),
    ("zero knowledge", "Zero Knowledge", "Kryptografische Beweise, mit denen sich etwas belegen lässt, ohne die "
     "Daten offenzulegen – Grundlage für Skalierung und Privatsphäre."),
    ("cross-chain", "Interoperabilität", "Verbindet verschiedene Blockchains, damit Werte und Daten zwischen "
     "ihnen fließen können."),
    ("interoperability", "Interoperabilität", "Verbindet verschiedene Blockchains, damit Werte und Daten "
     "zwischen ihnen fließen können."),
    ("exchange-based", "Börsen-Token", "Token einer Krypto-Börse; bringt oft Gebührenrabatte. Hängt stark am "
     "Geschäft und an der Regulierung der Börse."),
    ("centralized exchange", "Börsen-Token", "Token einer Krypto-Börse; bringt oft Gebührenrabatte. Hängt stark "
     "am Geschäft und an der Regulierung der Börse."),
    ("payment", "Zahlungen", "Für schnelle und günstige Zahlungen gedacht."),
    ("socialfi", "SocialFi", "Soziale Netzwerke mit eigenem Token, z. B. für Belohnungen oder Mitgliedschaften."),
    ("governance", "Governance", "Der Token gibt Stimmrechte über die Weiterentwicklung des Protokolls."),
    ("launchpad", "Launchpad", "Plattform für Verkäufe neuer Token; Inhaber erhalten oft Vorkaufsrechte."),
    ("infrastructure", "Infrastruktur", "Grundlegende Technik, auf der andere Projekte aufbauen."),
    ("rollup", "Rollup", "Bündelt Transaktionen und schreibt sie gesammelt auf eine Basis-Blockchain."),
]
# Englische Fassung je deutschem Kategorienamen aus CATEGORY_DE: (Name, Erklärung)
CATEGORY_EN = {
    "Stablecoin": ("Stablecoin", "Pegged to a fixed value, usually the US dollar. Not supposed to fluctuate; "
                   "the risk lies in the backing and the issuer."),
    "Memecoin": ("Memecoin", "Little or no technical utility. The price lives on attention, community and trends – "
                 "and swings accordingly."),
    "Layer 1": ("Layer 1", "Its own blockchain with its own consensus. The coin pays the network fees and secures it "
                "(staking or mining)."),
    "Layer 2": ("Layer 2", "Builds on another blockchain (usually Ethereum), bundles transactions and makes them faster "
                "and cheaper."),
    "Layer 0": ("Layer 0", "Infrastructure on which separate blockchains are launched and connected."),
    "Smart-Contract-Plattform": ("Smart contract platform", "Programs (smart contracts) run on the chain, e.g. for DeFi, "
                                 "NFTs or games. The more applications, the more demand for the coin to pay fees."),
    "Proof of Work": ("Proof of Work", "The network is secured by mining: computing power in exchange for new coins."),
    "Proof of Stake": ("Proof of Stake", "The network is secured by deposited coins (staking); stakers earn rewards for it."),
    "Liquid Restaking": ("Liquid Restaking", "Receipt for restaked coins; follows the price of the underlying coin, with "
                         "additional protocol risk."),
    "Liquid Staking": ("Liquid Staking", "Tradable receipt for staked coins; follows the price of the underlying coin plus "
                       "staking yield."),
    "Restaking": ("Restaking", "Already staked coins additionally secure other services – more yield, more risk."),
    "Wrapped Token": ("Wrapped token", "Representation of another coin on a different blockchain; follows its price. "
                      "Risk: the custody or bridge behind it."),
    "Bridged Token": ("Bridged token", "Copy brought to another chain via a bridge; follows the original. Risk: the bridge."),
    "DEX": ("DEX", "Decentralized exchange: trading directly from your own wallet. The token usually benefits from trading "
            "volume (fees, voting rights)."),
    "Perpetuals": ("Perpetuals", "Trading leveraged futures without expiry. Revenue depends on trading volume, i.e. market "
                   "activity."),
    "Derivate": ("Derivatives", "Trading futures, options and the like on the blockchain."),
    "Kredite": ("Lending", "Lending and borrowing coins against collateral; revenue from interest."),
    "Rendite-Protokoll": ("Yield protocol", "Bundles investment strategies to earn yield on coins."),
    "DeFi": ("DeFi", "Financial services without a bank: trading, loans, interest – as smart contracts."),
    "Oracle": ("Oracle", "Brings outside data (prices, events) onto the blockchain so smart contracts can react to it."),
    "RWA": ("RWA", "Brings real-world assets such as bonds, real estate or commodities onto the blockchain as tokens."),
    "Gold-Token": ("Gold token", "Tokens backed by physical gold; they follow the gold price."),
    "KI-Agenten": ("AI agents", "Software agents with artificial intelligence acting autonomously on the blockchain. "
                   "Young, highly speculative field."),
    "KI": ("AI", "Projects around artificial intelligence, e.g. computing power, data or models. Strongly trend-driven."),
    "Gaming": ("Gaming", "Games or gaming platforms; the token serves as in-game currency or reward."),
    "Play to Earn": ("Play to Earn", "Players earn tokens by playing. The model depends on an influx of new players."),
    "Metaverse": ("Metaverse", "Virtual worlds with their own land, items and currency."),
    "NFT": ("NFT", "Unique digital objects (art, collectibles, rights) on the blockchain."),
    "DePIN": ("DePIN", "Decentralized physical infrastructure: users provide hardware (wireless, storage, computing) and "
              "are paid in tokens."),
    "Speicher": ("Storage", "Decentralized data storage, paid in tokens."),
    "Privacy-Coin": ("Privacy coin", "Transactions are (partly) anonymous. Technically interesting, tricky from a regulatory "
                     "point of view (exchange delistings possible)."),
    "Privatsphäre": ("Privacy", "Technology for confidential transactions or data."),
    "Zero Knowledge": ("Zero Knowledge", "Cryptographic proofs that show something without revealing the data – the basis "
                       "for scaling and privacy."),
    "Interoperabilität": ("Interoperability", "Connects different blockchains so value and data can flow between them."),
    "Börsen-Token": ("Exchange token", "Token of a crypto exchange; often brings fee discounts. Depends heavily on the "
                     "exchange's business and regulation."),
    "Zahlungen": ("Payments", "Meant for fast and cheap payments."),
    "SocialFi": ("SocialFi", "Social networks with their own token, e.g. for rewards or memberships."),
    "Governance": ("Governance", "The token grants voting rights on the protocol's development."),
    "Launchpad": ("Launchpad", "Platform for sales of new tokens; holders often get priority access."),
    "Infrastruktur": ("Infrastructure", "Basic technology that other projects build on."),
    "Rollup": ("Rollup", "Bundles transactions and writes them to a base blockchain in batches."),
}
# Kategorien, die nichts über den Coin selbst sagen (Ökosysteme, Indizes, Portfolios von Investoren …)
CATEGORY_SKIP = re.compile(r"ecosystem|index|portfolio|holdings|made in|hodler|alpha|binance|coinbase|"
                           r"world liberty|launchpool|native$|tokens? list|^fdv|x402|-themed|inspired", re.I)
CATEGORY_MAX = 12

# Chain-IDs von CoinGecko → Anzeigename (für „Token auf …“); andere werden aus der ID abgeleitet
PLATFORM_NAMES = {
    "ethereum": "Ethereum", "solana": "Solana", "binance-smart-chain": "BNB Chain", "base": "Base",
    "arbitrum-one": "Arbitrum", "optimistic-ethereum": "Optimism", "polygon-pos": "Polygon", "avalanche": "Avalanche",
    "tron": "Tron", "the-open-network": "TON", "sui": "Sui", "aptos": "Aptos", "hyperliquid": "Hyperliquid",
    "hyperevm": "HyperEVM", "near-protocol": "NEAR", "cardano": "Cardano", "stellar": "Stellar", "xrp": "XRP Ledger",
    "cosmos": "Cosmos", "osmosis": "Osmosis", "zksync": "zkSync", "linea": "Linea", "scroll": "Scroll",
    "mantle": "Mantle", "sonic": "Sonic", "berachain": "Berachain", "blast": "Blast", "fantom": "Fantom",
}

PROFILE_DESC_MAX = 8000   # Zeichen der Beschreibung (manche Projekte schreiben Romane)


def _plain_paragraphs(text: str) -> List[str]:
    """CoinGecko-Beschreibung (HTML mit Links) als reine Absätze."""
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))[:PROFILE_DESC_MAX]
    return [p.strip() for p in re.split(r"\n\s*\n|\r\n\r\n", text) if p.strip()]


def _categories_de(names: List[str], lang: str = "de") -> List[Dict[str, Any]]:
    """Aussagekräftige Kategorien ohne Doppelungen (gleicher deutscher Name).

    id: deutscher Name (Schlüssel für coin_type), label/text: Anzeige in der Sprache, name: Original von CoinGecko."""
    out, seen = [], set()
    for name in names or []:
        if not name or CATEGORY_SKIP.search(name):
            continue
        low = name.lower()
        de = next(((n, t) for key, n, t in CATEGORY_DE if key in low), None)
        cid = de[0] if de else name
        if cid in seen:
            continue
        seen.add(cid)
        label, text = (CATEGORY_EN.get(cid, (cid, de[1])) if lang == "en" else de) if de else (name, None)
        out.append({"name": name, "id": cid, "label": label, "text": text})
    return out[:CATEGORY_MAX]


def _platform_name(pid: str) -> str:
    return PLATFORM_NAMES.get(pid) or pid.replace("-", " ").title()


def coin_type(cats: List[Dict[str, Any]], platform: Optional[str], lang: str = "de") -> Dict[str, str]:
    """Grobe Art des Coins: bestimmt, wovon der Kurs im Kern abhängt."""
    L = _l(lang)
    labels = {c["id"] for c in cats}
    on = (L(" auf ", " on ") + _platform_name(platform)) if platform else ""
    own = on or L(" mit eigener Blockchain", " with its own blockchain")
    if "Stablecoin" in labels:
        return {"key": "stable", "label": "Stablecoin" + on,
                "text": L("Soll den Wert seiner Kopplung (meist 1 US-Dollar) halten. Kein Anlageobjekt für Kursgewinne; "
                          "entscheidend sind Deckung, Transparenz und Herausgeber.",
                          "Meant to hold the value of its peg (usually 1 US dollar). Not an investment for price gains; "
                          "what matters is backing, transparency and the issuer.")}
    if labels & {"Wrapped Token", "Bridged Token", "Liquid Staking", "Liquid Restaking", "Gold-Token"}:
        return {"key": "derivative", "label": L("Abgeleiteter Token", "Derivative token") + on,
                "text": L("Bildet einen anderen Wert ab (einen Coin, gestakte Coins oder Gold) und folgt dessen Kurs. "
                          "Zusätzliches Risiko: Verwahrung, Brücke oder Protokoll dahinter.",
                          "Represents another asset (a coin, staked coins or gold) and follows its price. "
                          "Additional risk: the custody, bridge or protocol behind it.")}
    if "Memecoin" in labels:
        return {"key": "meme", "label": "Memecoin" + own,
                "text": L("Kein nennenswerter Nutzen hinter dem Token; der Kurs hängt fast nur an Stimmung, "
                          "Community und Aufmerksamkeit.",
                          "No significant utility behind the token; the price depends almost entirely on sentiment, "
                          "community and attention.")}
    if "Börsen-Token" in labels:
        return {"key": "exchange", "label": L("Börsen-Token", "Exchange token") + own,
                "text": L("Der Wert hängt am Geschäft der Börse (Handelsvolumen, Gebührenrabatte, Rückkäufe) und an "
                          "deren Regulierung.",
                          "The value depends on the exchange's business (trading volume, fee discounts, buybacks) and "
                          "its regulation.")}
    if not platform:
        return {"key": "native", "label": L("Nativer Coin einer eigenen Blockchain", "Native coin of its own blockchain"),
                "text": L("Der Coin ist die Grundwährung seiner Blockchain: Mit ihm werden Gebühren bezahlt und das Netz "
                          "gesichert. Sein Wert hängt an der Nutzung dieses Netzes.",
                          "The coin is the base currency of its blockchain: it pays the fees and secures the network. "
                          "Its value depends on how much the network is used.")}
    return {"key": "token", "label": "Token" + on,
            "text": L(f"Läuft als Token{on} und nutzt deren Sicherheit. Sein Wert hängt am Erfolg der Anwendung "
                      "dahinter und daran, welchen Nutzen der Token darin hat (Gebühren, Stimmrechte, Anteil an "
                      "Einnahmen).",
                      f"Runs as a token{on} and uses its security. Its value depends on the success of the application "
                      "behind it and on the token's role in it (fees, voting rights, share of revenue).")}


def _frac(v: Optional[float]) -> Optional[float]:
    return v / 100 if v is not None else None


def coin_assessment(kind: str, f: Dict[str, Any], lang: str = "de") -> Dict[str, Any]:
    """Regelbasierte Einschätzung aus Größe, Art, Angebot, Handel und Kursgeschichte.

    Ergebnis: Risikostufe (für Krypto-Verhältnisse) mit Punkten je Faktor, Stärken, Risiken.
    Erfahrungswerte, keine Bewertung des Projekts selbst. risk.key bleibt deutsch (CSS-Klasse)."""
    L = _l(lang)
    pro, contra, notes = [], [], []
    risk = 0
    rank = f.get("rank")
    if rank is None:
        risk += 4
        contra.append(L("Ohne Rang bei CoinGecko: sehr klein oder kaum gehandelt.",
                        "No rank on CoinGecko: very small or hardly traded."))
    elif rank <= 10:
        pro.append(L(f"Einer der zehn größten Coins (Rang {rank}): etabliert, viel Liquidität, breite Börsenlistung.",
                     f"One of the ten largest coins (rank {rank}): established, deep liquidity, widely listed."))
    elif rank <= 50:
        risk += 1
        pro.append(L(f"Große Marktkapitalisierung (Rang {rank}): bekannt und gut handelbar.",
                     f"Large market cap (rank {rank}): well known and easy to trade."))
    elif rank <= 200:
        risk += 2
        notes.append(L(f"Mittlere Größe (Rang {rank}): mehr Potenzial, aber auch deutlich größere Schwankungen als "
                       "bei den Großen.",
                       f"Mid-sized (rank {rank}): more potential, but also much larger swings than the big ones."))
    elif rank <= 500:
        risk += 3
        contra.append(L(f"Kleiner Coin (Rang {rank}): stark schwankend, kann leicht bewegt werden.",
                        f"Small coin (rank {rank}): highly volatile, easy to move."))
    else:
        risk += 4
        contra.append(L(f"Sehr kleiner Coin (Rang {rank}): hohes Risiko, Kurse lassen sich leicht bewegen.",
                        f"Very small coin (rank {rank}): high risk, prices are easy to move."))

    if kind == "meme":
        risk += 2
        contra.append(L("Memecoin: kein fundamentaler Wert dahinter; Einbrüche von 80–90 % sind üblich, wenn die "
                        "Aufmerksamkeit nachlässt.",
                        "Memecoin: no fundamental value behind it; crashes of 80–90% are common once attention fades."))

    mc_fdv = f.get("mc_fdv")   # Anteil der Marktkap. am voll verwässerten Wert = Anteil im Umlauf
    if mc_fdv is not None and kind not in ("stable", "derivative"):
        share = round(mc_fdv * 100)
        if mc_fdv < 0.5:
            risk += 2
            contra.append(L(f"Erst {share} % der Coins im Umlauf: Freigaben (Unlocks) für Team und "
                            "Investoren können den Kurs über Jahre drücken.",
                            f"Only {share}% of coins in circulation: unlocks for team and investors can weigh on "
                            "the price for years."))
        elif mc_fdv < 0.8:
            risk += 1
            contra.append(L(f"{share} % der Coins im Umlauf: weitere Freigaben verwässern noch.",
                            f"{share}% of coins in circulation: further unlocks still dilute."))
        elif mc_fdv >= 0.95:
            pro.append(L("Fast alle Coins sind bereits im Umlauf: kaum Verwässerung durch neue Freigaben.",
                         "Almost all coins are already in circulation: hardly any dilution from new unlocks."))
    if kind not in ("stable", "derivative"):
        if f.get("max_supply"):
            pro.append(L("Feste Obergrenze der Menge: Es können nicht beliebig viele Coins entstehen.",
                         "Fixed supply cap: coins cannot be created without limit."))
        elif f.get("max_supply_infinite") or f.get("total_supply"):
            notes.append(L("Keine feste Obergrenze: Neue Coins können laufend hinzukommen (Inflation). Entscheidend "
                           "ist, ob Gebühren-Verbrennung oder Nachfrage das ausgleichen.",
                           "No fixed cap: new coins can keep being added (inflation). What matters is whether fee "
                           "burning or demand offsets it."))

    turnover = f.get("turnover")
    if turnover is not None and kind != "stable":
        if turnover < 0.01:
            risk += 1
            contra.append(L("Wenig Handel (unter 1 % der Marktkap. am Tag): größere Käufe oder Verkäufe bewegen den "
                            "Kurs spürbar.",
                            "Little trading (under 1% of market cap per day): larger buys or sells move the price "
                            "noticeably."))
        elif turnover > 0.5:
            risk += 1
            contra.append(L(f"Sehr reger Handel ({round(turnover * 100)} % der Marktkap. am Tag): spricht für "
                            "kurzfristige Spekulation.",
                            f"Very heavy trading ({round(turnover * 100)}% of market cap per day): points to "
                            "short-term speculation."))
        elif turnover >= 0.03:
            pro.append(L("Gesunder Handel: genug Umsatz, um ohne große Kurssprünge zu kaufen oder zu verkaufen.",
                         "Healthy trading: enough volume to buy or sell without big price jumps."))

    ath_change = f.get("ath_change")
    if ath_change is not None and kind not in ("stable", "derivative"):
        below = round(-ath_change * 100)
        if ath_change <= -0.9:
            risk += 1
            contra.append(L(f"{below} % unter dem Allzeithoch: Viele frühere Käufer liegen im Minus; "
                            "eine Rückkehr zum Hoch ist bei so tiefen Abstürzen selten.",
                            f"{below}% below the all-time high: many earlier buyers are at a loss; a return to the "
                            "high is rare after crashes this deep."))
        elif ath_change <= -0.7:
            notes.append(L(f"{below} % unter dem Allzeithoch: Der Kurs hat einen großen Teil "
                           "seines früheren Werts verloren.",
                           f"{below}% below the all-time high: the price has lost a large part of its former value."))
        elif ath_change >= -0.1:
            notes.append(L("Nahe am Allzeithoch: starke Phase, aber Käufe hier sind historisch teuer.",
                           "Close to the all-time high: strong phase, but buying here has historically been expensive."))

    change_1y = f.get("change_1y")
    if change_1y is not None and kind not in ("stable", "derivative") and change_1y <= -0.7:
        risk += 1
        contra.append(L(f"Im letzten Jahr {pct_word(change_1y)}: anhaltender Abwärtstrend.",
                        f"{pct_word(change_1y, 'en')} over the last year: persistent downtrend."))

    age_days = f.get("age_days")
    if age_days is not None:
        if age_days < 365:
            risk += 1
            contra.append(L("Jünger als ein Jahr: wenig Erfahrung, wie sich der Coin in schwachen Marktphasen hält.",
                            "Less than a year old: little evidence of how the coin holds up in weak markets."))
        elif age_days > 5 * 365:
            years = int(age_days // 365)
            pro.append(L(f"Seit über {years} Jahren am Markt: hat schon mehrere Marktzyklen überstanden.",
                         f"On the market for over {years} years: has already survived several market cycles."))

    if f.get("notice"):
        risk += 1
        contra.append(L("CoinGecko zeigt einen Warnhinweis zu diesem Coin (siehe oben).",
                        "CoinGecko shows a warning for this coin (see above)."))

    if kind == "stable":
        level = {"key": "stable", "label": "Stablecoin",
                 "text": L("Kursrisiko gering, solange die Kopplung hält. Das eigentliche Risiko sind Deckung und "
                           "Herausgeber: Bricht die Kopplung, kann der Wert schnell fallen.",
                           "Low price risk as long as the peg holds. The real risk is backing and issuer: if the peg "
                           "breaks, the value can drop quickly.")}
    else:
        if kind == "derivative":
            notes.append(L("Folgt einem anderen Wert: Für die Einschätzung zählt vor allem der zugrunde liegende Coin.",
                           "Follows another asset: what matters most for the assessment is the underlying coin."))
        levels = [(1, "moderat", L("moderat", "moderate"),
                   L("Für Krypto-Verhältnisse vergleichsweise solide. Schwankungen von 50 % und mehr sind trotzdem möglich.",
                     "Comparatively solid by crypto standards. Swings of 50% and more are still possible.")),
                  (3, "erhöht", L("erhöht", "elevated"),
                   L("Deutlich schwankungsanfälliger als die großen Coins; nur mit Geld, auf das man länger verzichten kann.",
                     "Much more volatile than the big coins; only with money you can do without for a long time.")),
                  (5, "hoch", L("hoch", "high"),
                   L("Spekulativ: hohe Gewinne wie Verluste möglich; höchstens als kleine Beimischung.",
                     "Speculative: large gains and losses possible; at most a small addition to a portfolio.")),
                  (99, "sehr hoch", L("sehr hoch", "very high"),
                   L("Hochspekulativ: Totalverlust ist ein realistisches Szenario.",
                     "Highly speculative: total loss is a realistic scenario."))]
        key, label, text = next((k, lb, t) for limit, k, lb, t in levels if risk <= limit)
        level = {"key": key.replace(" ", "-"), "label": label, "text": text}
    return {"risk": level, "risk_points": risk, "pro": pro, "contra": contra, "notes": notes}


def pct_word(v: float, lang: str = "de") -> str:
    """Änderung als Wort, z. B. „−72 %“ → „72 % verloren“ bzw. „lost 72%“."""
    if lang == "en":
        return f"lost {round(-v * 100)}%" if v < 0 else f"gained {round(v * 100)}%"
    return f"{round(-v * 100)} % verloren" if v < 0 else f"{round(v * 100)} % gewonnen"


def resolve_coin_id(symbol: str) -> Optional[str]:
    """CoinGecko-ID zu einem Symbol (bei Mehrdeutigkeit der größte Coin); eine Abfrage."""
    coins = fetch_json("https://api.coingecko.com/api/v3/coins/markets"
                       f"?vs_currency=usd&symbols={symbol.lower()}&include_tokens=top")
    return coins[0]["id"] if coins else None


def get_coin_profile(coin_id: str) -> Dict[str, Dict[str, Any]]:
    """Beschreibung, Kategorien, Fakten, Links und Einschätzung eines Coins (eine CoinGecko-Abfrage),
    je Sprache aus LANGS: {"de": {...}, "en": {...}}."""
    d = fetch_json(f"https://api.coingecko.com/api/v3/coins/{urllib.parse.quote(coin_id)}"
                   "?localization=true&tickers=false&market_data=true&community_data=false"
                   "&developer_data=false&sparkline=false")
    return {lang: _coin_profile(d, lang) for lang in LANGS}


def _coin_profile(d: Dict[str, Any], ui_lang: str) -> Dict[str, Any]:
    md = d.get("market_data") or {}
    usd = lambda key: (md.get(key) or {}).get("usd")   # noqa: E731
    desc = d.get("description") or {}
    de, en = _plain_paragraphs(desc.get("de", "")), _plain_paragraphs(desc.get("en", ""))
    # CoinGecko füllt „de“ oft nicht oder kopiert den englischen Text hinein
    lang = "de" if ui_lang == "de" and de and de != en else "en"
    platform = d.get("asset_platform_id")
    cats = _categories_de(d.get("categories") or [], ui_lang)
    kind = coin_type(cats, platform, ui_lang)
    mcap, volume = usd("market_cap"), usd("total_volume")
    genesis = d.get("genesis_date")
    age_days = None
    if genesis:
        try:
            age_days = (datetime.now() - datetime.strptime(genesis, "%Y-%m-%d")).days
        except ValueError:
            genesis = None
    links = d.get("links") or {}
    first = lambda xs: next((x for x in xs or [] if x), None)   # noqa: E731
    twitter = links.get("twitter_screen_name")
    notice = re.sub(r"<[^>]+>", "", html.unescape(d.get("public_notice") or "")).strip() or None
    facts = {
        "rank": d.get("market_cap_rank") or md.get("market_cap_rank"),
        "market_cap": mcap, "fdv": usd("fully_diluted_valuation"), "volume_24h": volume,
        "turnover": volume / mcap if mcap and volume else None,
        "mc_fdv": md.get("market_cap_fdv_ratio"),
        "circulating_supply": md.get("circulating_supply"), "total_supply": md.get("total_supply"),
        "max_supply": md.get("max_supply"), "max_supply_infinite": md.get("max_supply_infinite"),
        "price": usd("current_price"),
        "ath": usd("ath"), "ath_date": (md.get("ath_date") or {}).get("usd"), "ath_change": _frac(usd("ath_change_percentage")),
        "atl": usd("atl"), "atl_date": (md.get("atl_date") or {}).get("usd"), "atl_change": _frac(usd("atl_change_percentage")),
        "change_1y": _frac(md.get("price_change_percentage_1y")),
        "tvl": usd("total_value_locked"),
        "genesis_date": genesis, "age_days": age_days,
        "hashing_algorithm": d.get("hashing_algorithm"),
        "block_time_minutes": d.get("block_time_in_minutes") or None,
        "platform": _platform_name(platform) if platform else None,
        "chains": len([p for p in (d.get("platforms") or {}) if p]),
        "country": d.get("country_origin") or None,
        "watchlists": d.get("watchlist_portfolio_users"),
        "votes_up": _frac(d.get("sentiment_votes_up_percentage")),
        "notice": notice,
    }
    return {
        "id": d.get("id"), "symbol": (d.get("symbol") or "").upper(), "name": d.get("name"),
        "image": (d.get("image") or {}).get("large"),
        "description": {"lang": lang, "paragraphs": de if lang == "de" else en},
        "categories": cats,
        "type": kind,
        "facts": facts,
        "links": {
            "homepage": first(links.get("homepage")),
            "whitepaper": links.get("whitepaper") or None,
            "github": first((links.get("repos_url") or {}).get("github")),
            "explorer": first(links.get("blockchain_site")),
            "twitter": f"https://x.com/{twitter}" if twitter else None,
            "reddit": links.get("subreddit_url") if (links.get("subreddit_url") or "").rstrip("/").count("/") > 3 else None,
            "coingecko": f"https://www.coingecko.com/en/coins/{d.get('id')}",
        },
        "assessment": coin_assessment(kind["key"], facts, ui_lang),
    }


# DefiLlama: TVL je Kategorie und Chain, aus der Protokoll-Liste summiert (eine Abfrage, ~10 MB, ohne Key).
# Ohne Börsen (CEX), Chains selbst und Bridges (deren TVL liegt doppelt auch auf den Ziel-Chains).
DEFI_EXCLUDED = {"CEX", "Chain", "Bridge", "Canonical Bridge", "Cross Chain Bridge"}
# Chain-Namen in den Protokolldaten → Namen in /v2/chains
DEFI_CHAIN_ALIASES = {"Binance": "BSC", "Optimism": "OP Mainnet", "xDai": "Gnosis", "Pulse": "PulseChain", "RSK": "Rootstock"}
DEFI_CHAIN_EXTRA = ("staking", "pool2", "borrowed", "vesting", "treasury", "offers", "doublecounted")


def get_defi_tvl() -> Dict[str, Any]:
    """TVL heute und vor 1 bzw. 7 Tagen je Kategorie und Chain.

    Frühere Werte ergeben sich aus der Änderung je Protokoll (tvl / (1 + Änderung)); Protokolle
    ohne Angabe zählen als unverändert. TVL enthält Kurseffekte: fällt ETH, sinkt auch der TVL.
    Chains: TVL-Stand aus /v2/chains (wie auf DefiLlama, ohne Doppelzählungen), die Änderung aus
    den summierten Protokollen; die Summe der Chains ist der Gesamtwert."""
    protocols = fetch_json("https://api.llama.fi/protocols")
    official = {c["name"]: float(c["tvl"]) for c in fetch_json("https://api.llama.fi/v2/chains") if c.get("tvl")}

    def prev(tvl: float, change: Optional[float]) -> float:
        return tvl / (1 + change / 100) if change is not None and change > -100 else tvl

    def bucket() -> Dict[str, Any]:
        return {"tvl": 0.0, "prev_1d": 0.0, "prev_7d": 0.0, "protocols": 0, "top": []}

    cats: Dict[str, Dict[str, Any]] = {}
    chains: Dict[str, Dict[str, Any]] = {}
    total = bucket()
    for p in protocols:
        tvl, cat = p.get("tvl"), p.get("category")
        if not tvl or tvl <= 0 or not cat or cat in DEFI_EXCLUDED:
            continue
        c1, c7 = p.get("change_1d"), p.get("change_7d")
        entry = {"name": p["name"], "tvl": tvl, "logo": p.get("logo")}
        targets = [(total, tvl), (cats.setdefault(cat, bucket()), tvl)]
        for chain, v in (p.get("chainTvls") or {}).items():
            if "-" in chain or chain in DEFI_CHAIN_EXTRA or not isinstance(v, (int, float)) or v <= 0:
                continue
            chain = DEFI_CHAIN_ALIASES.get(chain, chain)
            targets.append((chains.setdefault(chain, bucket()), float(v)))
        for b, v in targets:
            b["tvl"] += v
            b["prev_1d"] += prev(v, c1)
            b["prev_7d"] += prev(v, c7)
            b["protocols"] += 1
            if b is not total:
                b["top"].append({**entry, "tvl": v})

    def finish(name: str, b: Dict[str, Any]) -> Dict[str, Any]:
        top = sorted(b["top"], key=lambda x: -x["tvl"])[:3]
        return {
            "name": name, "tvl": b["tvl"], "protocols": b["protocols"],
            "change_1d": b["tvl"] / b["prev_1d"] - 1 if b["prev_1d"] else None,
            "change_7d": b["tvl"] / b["prev_7d"] - 1 if b["prev_7d"] else None,
            "top": top,
        }

    chain_list = []
    for name, tvl in official.items():
        agg = finish(name, chains[name]) if name in chains else {"name": name, "protocols": 0, "change_1d": None, "change_7d": None, "top": []}
        chain_list.append({**agg, "tvl": tvl})
    chain_list.sort(key=lambda x: -x["tvl"])
    tot = finish("DeFi gesamt", total)
    return {
        "total": {**tot, "tvl": sum(official.values())},
        "categories": sorted((finish(k, v) for k, v in cats.items()), key=lambda x: -x["tvl"]),
        "chains": chain_list[:60],
    }


# Gebühren und Einnahmen (DefiLlama): Zuordnung Coin → Protokoll oder Chain über die CoinGecko-ID
FEE_TYPES = {"fees": "dailyFees", "revenue": "dailyRevenue", "holders": "dailyHoldersRevenue"}
FEE_WEEKS = 52


def _llama_slug(name: str) -> str:
    return "".join(ch for ch in name.lower().replace(" ", "-") if ch.isalnum() or ch in "-.")


def get_llama_index() -> Dict[str, Dict[str, Dict[str, str]]]:
    """CoinGecko-ID bzw. Symbol → DefiLlama-Protokoll (Dachprotokoll bevorzugt) oder Chain.

    Zwei Abfragen (die Protokoll-Liste ist ~7 MB); der Server cacht das Ergebnis einen Tag."""
    lite = fetch_json("https://api.llama.fi/lite/protocols2?b=2")
    chains = fetch_json("https://api.llama.fi/v2/chains")
    by_gecko: Dict[str, Dict[str, str]] = {}
    by_symbol: Dict[str, Dict[str, str]] = {}

    def add(gecko: Optional[str], symbol: Optional[str], entry: Dict[str, str]) -> None:
        if gecko:
            by_gecko.setdefault(gecko, entry)
        if symbol and symbol != "-":
            # Symbole sind mehrdeutig: Chains haben hier Vorrang (BTC, ETH, SOL ohne CoinGecko-ID)
            if entry["kind"] == "chain" and by_symbol.get(symbol.upper(), {}).get("kind") != "chain":
                by_symbol[symbol.upper()] = entry
            else:
                by_symbol.setdefault(symbol.upper(), entry)

    # Reihenfolge = Vorrang: Dachprotokolle (HYPE → Hyperliquid-Börse statt -Chain), dann Chains
    # (ETH → Ethereum-Chain statt „Ethereum Foundation“), dann einzelne Protokolle
    for p in lite.get("parentProtocols", []):
        add(p.get("gecko_id"), p.get("symbol"),
            {"kind": "protocol", "slug": p["id"].split("#", 1)[-1], "name": p["name"]})
    for c in chains:
        add(c.get("gecko_id"), c.get("tokenSymbol"), {"kind": "chain", "slug": _llama_slug(c["name"]), "name": c["name"]})
    for p in lite.get("protocols", []):
        if p.get("parentProtocol"):
            continue
        add(p.get("geckoId"), p.get("symbol"), {"kind": "protocol", "slug": _llama_slug(p["name"]), "name": p["name"]})
    return {"gecko": by_gecko, "symbol": by_symbol}


def get_fees(entry: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Gebühren, Einnahmen und Einnahmen für Token-Halter eines Protokolls bzw. einer Chain.

    Je Art: Summen für 24 h, 7 T, 30 T (dazu die 30 T davor), 1 J und gesamt sowie Wochensummen
    der letzten 52 Wochen für das Chart. None, wenn DefiLlama keine Gebührendaten hat
    (antwortet dann mit HTTP 400). Schlägt eine Abfrage aus anderem Grund fehl, steht im Ergebnis
    "incomplete": True; gibt es dann gar keine Daten, wird ein ValueError ausgelöst – damit der
    Server einen kurzen Ausfall nicht als „keine Gebühren“ cacht."""
    out: Dict[str, Any] = {"name": entry["name"], "kind": entry["kind"], "slug": entry["slug"]}
    found = failed = False
    for key, data_type in FEE_TYPES.items():
        d = None
        try:
            d = fetch_json(f"https://api.llama.fi/summary/fees/{urllib.parse.quote(entry['slug'])}"
                           f"?dataType={data_type}&excludeTotalDataChartBreakdown=true")
        except urllib.error.HTTPError as exc:
            if exc.code not in (400, 404):   # 400/404 = keine Daten dieser Art
                failed = True
                print(f"  [warn] get_fees {entry['slug']} {data_type}: {exc}", file=sys.stderr)
        except NETWORK_ERRORS + (ValueError,) as exc:
            failed = True
            print(f"  [warn] get_fees {entry['slug']} {data_type}: {exc}", file=sys.stderr)
        if not isinstance(d, dict):
            d = None
        chart = (d or {}).get("totalDataChart") or []
        if not chart or not any(v for _, v in chart[-FEE_WEEKS * 7:]):   # leer oder im letzten Jahr nur Nullen
            out[key] = None
            continue
        found = True
        day = 86400
        last = chart[-1][0]
        def window(start_days: int, end_days: int) -> float:
            return sum(v for t, v in chart if last - start_days * day < t <= last - end_days * day)
        weeks: Dict[int, float] = {}
        days_in: Dict[int, int] = {}
        for t, v in chart:
            if t > last - (FEE_WEEKS + 1) * 7 * day:
                w = (t - 4 * day) // (7 * day) * (7 * day) + 4 * day   # Wochenbeginn Montag (1.1.1970 war Donnerstag)
                weeks[w] = weeks.get(w, 0.0) + v
                days_in[w] = days_in.get(w, 0) + 1
        # angebrochene Wochen (am Anfang und die laufende) weglassen, sonst sackt die Linie ab
        weeks = {w: v for w, v in weeks.items() if days_in[w] == 7}
        out[key] = {
            "total24h": d.get("total24h"), "total7d": d.get("total7d"),
            "total30d": window(30, 0), "prev30d": window(60, 30),
            "total1y": d.get("total1y"), "totalAllTime": d.get("totalAllTime"),
            "days": len(chart),
            "weekly": [{"time": w, "value": v} for w, v in sorted(weeks.items())],
        }
    if failed:
        if not found:
            raise ValueError(f"DefiLlama-Gebühren für {entry['slug']} nicht erreichbar")
        out["incomplete"] = True
    return out if found else None


# Deutschsprachige Krypto-Nachrichten per RSS (ohne Key). Schlagzeilen werden nur verlinkt, nicht bewertet.
NEWS_FEEDS = [
    ("BTC-ECHO", "https://www.btc-echo.de/feed/"),
    ("BeInCrypto", "https://de.beincrypto.com/feed/"),
    ("Krypto Magazin", "https://www.krypto-magazin.de/feed/"),
    ("Blocktrainer", "https://www.blocktrainer.de/feed/"),
    ("CVJ", "https://cvj.ch/feed/"),
    ("Bitcoin Kurier", "https://bitcoin-kurier.de/feed/"),
]
NEWS_FEEDS_EN = [   # englische Oberfläche
    ("Cointelegraph", "https://cointelegraph.com/rss"),
    ("Decrypt", "https://decrypt.co/feed"),
    ("Bitcoin Magazine", "https://bitcoinmagazine.com/feed"),
    ("CryptoSlate", "https://cryptoslate.com/feed/"),
    ("The Block", "https://www.theblock.co/rss.xml"),
    ("BeInCrypto", "https://beincrypto.com/feed/"),
    ("CryptoPotato", "https://cryptopotato.com/feed/"),
]
NEWS_MAX_BYTES = 3_000_000   # mehr wird von einem Feed nicht gelesen
NEWS_PER_COIN = 5
NEWS_TEASER_LEN = 220
NEWS_MIN_TERM = 3            # kürzere Symbole/Namen (z. B. „OP“) treffen zu viel Unpassendes


def _plain(text: Optional[str]) -> str:
    """HTML aus Feed-Texten entfernen (Tags, Entities, überzählige Leerzeichen)."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def get_news_feed(source: str, url: str) -> List[Dict[str, Any]]:
    """Einträge eines RSS-Feeds: Titel, Link, Zeit (Sekunden), Anreißer, Quelle."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read(NEWS_MAX_BYTES)
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError(f"Feed {source} nicht lesbar: {exc}") from exc
    items = []
    for item in root.iter("item"):
        title, link = _plain(item.findtext("title")), (item.findtext("link") or "").strip()
        if not title or not link.startswith(("https://", "http://")):
            continue
        try:
            when = int(parsedate_to_datetime(item.findtext("pubDate") or "").timestamp())
        except (TypeError, ValueError):
            continue
        teaser = _plain(item.findtext("description"))
        if len(teaser) > NEWS_TEASER_LEN:
            teaser = teaser[:NEWS_TEASER_LEN].rsplit(" ", 1)[0] + " …"
        items.append({"title": title, "link": link, "time": when, "teaser": teaser, "source": source})
    return items


def get_news(lang: str = "de") -> List[Dict[str, Any]]:
    """Alle Feeds der Sprache parallel, neueste zuerst; ValueError, wenn kein Feed erreichbar ist."""
    feeds = NEWS_FEEDS_EN if lang == "en" else NEWS_FEEDS
    with ThreadPoolExecutor(max_workers=len(feeds)) as pool:
        results = list(pool.map(lambda f: safe(get_news_feed, *f), feeds))
    if not any(results):
        raise ValueError("kein News-Feed erreichbar")
    return sorted((i for r in results if r for i in r), key=lambda i: -i["time"])


def match_news(items: List[Dict[str, Any]], symbol: str, name: Optional[str]) -> Dict[str, Any]:
    """Meldungen zu einem Coin: zuerst Treffer im Titel, dann im Anreißer, je neueste zuerst.

    Gesucht wird das Symbol (Großschreibung, ganzes Wort) und der Name (ganzes Wort). Ohne Treffer
    kommen die neuesten allgemeinen Meldungen, gekennzeichnet mit general = True."""
    terms = []
    if len(symbol) >= NEWS_MIN_TERM:
        terms.append(re.compile(rf"(?<![A-Za-z0-9]){re.escape(symbol)}(?![A-Za-z0-9])"))
    if name and len(name) >= NEWS_MIN_TERM:
        terms.append(re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE))
    hit = lambda text: any(t.search(text) for t in terms)
    in_title = [i for i in items if hit(i["title"])]
    in_teaser = [i for i in items if not hit(i["title"]) and hit(i["teaser"])]
    found = (in_title + in_teaser)[:NEWS_PER_COIN]
    return {"general": not found, "items": found or items[:NEWS_PER_COIN]}


def get_stablecoins() -> Dict[str, Any]:
    chart = fetch_json("https://stablecoins.llama.fi/stablecoincharts/all")
    now = chart[-1]["totalCirculatingUSD"]["peggedUSD"]
    month_ago = chart[-31]["totalCirculatingUSD"]["peggedUSD"]
    return {"mcap": now, "change_30d": (now - month_ago) / month_ago}


def get_btc_dominance() -> Dict[str, Any]:
    data = fetch_json("https://api.coingecko.com/api/v3/global")["data"]
    return {"btc": data["market_cap_percentage"]["btc"]}


def get_trending() -> List[Dict[str, Any]]:
    """Meistgesuchte Coins der letzten 24 h, nach Rang sortiert (CoinGecko liefert max. 15)."""
    coins = fetch_json("https://api.coingecko.com/api/v3/search/trending")["coins"]
    result = []
    for rank, c in enumerate(coins, start=1):
        item = c["item"]
        data = item.get("data") or {}
        change = data.get("price_change_percentage_24h", {}).get("usd")
        mcap = data.get("market_cap")  # als Text, z. B. "$16,819,539,659"
        result.append({
            "id": item["id"],
            "symbol": item["symbol"].upper(),
            "name": item["name"],
            "rank": rank,
            "change_24h": change / 100 if change is not None else None,
            "market_cap": float(mcap.lstrip("$").replace(",", "")) if mcap else None,
            "market_cap_rank": item.get("market_cap_rank"),
        })
    return result


# --- Auswertung -------------------------------------------------------------

def scale(value: float, anchors: List[Tuple[float, int]]) -> int:
    """Rechnet einen Messwert anhand der Ankerpunkte in -100 … +100 Punkte um."""
    if value <= anchors[0][0]:
        return anchors[0][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if value <= x1:
            return round(y0 + (value - x0) / (x1 - x0) * (y1 - y0))
    return anchors[-1][1]


LANGS = ("de", "en")   # Sprachen der Oberfläche; Texte vom Server je Sprache (Parameter lang, Standard deutsch)


def _l(lang: str):
    """Wählt den Text zur Sprache: L("deutsch", "english")."""
    return (lambda de, en: en) if lang == "en" else (lambda de, en: de)


def mvrv_word(v: float, L: Any) -> str:
    # Einheitliche MVRV-Stufen (auch in coin-onchain.js, MVRV_ZONES): 1 = Einstand, 3,5 = frühere BTC-Hochs
    if v < 1:
        return L("Halter im Schnitt im Verlust", "holders at a loss on average")
    if v < 1.6:
        return L("Halter leicht im Gewinn, nahe am Einstand", "holders slightly in profit, near cost basis")
    if v < 2.5:
        return L("Halter moderat im Gewinn", "holders moderately in profit")
    if v < 3.5:
        return L("Halter deutlich im Gewinn", "holders well in profit")
    return L("Halter weit im Gewinn", "holders deep in profit")


def rsi_zone(v: float) -> str:
    # Einheitlich für Score, Lagebild und Chart: am angezeigten (gerundeten) Wert, überverkauft erst unter 30,
    # überkauft erst über 70; bis 40 bzw. ab 60 „nahe am …“
    r = round(v)
    low, high = RSI_SCALE[0][0], RSI_SCALE[-1][0]
    return "sold" if r < low else "bought" if r > high else "weak" if r <= 40 else "strong" if r >= 60 else "neutral"


def rsi_word(v: float, L: Any, prefix: str = "") -> str:
    z = rsi_zone(v)
    if z == "sold":
        return prefix + L("überverkauft", "oversold")
    if z == "bought":
        return prefix + L("überkauft", "overbought")
    if z == "weak":
        return L("schwach, nahe am überverkauften Bereich", "weak, close to oversold")
    if z == "strong":
        return L("stark, nahe am überkauften Bereich", "strong, close to overbought")
    return "neutral"


def score(d: Dict[str, Any], lang: str = "de") -> List[Dict[str, Any]]:
    """Liefert Einzelsignale von -100 (Angst) über 0 (neutral) bis +100 (überhitzt/Gier).

    name ist der Schlüssel des Signals (deutsch, auch für SIGNAL_WEIGHTS und die Tooltips);
    die Anzeige übersetzt ihn im Browser. reason ist in der gewünschten Sprache."""
    L = _l(lang)
    signals = []
    # Zahlen in der Schreibweise der Sprache (deutsch: Komma, „ %“), wie im Lagebild
    pct = lambda v, d=1, sign=True: _pct_de(v, d, sign, lang)
    num = lambda v, d: f"{v:.{d}f}" if lang == "en" else f"{v:.{d}f}".replace(".", ",")
    sgn = lambda v: "+" if v > 0 else "−" if v < 0 else ""

    def add(name: str, points: int, reason: str, hot: str, cold: str, neutral: str) -> None:
        word = hot if points >= SIGNAL_STRONG else cold if points <= -SIGNAL_STRONG else neutral
        signals.append({"name": name, "points": points, "reason": f"{reason}, {word}",
                        "weight": SIGNAL_WEIGHTS.get(name, 1.0)})

    fng = d.get("fear_greed")
    if fng:
        v = fng["value"]
        add("Fear & Greed", scale(v, FNG_SCALE), f"{v}", L("Gier", "greed"), L("Angst", "fear"), "neutral")

    fr = d.get("funding")
    if fr:
        r = fr["avg_7d"]
        takt = fr.get("interval_h", FUNDING_BASE_HOURS)
        note = L(f" (Takt {takt} h, umgerechnet)", f" (every {takt} h, converted)") if takt != FUNDING_BASE_HOURS else ""
        add("Funding Rate", scale(r, FUNDING_SCALE), f"{L('Ø', 'avg')} {pct(r, 4)} / 8 h{note}",
            L("Longs zahlen viel", "longs pay a lot"), L("Shorts dominieren", "shorts dominate"), "normal")

    oi = d.get("open_interest")
    if oi:
        c = oi["change_7d"]
        add("Open Interest", scale(c, OI_SCALE), L(f"{pct(c)} in 7 Tagen", f"{pct(c)} in 7 days"),
            L("viel neuer Hebel", "lots of new leverage"), L("starker Hebelabbau", "strong deleveraging"), "normal")

    ma = d.get("price_ma200")
    if ma:
        dist = ma["distance"]
        add("Preis vs. MA200", scale(dist, MA200_SCALE), pct(dist),
            L("stark gestreckt", "strongly extended"), L("deutlich unter dem Trend", "well below trend"),
            L("im Trendbereich", "near trend"))

    rs = d.get("rsi")
    if rs:
        v = rs["value"]
        w = rsi_word(v, L)
        add("RSI (14)", scale(v, RSI_SCALE), f"{v:.0f}", w, w, w)

    dv = d.get("dvol")
    if dv:
        v, z = dv["value"], dv["z"]
        add("Optionsvola (DVOL)", scale(z, DVOL_SCALE), f"{num(v, 1)} ({sgn(z)}{num(abs(z), 1)}σ)",
            L("ungewöhnlich ruhig, Sorglosigkeit", "unusually calm, complacency"),
            L("ungewöhnlich hoch, Absicherung gefragt", "unusually high, hedging in demand"), "normal")

    mo = d.get("momentum")
    if mo:
        c7, c30, turnover = mo["change_7d"], mo["change_30d"], mo["turnover"]
        up, down, mid = L("starker Anstieg", "strong rise"), L("starker Rückgang", "strong decline"), L("moderat", "moderate")
        if c7 is not None:
            add("Momentum 7T", scale(c7, MOMENTUM_7D_SCALE), pct(c7), up, down, mid)
        if c30 is not None:
            add("Momentum 30T", scale(c30, MOMENTUM_30D_SCALE), pct(c30), up, down, mid)
        if turnover is not None and c7 is not None:
            pts = scale(turnover, TURNOVER_SCALE) * (1 if c7 >= 0 else -1)
            add("Umschlag", pts, L(f"{pct(turnover, 0, False)} der Marktkap. pro Tag", f"{pct(turnover, 0, False)} of market cap per day"),
                L("Spekulationsrausch", "speculative frenzy"), L("Panikverkäufe", "panic selling"), "normal")
        if not rs and mo["rsi_hourly"] is not None:
            v = mo["rsi_hourly"]
            w = rsi_word(v, L, L("kurzfristig ", "short term "))
            add("RSI (14 Std.)", scale(v, RSI_SCALE), f"{v:.0f}", w, w, w)

    mv = d.get("mvrv")
    if mv:
        v = mv["value"]
        w = mvrv_word(v, L)
        add("MVRV", scale(v, MVRV_SCALE), num(v, 2), w, w, w)

    ls = d.get("long_short")
    if ls:
        r = ls["ratio"]
        add("Long/Short Ratio", scale(r, LS_SCALE), num(r, 2),
            L("Masse stark long", "crowd heavily long"), L("Masse short", "crowd short"), L("ausgeglichen", "balanced"))

    return signals


def total_score(signals: List[Dict[str, Any]]) -> int:
    """Gesamtscore = gewichteter Durchschnitt der Einzelsignale (-100 … +100), Gewichte SIGNAL_WEIGHTS."""
    if not signals:
        return 0
    w = [SIGNAL_WEIGHTS.get(s["name"], 1.0) for s in signals]
    return round(sum(s["points"] * x for s, x in zip(signals, w)) / sum(w))


def verdict(total: int, count: int, lang: str = "de") -> str:
    L = _l(lang)
    if count == 0:
        return L("Keine Daten verfügbar.", "No data available.")
    if total >= VERDICT_HOT:
        return L("ÜBERHITZT: Vorsicht, keine neuen Hebelpositionen, Gewinne sichern erwägen.",
                 "OVERHEATED: be careful, no new leveraged positions, consider taking profits.")
    if total >= VERDICT_WARM:
        return L("Leicht gierig: Markt läuft heiß, nicht der FOMO folgen.",
                 "Slightly greedy: the market is running hot, don't chase FOMO.")
    if total <= -VERDICT_HOT:
        return L("EXTREME ANGST: allein kein verlässliches Kaufsignal – Trendwende abwarten.",
                 "EXTREME FEAR: not a reliable buy signal on its own – wait for the trend to turn.")
    if total <= -VERDICT_WARM:
        return L("Leicht ängstlich: kein Verkaufssignal, Einstieg am besten mit Trendbestätigung.",
                 "Slightly fearful: no sell signal, best to enter with trend confirmation.")
    return L("Neutral: Keine Extreme erkennbar.", "Neutral: no extremes visible.")


def _pct_de(v: float, digits: int = 1, sign: bool = True, lang: str = "de") -> str:
    """Anteil als Prozent in der Schreibweise der Sprache, z. B. −12,3 % bzw. −12.3%."""
    r = round(v * 100, digits)
    pre = ("+" if r > 0 else "−" if r < 0 else "") if sign else ""
    if lang == "en":
        return f"{pre}{abs(r):.{digits}f}%"
    return f"{pre}{abs(r):.{digits}f} %".replace(".", ",")


# Lagebild: Stimmung (Score) × Trend (Kurs zum MA200). Titel und Einordnung je Feld sind
# Erfahrungswerte, keine Regeln.
SITUATIONS = {
    ("fear", "up"): ("Rücksetzer im Aufwärtstrend",
                     "Ängstliche Stimmung bei intaktem Aufwärtstrend. Solche Phasen waren historisch oft günstig."),
    ("fear", "down"): ("Angst im Abwärtstrend",
                       "Mögliche Bodenbildung, aber der Trend zeigt abwärts („fallendes Messer“)."),
    ("greed", "up"): ("Heiß gelaufen",
                      "Gierige Stimmung im Aufwärtstrend. Der Trend trägt, Rücksetzer werden aber wahrscheinlicher."),
    ("greed", "down"): ("Erholung im Abwärtstrend",
                        "Gierige Stimmung, obwohl der Trend abwärts zeigt. Solche Erholungen sind oft kurzlebig."),
    ("neutral", "up"): ("Ruhiger Aufwärtstrend",
                        "Trend aufwärts, Stimmung ohne Extreme: kein antizyklisches Signal."),
    ("neutral", "down"): ("Abwärtstrend ohne Extreme",
                          "Trend abwärts, Stimmung weder panisch noch gierig: kein antizyklisches Signal."),
    ("fear", None): ("Ängstliche Stimmung", "Der Trend lässt sich nicht bestimmen (zu wenig Kursdaten)."),
    ("greed", None): ("Gierige Stimmung", "Der Trend lässt sich nicht bestimmen (zu wenig Kursdaten)."),
    ("neutral", None): ("Stimmung ohne Extreme", "Der Trend lässt sich nicht bestimmen (zu wenig Kursdaten)."),
}


# Handlungsempfehlung je Lagebild-Feld: (Aktion, Ton, Begründung). Ton steuert die Farbe auf der
# Detailseite: buy = grün, sell = rot, hold/wait = neutral. Bei extremer Stimmung (ab ±VERDICT_HOT)
# gelten die Varianten in ACTIONS_EXTREME. Erfahrungswerte, keine Regeln.
ACTIONS = {
    ("fear", "up"): ("Nachkaufen", "buy",
                     "Rücksetzer im intakten Aufwärtstrend schrittweise zukaufen; "
                     "Absicherung knapp unter dem MA200."),
    ("fear", "down"): ("Abwarten", "wait",
                       "Nicht ins fallende Messer greifen: Einstieg erst, wenn der Kurs den MA200 "
                       "zurückerobert oder die Angst nachlässt."),
    ("greed", "up"): ("Halten, nicht nachkaufen", "hold",
                      "Der Trend trägt, aber Einstiege sind jetzt teuer. Bestehende Positionen halten "
                      "und Absicherung nachziehen."),
    ("greed", "down"): ("Reduzieren", "sell",
                        "Die Erholung gegen den Abwärtstrend zum Abbau von Positionen nutzen."),
    ("neutral", "up"): ("Halten", "hold",
                        "Trend intakt, kein Extrem: Positionen laufen lassen, "
                        "Nachkäufe am besten bei Rücksetzern."),
    ("neutral", "down"): ("Nicht kaufen", "sell",
                          "Abwärtstrend ohne Übertreibung: kein Einstieg, "
                          "bestehende Positionen eher verkleinern."),
    ("fear", None): ("Beobachten", "wait",
                     "Angst allein war bisher kein verlässliches Kaufsignal; ohne Trend fehlt die Bestätigung."),
    ("greed", None): ("Nicht nachkaufen", "hold",
                      "Gier spricht gegen neue Käufe; ohne Trend fehlt die Bestätigung für mehr."),
    ("neutral", None): ("Abwarten", "wait",
                        "Weder Stimmung noch Trend geben ein klares Signal."),
}
ACTIONS_EXTREME = {
    ("fear", "down"): ("Abwarten", "wait",
                       "Extreme Angst war bisher kein verlässlicher Boden – der Kurs fiel oft weiter. "
                       "Einstieg erst, wenn der Trend dreht (Kurs zurück über dem MA200)."),
    ("greed", "up"): ("Gewinne sichern", "sell",
                      "Überhitzt: einen Teil der Gewinne mitnehmen, der Rest läuft mit enger Absicherung."),
    ("greed", "down"): ("Verkaufen", "sell",
                        "Überhitzte Erholung im Abwärtstrend: Positionen abbauen."),
}


SITUATIONS_EN = {
    ("fear", "up"): ("Pullback in an uptrend",
                     "Fearful mood while the uptrend is intact. Phases like this were often favourable historically."),
    ("fear", "down"): ("Fear in a downtrend",
                       "Possible bottoming, but the trend points down (“falling knife”)."),
    ("greed", "up"): ("Running hot",
                      "Greedy mood in an uptrend. The trend holds, but pullbacks become more likely."),
    ("greed", "down"): ("Bounce in a downtrend",
                        "Greedy mood although the trend points down. Bounces like this are often short-lived."),
    ("neutral", "up"): ("Calm uptrend",
                        "Trend up, mood without extremes: no contrarian signal."),
    ("neutral", "down"): ("Downtrend without extremes",
                          "Trend down, mood neither panicked nor greedy: no contrarian signal."),
    ("fear", None): ("Fearful mood", "The trend cannot be determined (not enough price data)."),
    ("greed", None): ("Greedy mood", "The trend cannot be determined (not enough price data)."),
    ("neutral", None): ("Mood without extremes", "The trend cannot be determined (not enough price data)."),
}
ACTIONS_EN = {
    ("fear", "up"): ("Buy the dip", "buy",
                     "Add gradually on pullbacks in an intact uptrend; place protection just below the MA200."),
    ("fear", "down"): ("Wait", "wait",
                       "Don't catch the falling knife: enter only once the price reclaims the MA200 or fear eases."),
    ("greed", "up"): ("Hold, don't add", "hold",
                      "The trend holds, but entries are expensive now. Keep existing positions and trail your stops."),
    ("greed", "down"): ("Reduce", "sell",
                        "Use the bounce against the downtrend to reduce positions."),
    ("neutral", "up"): ("Hold", "hold",
                        "Trend intact, no extreme: let positions run, add preferably on pullbacks."),
    ("neutral", "down"): ("Don't buy", "sell",
                          "Downtrend without excess: no entry, rather trim existing positions."),
    ("fear", None): ("Watch", "wait",
                     "Fear alone has not been a reliable buy signal so far; without a trend there is no confirmation."),
    ("greed", None): ("Don't add", "hold",
                      "Greed argues against new buys; without a trend there is no confirmation for more."),
    ("neutral", None): ("Wait", "wait",
                        "Neither mood nor trend gives a clear signal."),
}
ACTIONS_EXTREME_EN = {
    ("fear", "down"): ("Wait", "wait",
                       "Extreme fear has not been a reliable bottom so far – the price often kept falling. "
                       "Enter only once the trend turns (price back above the MA200)."),
    ("greed", "up"): ("Take profits", "sell",
                      "Overheated: take part of the profits, let the rest run with tight protection."),
    ("greed", "down"): ("Sell", "sell",
                        "Overheated bounce in a downtrend: reduce positions."),
}


def situation(d: Dict[str, Any], signals: List[Dict[str, Any]], total: int, lang: str = "de") -> Optional[Dict[str, Any]]:
    """Lagebild für den Kopf der Detailseite: Einordnung aus Stimmung und Trend, ein Satz zur Lage,
    eine Handlungsempfehlung (ACTIONS) und was für bzw. gegen steigende Kurse spricht.

    Stimmung: Score ab ±VERDICT_WARM. Trend: Kurs über/unter dem MA200, ohne MA200 die Kursänderung
    über 30 Tage. Ausgeprägte Signale (ab ±SIGNAL_STRONG) werden antizyklisch gelesen: Angst und
    abgebauter Hebel sprechen eher für steigende Kurse, Gier und viel Hebel dagegen."""
    if not signals:
        return None
    L = _l(lang)
    en = lang == "en"
    pct = lambda v, digits=1, sign=True: _pct_de(v, digits, sign, lang)   # noqa: E731
    pts = {s["name"]: s["points"] for s in signals}
    pro: List[str] = []
    contra: List[str] = []
    words: List[str] = []

    def strong(name: str, cold: Tuple[str, str], hot: Tuple[str, str]) -> None:
        """cold/hot = (Satz für die Liste, Stichwort für den Lage-Satz) bei ≤ −50 bzw. ≥ +50 Punkten."""
        p = pts.get(name, 0)
        if p <= -SIGNAL_STRONG and cold[0]:
            pro.append(cold[0])
            words.append(cold[1])
        elif p >= SIGNAL_STRONG and hot[0]:
            contra.append(hot[0])
            words.append(hot[1])

    trend = basis = trend_text = None
    ma = d.get("price_ma200")
    if ma:
        dist = ma["distance"]
        up = dist >= 0
        trend, basis = ("up" if up else "down"), "MA200"
        if en:
            where = f"{pct(abs(dist), sign=False)} {'above' if up else 'below'} the MA200"
            trend_text = f"{'Uptrend' if up else 'Downtrend'} ({where})"
            (pro if up else contra).append(f"Price {where}: long-term {'uptrend' if up else 'downtrend'}")
        else:
            where = f"{pct(abs(dist), sign=False)} {'über' if up else 'unter'} dem MA200"
            trend_text = f"{'Aufwärtstrend' if up else 'Abwärtstrend'} ({where})"
            (pro if up else contra).append(f"Kurs {where}: langfristiger {'Aufwärtstrend' if up else 'Abwärtstrend'}")
    else:
        c30 = ((d.get("market_stats") or {}).get("change") or {}).get("30d")
        if c30 is None:
            c30 = (d.get("momentum") or {}).get("change_30d")
        if c30:
            trend, basis = ("up" if c30 > 0 else "down"), L("30 Tage", "30 days")
            if en:
                trend_text = f"{'Upward' if c30 > 0 else 'Downward'} move ({pct(c30)} in 30 days)"
                (pro if c30 > 0 else contra).append(f"Price {pct(c30)} in 30 days (no MA200 available, trend only rough)")
            else:
                trend_text = f"{'Aufwärts' if c30 > 0 else 'Abwärts'}bewegung ({pct(c30)} in 30 Tagen)"
                (pro if c30 > 0 else contra).append(f"Kurs {pct(c30)} in 30 Tagen (kein MA200 verfügbar, Trend nur grob)")

    def num(v: float, digits: int) -> str:
        s = f"{v:.{digits}f}"
        return s if en else s.replace(".", ",")

    fng, rs, fr, oi = d.get("fear_greed"), d.get("rsi"), d.get("funding"), d.get("open_interest")
    ls, dv, mo = d.get("long_short"), d.get("dvol"), d.get("momentum") or {}
    if fng:
        strong("Fear & Greed",
               (L(f"Fear & Greed {fng['value']}: Angst am Markt (allein bisher ein schwaches Signal)",
                  f"Fear & Greed {fng['value']}: fear in the market (a weak signal on its own so far)"), ""),
               (L(f"Fear & Greed {fng['value']}: Gier am Markt (allein bisher ein schwaches Signal)",
                  f"Fear & Greed {fng['value']}: greed in the market (a weak signal on its own so far)"), ""))
    if ma:
        strong("Preis vs. MA200", ("", ""),
               (L("Kurs weit über dem MA200: stark gestreckt, anfällig für Rücksetzer",
                  "Price far above the MA200: strongly extended, prone to pullbacks"),
                L("stark gestreckt", "strongly extended")))
    if rs:
        r0 = num(rs["value"], 0)
        zone = rsi_zone(rs["value"])
        sold = L("überverkauft", "oversold") if zone == "sold" else L("schwach, nahe am überverkauften Bereich", "weak, close to oversold")
        bought = L("überkauft", "overbought") if zone == "bought" else L("stark, nahe am überkauften Bereich", "strong, close to overbought")
        strong("RSI (14)",
               (L(f"RSI {r0}: {sold}, Gegenbewegungen sind häufig", f"RSI {r0}: {sold}, rebounds are common"),
                L("überverkauft", "oversold") if zone == "sold" else L("RSI schwach", "RSI weak")),
               (L(f"RSI {r0}: {bought}, Rücksetzer sind häufig", f"RSI {r0}: {bought}, pullbacks are common"),
                L("überkauft", "overbought") if zone == "bought" else L("RSI stark", "RSI strong")))
    if mo.get("rsi_hourly") is not None:
        rh = num(mo["rsi_hourly"], 0)
        strong("RSI (14 Std.)",
               (L(f"RSI (Stunden) {rh}: kurzfristig überverkauft", f"RSI (hourly) {rh}: oversold short term"),
                L("kurzfristig überverkauft", "oversold short term")),
               (L(f"RSI (Stunden) {rh}: kurzfristig überkauft", f"RSI (hourly) {rh}: overbought short term"),
                L("kurzfristig überkauft", "overbought short term")))
    mvd = d.get("mvrv")
    if mvd:
        mvs = num(mvd["value"], 2)
        v = mvd["value"]
        strong("MVRV",
               (L(f"MVRV {mvs}: Halter im Schnitt im Verlust – bei Bitcoin lagen frühere Tiefs hier",
                  f"MVRV {mvs}: holders at a loss on average – earlier Bitcoin lows were here"),
                L("Halter im Verlust", "holders at a loss"))
               if v < 1 else (L(f"MVRV {mvs}: Halter leicht im Gewinn, Kurs nahe am durchschnittlichen Einstand – "
                                "historisch eine eher günstige Zone",
                                f"MVRV {mvs}: holders slightly in profit, price near their average cost basis – historically a rather cheap zone"),
                              L("nahe am Einstand", "near cost basis")),
               (L(f"MVRV {mvs}: Halter weit im Gewinn – bei Bitcoin lagen frühere Hochs hier",
                  f"MVRV {mvs}: holders deep in profit – earlier Bitcoin tops were here"),
                L("Halter weit im Gewinn", "holders deep in profit"))
               if v >= 3.5 else (L(f"MVRV {mvs}: Halter deutlich im Gewinn, Gewinnmitnahmen werden wahrscheinlicher",
                                 f"MVRV {mvs}: holders well in profit, profit taking becomes more likely"),
                               L("Halter deutlich im Gewinn", "holders well in profit")))
    if fr:
        rate = L(f"Funding {pct(fr['avg_7d'], 4)} pro 8 h", f"Funding {pct(fr['avg_7d'], 4)} per 8 h")
        strong("Funding Rate",
               (L(f"{rate}: Shorts dominieren, ein Short-Squeeze ist möglich", f"{rate}: shorts dominate, a short squeeze is possible"),
                L("Shorts dominieren", "shorts dominate")),
               (L(f"{rate}: Longs zahlen viel, der Markt ist gehebelt long", f"{rate}: longs pay a lot, the market is leveraged long"),
                L("viel Long-Hebel", "heavy long leverage")))
    if oi:
        chg = L(f"Open Interest {pct(oi['change_7d'])} in 7 Tagen", f"Open interest {pct(oi['change_7d'])} in 7 days")
        strong("Open Interest",
               (L(f"{chg}: Hebel wurde abgebaut, weniger Liquidationsdruck", f"{chg}: leverage was reduced, less liquidation pressure"),
                L("Hebel wird abgebaut", "deleveraging")),
               (L(f"{chg}: viel neuer Hebel, Risiko von Liquidationswellen", f"{chg}: lots of new leverage, risk of liquidation cascades"),
                L("viel neuer Hebel", "lots of new leverage")))
    if ls:
        ratio = f"Long/Short {num(ls['ratio'], 2)}"
        strong("Long/Short Ratio",
               (L(f"{ratio}: ungewöhnlich viele Konten short (Kontraindikator)", f"{ratio}: unusually many accounts short (contrarian indicator)"),
                L("Masse short", "crowd short")),
               (L(f"{ratio}: die Masse ist stark long (Kontraindikator)", f"{ratio}: the crowd is heavily long (contrarian indicator)"),
                L("Masse stark long", "crowd heavily long")))
    if dv:
        z = f"{'+' if dv['z'] >= 0 else '−'}{num(abs(dv['z']), 1)}σ"
        strong("Optionsvola (DVOL)",
               (L(f"Optionsvola ungewöhnlich hoch ({z}): Absicherung gefragt, viel Angst ist eingepreist",
                  f"Options volatility unusually high ({z}): hedging in demand, a lot of fear is priced in"),
                L("hohe Optionsvola", "high options volatility")),
               (L(f"Optionsvola ungewöhnlich niedrig ({z}): Sorglosigkeit", f"Options volatility unusually low ({z}): complacency"),
                L("niedrige Optionsvola", "low options volatility")))
    if mo.get("change_7d") is not None:
        c7 = pct(mo["change_7d"])
        strong("Momentum 7T",
               (L(f"{c7} in 7 Tagen: starker Ausverkauf, eine Gegenbewegung ist möglich", f"{c7} in 7 days: heavy sell-off, a rebound is possible"),
                L("starker Ausverkauf", "heavy sell-off")),
               (L(f"{c7} in 7 Tagen: steiler Anstieg, anfällig für Rücksetzer", f"{c7} in 7 days: steep rise, prone to pullbacks"),
                L("steiler Anstieg", "steep rise")))
    if mo.get("turnover") is not None:
        strong("Umschlag",
               (L("Sehr hoher Umschlag im Rückgang: Panikverkäufe, oft nahe an Zwischentiefs",
                  "Very high turnover in a decline: panic selling, often near interim lows"), L("Panikverkäufe", "panic selling")),
               (L("Sehr hoher Umschlag im Anstieg: Spekulationsrausch", "Very high turnover in a rise: speculative frenzy"),
                L("Spekulationsrausch", "speculative frenzy")))

    mood = "greed" if total >= VERDICT_WARM else "fear" if total <= -VERDICT_WARM else "neutral"
    mood_text = (L("Stimmung überhitzt", "mood overheated") if total >= VERDICT_HOT
                 else L("Stimmung leicht gierig", "mood slightly greedy") if total >= VERDICT_WARM
                 else L("Stimmung extrem ängstlich", "mood extremely fearful") if total <= -VERDICT_HOT
                 else L("Stimmung leicht ängstlich", "mood slightly fearful") if total <= -VERDICT_WARM
                 else L("Stimmung neutral", "mood neutral"))
    words = [w for w in words if w]
    summary = ", ".join(x for x in (trend_text, mood_text) if x) + ("; " + ", ".join(words[:3]) if words else "") + "."
    summary = summary[0].upper() + summary[1:]
    title, text = (SITUATIONS_EN if en else SITUATIONS)[(mood, trend)]
    extreme = abs(total) >= VERDICT_HOT
    actions, actions_x = (ACTIONS_EN, ACTIONS_EXTREME_EN) if en else (ACTIONS, ACTIONS_EXTREME)
    action, tone, reason = (extreme and actions_x.get((mood, trend))) or actions[(mood, trend)]
    return {"mood": mood, "trend": trend, "trend_basis": basis, "title": title, "text": text,
            "summary": summary, "pro": pro, "contra": contra,
            "action": action, "action_tone": tone, "action_text": reason}


def fmt_usd(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:,.1f} Mrd."
    if v >= 1e6:
        return f"${v / 1e6:,.1f} Mio."
    return f"${v:,.2f}"


def print_report(symbol: str, d: Dict[str, Any], signals: List[Dict[str, Any]]) -> None:
    total = total_score(signals)
    line = "─" * 60
    print(f"\n{line}\n  Crypto-Sentiment {symbol}  ·  {datetime.now():%d.%m.%Y %H:%M}\n{line}")

    ma = d.get("price_ma200")
    if ma:
        trend = "AUFWÄRTS (über MA200)" if ma["distance"] >= 0 else "ABWÄRTS (unter MA200)"
        print(f"  Preis:          {fmt_usd(ma['price'])}   MA200: {fmt_usd(ma['ma200'])}")
        print(f"  Trend:          {trend}")
    mc = d.get("market_cap")
    if mc:
        rank = f" (Rang {mc['rank']})" if mc.get("rank") else ""
        print(f"  Marktkap.:      ${mc['usd']:,.0f}{rank}")
    rs = d.get("rsi")
    if rs:
        print(f"  RSI (14):       {rs['value']:.0f}")
    dv = d.get("dvol")
    if dv:
        print(f"  DVOL:           {dv['value']:.1f} (Ø {DVOL_WINDOW}T {dv['mean']:.1f}, {dv['z']:+.1f}σ)")

    fng = d.get("fear_greed")
    if fng:
        print(f"  Fear & Greed:   {fng['value']} ({fng['label']}), vor 7 Tagen {fng['week_ago']}")
    oi = d.get("open_interest")
    if oi:
        print(f"  Open Interest:  {fmt_usd(oi['usd'])} ({oi['change_7d']:+.1%} / 7T)")
    st = d.get("stablecoins")
    if st:
        print(f"  Stablecoins:    {fmt_usd(st['mcap'])} ({st['change_30d']:+.1%} / 30T)")
    dom = d.get("btc_dominance")
    if dom:
        print(f"  BTC-Dominanz:   {dom['btc']:.1f} %")
    tr = d.get("trending")
    if tr:
        print(f"  Trending:       {', '.join(t['symbol'] for t in tr)}")

    print(f"\n  Signale:")
    for s in signals:
        pts = s["points"]
        arrow = "▲" if pts >= SIGNAL_STRONG else "▼" if pts <= -SIGNAL_STRONG else "·"
        print(f"    {arrow} {pts:+4d}  {s['name']:<19} {s['reason']}" if pts else
              f"    {arrow} {0:4d}  {s['name']:<19} {s['reason']}")

    print(f"\n  Score: {total:+d} (Bereich -100 … +100, Durchschnitt aus {len(signals)} Signalen)")
    print(f"  → {verdict(total, len(signals))}")
    sit = situation(d, signals, total)
    if sit:
        print(f"\n  Lage: {sit['title']} – {sit['summary']}")
        print(f"  Empfehlung: {sit['action']} – {sit['action_text']}")
        for label, items in (("Für steigende Kurse spricht", sit["pro"]), ("Dagegen spricht", sit["contra"])):
            if items:
                print(f"  {label}:")
                for item in items:
                    print(f"    · {item}")
    print(f"{line}\n  Schwellenwerte sind Erfahrungswerte.\n")


LIQ_MODEL_HOURS = 500    # Binance liefert OI-Historie höchstens 500 Punkte (1 h ≈ 21 Tage)


def get_liquidation_inputs(symbol: str) -> Dict[str, Any]:
    """Futures-Kerzen und Open Interest je Stunde für das Liquidations-Modell der Detailseite.

    Beides von Binance Futures (Perpetual), damit Kurs und OI zum selben Markt gehören."""
    klines = futures_data(
        f"https://fapi.binance.com/fapi/v1/klines?symbol={fpair(symbol)}&interval=1h&limit={LIQ_MODEL_HOURS}", symbol
    )
    mult = futures_pair(symbol)[1]
    oi = futures_data(
        "https://fapi.binance.com/futures/data/openInterestHist"
        f"?symbol={fpair(symbol)}&period=1h&limit={LIQ_MODEL_HOURS}", symbol
    )
    return {
        "candles": [{"time": k[0] // 1000, "open": float(k[1]) / mult, "high": float(k[2]) / mult,
                     "low": float(k[3]) / mult, "close": float(k[4]) / mult} for k in klines],
        # Zeitstempel = Stand zu Beginn der Stunde → der Kerze zuordnen, die dort beginnt
        "open_interest": [{"time": h["timestamp"] // 1000, "usd": float(h["sumOpenInterestValue"])} for h in oi],
    }


def _as_of(series: List[Tuple[int, Any]], t: float, max_age: float) -> Optional[Any]:
    """Letzter Wert mit Zeit <= t, sofern nicht älter als max_age Sekunden."""
    i = bisect.bisect_right(series, (t, float("inf"))) - 1
    if i < 0 or t - series[i][0] > max_age:
        return None
    return series[i][1]


def get_dvol_history(symbol: str, days: int) -> List[Tuple[int, float]]:
    end = int(time.time() * 1000)
    start = end - (days + DVOL_WINDOW + 3) * 86_400_000
    data = fetch_json(
        "https://www.deribit.com/api/v2/public/get_volatility_index_data"
        f"?currency={symbol}&start_timestamp={start}&end_timestamp={end}&resolution=1D"
    )["result"]["data"]
    return [(int(c[0]) // 1000, float(c[4])) for c in data]


def get_score_history(symbol: str, days: int = SCORE_HISTORY_DAYS) -> Optional[Dict[str, Any]]:
    """Score je Tag, rückwirkend aus den Tageswerten berechnet, mit derselben Logik wie score().

    Jeder Tag nutzt die Daten, die bis zu seinem Ende (UTC) vorlagen; der heutige Tag die bis jetzt.
    Enthalten sind nur Signale mit Historie: Fear & Greed, Funding, Open Interest und
    Long/Short (Binance: nur 30 Tage), Preis vs. MA200, RSI, DVOL und MVRV. Die CoinGecko-Ersatzsignale
    fehlen. None, wenn es weder Binance-Kerzen noch -Futures gibt."""
    fut = "https://fapi.binance.com/futures/data"
    jobs = {
        "fng": (fetch_json, f"https://api.alternative.me/fng/?limit={days + 2}"),
        "klines": (get_binance_klines, symbol, "1d", 1000),
        "funding": (get_funding_history, symbol, days + 8),
        "oi": (futures_data, f"{fut}/openInterestHist?symbol={fpair(symbol)}&period=1d&limit=30", symbol),
        "ls": (futures_data, f"{fut}/globalLongShortAccountRatio?symbol={fpair(symbol)}&period=1d&limit=30", symbol),
    }
    if symbol in DVOL_SYMBOLS:
        jobs["dvol"] = (get_dvol_history, symbol, days)
    jobs["mvrv"] = (get_mvrv_history, symbol, days + 2)
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {k: pool.submit(safe, *job) for k, job in jobs.items()}
        got = {k: f.result() for k, f in futures.items()}

    klines = got["klines"][0] if got["klines"] else []
    if not klines and not (got["funding"] or got["oi"] or got["ls"]):
        return None
    fng = sorted((int(x["timestamp"]), int(x["value"])) for x in (got["fng"] or {}).get("data", []))
    closes = [(k[0] // 1000, float(k[4])) for k in klines]
    funding = got["funding"] or []
    oi = [(h["timestamp"] // 1000, float(h["sumOpenInterestValue"])) for h in got["oi"] or []]
    ls = [(h["timestamp"] // 1000, float(h["longShortRatio"])) for h in got["ls"] or []]
    dvol = got.get("dvol") or []
    mvrv = got.get("mvrv") or []

    now = time.time()
    today = int(now // 86400 * 86400)
    out = []
    for day in range(today - (days - 1) * 86400, today + 1, 86400):
        end = min(day + 86400, now)
        d: Dict[str, Any] = {}
        v = _as_of(fng, end, 2 * 86400)
        if v is not None:
            d["fear_greed"] = {"value": v}
        price = None
        i = bisect.bisect_right(closes, (day, float("inf"))) - 1
        if i >= 0 and closes[i][0] == day:
            price = closes[i][1]
            window = [c for _, c in closes[max(0, i - MA_PERIOD + 1):i + 1]]  # wie get_daily_closes
            d["price_ma200"] = price_vs_ma200(window)
            d["rsi"] = rsi(window)
        avg = funding_avg_7d(funding, end)
        if avg is not None:
            d["funding"] = {"avg_7d": avg}
        o1, o0 = _as_of(oi, end, 1.5 * 86400), _as_of(oi, end - 7 * 86400, 1.5 * 86400)
        if o1 and o0:
            d["open_interest"] = {"change_7d": (o1 - o0) / o0}
        r = _as_of(ls, end, 1.5 * 86400)
        if r is not None:
            d["long_short"] = {"ratio": r}
        j = bisect.bisect_right(dvol, (end, float("inf"))) - 1
        if j >= DVOL_WINDOW and end - dvol[j][0] <= 2 * 86400:
            window = [c for _, c in dvol[j - DVOL_WINDOW:j]]  # wie get_dvol: die 90 Tage davor
            d["dvol"] = {"value": dvol[j][1], "z": (dvol[j][1] - statistics.mean(window)) / statistics.stdev(window)}
        m = _as_of(mvrv, end - 1, 2 * 86400)   # Wert des Tages selbst (Zeitstempel 00:00), nicht des Folgetags
        if m is not None:
            d["mvrv"] = {"value": m}
        signals = score(d)
        if not signals:
            continue
        out.append({
            "time": day,
            "score": total_score(signals),
            "price": price,
            "signals": [{"name": x["name"], "points": x["points"]} for x in signals],
        })
    return {"days": out, "price_source": got["klines"][1] if got["klines"] else None}


SCORE_ZONES = [  # (Schlüssel, Name, von, bis) – Grenzen wie VERDICT_HOT / VERDICT_WARM
    ("cold2", "extreme Angst (bis −40)", -101, -VERDICT_HOT),
    ("cold1", "Angst (−40 bis −15)", -VERDICT_HOT, -VERDICT_WARM),
    ("neutral", "neutral (−15 bis +15)", -VERDICT_WARM, VERDICT_WARM),
    ("hot1", "Gier (+15 bis +40)", VERDICT_WARM, VERDICT_HOT),
    ("hot2", "überhitzt (ab +40)", VERDICT_HOT, 101),
]


def score_zone(total: int) -> str:
    for key, _, lo, hi in SCORE_ZONES:
        if lo < total <= hi if key.startswith("cold") else lo <= total < hi:
            return key
    return "neutral"


def score_track_record(days: List[Dict[str, Any]], horizons: Tuple[int, ...] = (30, 90)) -> Dict[str, Any]:
    """Wie entwickelte sich der Kurs nach Tagen in jeder Score-Zone? Je Zone und Zeitraum: Anzahl Tage,
    Anteil mit höherem Kurs danach, Median der Änderung; dazu dieselben Werte über alle Tage zum Vergleich.
    Rückblickend und mit Überlappung der Zeiträume – ein Indiz, keine Prognose."""
    price = {d["time"]: d["price"] for d in days if d.get("price")}

    def stats(rows: List[Dict[str, Any]], n: int) -> Optional[Dict[str, Any]]:
        fw = [price[d["time"] + n * 86400] / d["price"] - 1 for d in rows
              if d.get("price") and d["time"] + n * 86400 in price]
        if not fw:
            return None
        return {"n": len(fw), "up": sum(x > 0 for x in fw) / len(fw), "median": statistics.median(fw)}

    zones = {}
    for key, name, _, _ in SCORE_ZONES:
        rows = [d for d in days if score_zone(d["score"]) == key]
        zones[key] = {"name": name, **{f"{n}d": stats(rows, n) for n in horizons}}
    with_price = [d["time"] for d in days if d.get("price")]
    return {"zones": zones, "all": {f"{n}d": stats(days, n) for n in horizons},
            "since": min(with_price) if with_price else None}


# --- On-Chain (Coin Metrics Community-API, kostenlos ohne Key) -------------------------------
# Börsenflüsse und -bestand gibt es dort nur für BTC und ETH, MVRV für viele größere Coins (nicht SOL).
# Tageswerte, der letzte Tag ist gestern. Anzeige auf der Detailseite (Karte „On-Chain“).
COINMETRICS_URL = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
ONCHAIN_DAYS = 730
COINMETRICS_TIMEOUT = 60
ONCHAIN_METRICS = ("FlowInExUSD,FlowOutExUSD,SplyExNtv,CapMVRVCur,PriceUSD", "CapMVRVCur,PriceUSD")


def get_mvrv_history(symbol: str, days: int, timeout: float = COINMETRICS_TIMEOUT) -> List[Tuple[int, float]]:
    """MVRV je Tag als (Zeit, Wert); leer, wenn Coin Metrics den Coin nicht führt."""
    start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    try:
        rows = fetch_json(f"{COINMETRICS_URL}?assets={symbol.lower()}&metrics=CapMVRVCur&frequency=1d"
                          f"&start_time={start}&page_size=10000", timeout).get("data") or []
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 403, 404):
            return []
        raise
    return [(int(datetime.strptime(r["time"][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()),
             float(r["CapMVRVCur"])) for r in rows if r.get("CapMVRVCur")]


# Aktueller MVRV-Wert für den Score. Coin Metrics antwortet meist in < 1 s, aber unregelmäßig auch erst
# nach 40 s und mehr – deshalb wartet der Score nie darauf: Bekannte Werte (bis 2 Tage alt) kommen sofort
# und werden im Hintergrund erneuert; nur der allererste Abruf je Coin wartet kurz (MVRV_WAIT).
MVRV_REFRESH_SECONDS = 3600      # Tageswert: stündlich erneuern reicht
MVRV_MAX_AGE = 2 * 86400         # älter wird nicht mehr verwendet
MVRV_WAIT = 8
_mvrv_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}   # Symbol → (geladen um, Wert oder None)
_mvrv_loading: set = set()
_mvrv_lock = threading.Lock()


def _load_mvrv(symbol: str) -> None:
    try:
        url = (f"{COINMETRICS_URL}?assets={symbol.lower()}&metrics=CapMVRVCur&frequency=1d"
               f"&page_size=3&paging_from=end")   # gleichbleibende Adresse, ohne Datum
        try:
            rows = [r for r in fetch_json(url, COINMETRICS_TIMEOUT).get("data") or [] if r.get("CapMVRVCur")]
        except urllib.error.HTTPError as exc:
            if exc.code not in (400, 403, 404):   # 400 = Coin nicht im kostenlosen Angebot
                raise
            rows = []
        value = None
        if rows:
            r = rows[-1]
            t = int(datetime.strptime(r["time"][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
            value = {"value": float(r["CapMVRVCur"]), "time": t}
        _mvrv_cache[symbol] = (time.time(), value)
    except Exception as exc:   # Netzfehler: alten Wert behalten, beim nächsten Aufruf erneut
        print(f"  [warn] MVRV {symbol}: {exc}")
    finally:
        with _mvrv_lock:
            _mvrv_loading.discard(symbol)


def get_mvrv(symbol: str) -> Optional[Dict[str, Any]]:
    """Aktuelles MVRV (letzter Tageswert, also gestern) für den Score; None ohne (aktuelle) Daten."""
    hit = _mvrv_cache.get(symbol)
    if not hit or time.time() - hit[0] > MVRV_REFRESH_SECONDS:
        with _mvrv_lock:
            start = symbol not in _mvrv_loading
            _mvrv_loading.add(symbol)
        if start:
            th = threading.Thread(target=_load_mvrv, args=(symbol,), daemon=True, name=f"mvrv-{symbol}")
            th.start()
            if not hit:
                th.join(MVRV_WAIT)
                hit = _mvrv_cache.get(symbol)
    if not hit or not hit[1] or time.time() - hit[1]["time"] > MVRV_MAX_AGE + 86400:
        return None
    return hit[1]


def get_onchain(symbol: str, days: int = ONCHAIN_DAYS) -> Optional[Dict[str, Any]]:
    """Börsenzu- und -abflüsse (USD), Bestand auf Börsen (Coins), MVRV und Kurs je Tag; None, wenn
    Coin Metrics den Coin nicht führt. Erst alle Kennzahlen, sonst nur MVRV (Altcoins)."""
    start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
    rows = None
    for metrics in ONCHAIN_METRICS:
        url = (f"{COINMETRICS_URL}?assets={symbol.lower()}&metrics={metrics}&frequency=1d"
               f"&start_time={start}&page_size=10000")
        try:
            # Coin Metrics braucht für eine Abfrage, die es länger nicht gab, manchmal über 40 s
            rows = fetch_json(url, COINMETRICS_TIMEOUT).get("data") or None
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 403, 404):   # Coin oder Kennzahl nicht im kostenlosen Angebot
                continue
            raise
        if rows:
            break
    if not rows:
        return None

    def num(r: Dict[str, Any], k: str) -> Optional[float]:
        v = r.get(k)
        return float(v) if v not in (None, "") else None

    out = [{"time": int(datetime.strptime(r["time"][:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()),
            "inflow": num(r, "FlowInExUSD"), "outflow": num(r, "FlowOutExUSD"),
            "supply_ex": num(r, "SplyExNtv"), "mvrv": num(r, "CapMVRVCur"), "price": num(r, "PriceUSD")}
           for r in rows]
    has_flows = any(d["inflow"] is not None for d in out)
    if not has_flows and not any(d["mvrv"] is not None for d in out):
        return None
    return {"symbol": symbol, "source": "Coin Metrics (Community)", "has_flows": has_flows, "days": out}


def collect_global() -> Dict[str, Any]:
    """Marktweite Daten, unabhängig vom Coin."""
    return {
        "fear_greed": safe(get_fear_greed),
        "stablecoins": safe(get_stablecoins),
        "btc_dominance": safe(get_btc_dominance),
        "trending": safe(get_trending),
    }


def collect_symbol(symbol: str, coin_id: Optional[str] = None,
                   with_market: bool = False, market: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Coin-spezifische Derivate- und Kursdaten.

    with_market: Marktkapitalisierung mit abfragen (eine CoinGecko-Abfrage).
    Fehlen Binance-Futures oder -Kursdaten, kommen Ersatz-Signale aus derselben Abfrage.
    market: schon geladenes Ergebnis von get_coingecko_coins (auch leer = fehlgeschlagen); dann keine eigene Abfrage."""
    closes = safe(get_daily_closes, symbol)
    data = {
        "funding": safe(get_funding, symbol),
        "open_interest": safe(get_open_interest, symbol),
        "long_short": safe(get_long_short, symbol),
        "price": closes[-1] if closes else None,
        "price_ma200": price_vs_ma200(closes) if closes else None,
        "rsi": rsi(closes) if closes else None,
        "dvol": safe(get_dvol, symbol),
        "mvrv": safe(get_mvrv, symbol),
    }
    no_futures = not (data["funding"] or data["open_interest"] or data["long_short"])
    if with_market or no_futures or not closes:
        cg = market if market is not None else safe(get_coingecko_coin, symbol, coin_id) or {}
        if with_market:
            data["market_cap"] = cg.get("market_cap")
            data["market_stats"] = cg.get("market_stats")
            data["name"] = cg.get("name")
            data["coin_id"] = cg.get("coin_id")
        if no_futures or not closes:
            data["momentum"] = cg.get("momentum")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto-Sentiment-Übersicht")
    parser.add_argument("--symbol", default="BTC", help="Coin-Symbol, z. B. BTC oder ETH")
    parser.add_argument("--json", action="store_true", help="Rohdaten als JSON ausgeben")
    args = parser.parse_args()
    symbol = args.symbol.upper()

    data = {**collect_global(), **collect_symbol(symbol, with_market=True)}
    signals = score(data)

    if args.json:
        total = total_score(signals)
        print(json.dumps({"symbol": symbol, "data": data, "signals": signals,
                          "score": total, "verdict": verdict(total, len(signals)),
                          "situation": situation(data, signals, total)},
                         indent=2, ensure_ascii=False))
    else:
        print_report(symbol, data, signals)


if __name__ == "__main__":
    main()

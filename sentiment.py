#!/usr/bin/env python3
"""Tägliche Crypto-Sentiment-Übersicht.

Sammelt kostenlose, öffentliche Marktdaten (ohne API-Keys) und berechnet
einen Überhitzungs-Score von -100 (extreme Angst) bis +100 (überhitzt).

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

Die Schwellenwerte sind Erfahrungswerte, keine Naturgesetze. Keine Anlageberatung.
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
from datetime import datetime
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
# Ersatz-Signale aus CoinGecko-Kursdaten für Coins ohne Binance-Futures oder -Kursdaten
MOMENTUM_7D_SCALE = [(-0.25, -100), (0.0, 0), (0.25, 100)]
MOMENTUM_30D_SCALE = [(-0.50, -100), (0.0, 0), (0.50, 100)]
TURNOVER_SCALE = [(0.05, 0), (0.50, 100)]           # Tagesvolumen / Marktkap.; Vorzeichen folgt dem 7T-Trend

SIGNAL_STRONG = 50        # ab hier gilt ein Einzelsignal als ausgeprägt
VERDICT_HOT = 40          # Gesamtscore ab +40 = überhitzt, ab -40 = extreme Angst
VERDICT_WARM = 15         # ab ±15 = leicht gierig / ängstlich

RSI_PERIOD = 14
MA_PERIOD = 200           # Tage für den gleitenden Durchschnitt
DVOL_SYMBOLS = {"BTC", "ETH"}  # Deribit bietet DVOL nur für diese Coins
DVOL_WINDOW = 90          # Tage für Mittelwert und Standardabweichung
FUNDING_HISTORY_DAYS = 90 # Funding-Verlauf auf der Detailseite
FUNDING_BASE_HOURS = 8    # FUNDING_SCALE gilt pro 8 h; kürzere Takte werden darauf umgerechnet
SCORE_HISTORY_DAYS = 365  # rückwirkend berechneter Score-Verlauf auf der Detailseite


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


def fetch_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    coingecko = COINGECKO_HOST in url
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        if coingecko:
            _coingecko_wait()
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
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
        "yesterday": int(data[1]["value"]),
        "week_ago": int(data[7]["value"]),
    }


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
        f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}USDT&limit=200", symbol
    ))
    last = funding[-1]
    values = [f["rate_8h"] for f in funding if f["time"] > last["time"] - 7 * 86400]
    return {"last": last["rate"], "interval_h": last["interval_h"], "avg_7d": sum(values) / len(values)}


def get_open_interest(symbol: str) -> Dict[str, Any]:
    hist = futures_data(
        "https://fapi.binance.com/futures/data/openInterestHist"
        f"?symbol={symbol}USDT&period=1d&limit=8", symbol
    )
    first = float(hist[0]["sumOpenInterestValue"])
    last = float(hist[-1]["sumOpenInterestValue"])
    return {"usd": last, "change_7d": (last - first) / first}


def get_long_short(symbol: str) -> Dict[str, Any]:
    data = futures_data(
        "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
        f"?symbol={symbol}USDT&period=1d&limit=1", symbol
    )
    return {"ratio": float(data[-1]["longShortRatio"])}


def get_funding_history(symbol: str, days: int = FUNDING_HISTORY_DAYS) -> List[Dict[str, Any]]:
    """Funding-Zahlungen der letzten `days` Tage (Binance liefert max. 1000 je Abfrage,
    ältere werden mit endTime nachgeladen, höchstens 4 Abfragen)."""
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}USDT&limit=1000"
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
        f"?symbol={symbol}USDT&period=4h&limit=500", symbol
    )
    out = []
    for h in hist:
        value, amount = float(h["sumOpenInterestValue"]), float(h["sumOpenInterest"])
        out.append({"time": h["timestamp"] // 1000, "usd": value, "price": value / amount if amount else None})
    return out


def get_binance_klines(symbol: str, interval: str, limit: int) -> Tuple[List[list], str]:
    """Rohkerzen vom USDT-Paar: Spot, oder Perpetual-Futures, wenn Spot fehlt oder die
    Spot-Historie kürzer ist (z. B. HYPE: Spot erst seit Kurzem, Futures deutlich länger)."""
    spot = safe(fetch_json,
                f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}")
    if spot and len(spot) >= limit:
        return spot, "Binance Spot"
    futures = safe(fetch_json,
                   f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}USDT&interval={interval}&limit={limit}")
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
    } for c in coins}


def get_coingecko_coin(symbol: str, coin_id: Optional[str] = None) -> Dict[str, Any]:
    """Marktkapitalisierung, Kennzahlen und Kursmomentum eines Coins (CoinGecko).

    Mit coin_id eindeutig, sonst per Symbol (bei Mehrdeutigkeit der größte Coin)."""
    query = f"ids={coin_id}" if coin_id else f"symbols={symbol.lower()}&include_tokens=top"
    c = fetch_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&{query}&sparkline=true" + STATS_CHANGES
    )[0]
    mcap, volume = c.get("market_cap"), c.get("total_volume")
    change_7d = c.get("price_change_percentage_7d_in_currency")
    change_30d = c.get("price_change_percentage_30d_in_currency")
    hourly = (c.get("sparkline_in_7d") or {}).get("price") or []
    rsi_hourly = rsi(hourly)
    return {
        "market_cap": {"usd": float(mcap), "rank": c.get("market_cap_rank")} if mcap else None,
        "market_stats": market_stats(c),
        "name": c.get("name"),
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


def get_news() -> List[Dict[str, Any]]:
    """Alle Feeds parallel, neueste zuerst; ValueError, wenn kein Feed erreichbar ist."""
    with ThreadPoolExecutor(max_workers=len(NEWS_FEEDS)) as pool:
        results = list(pool.map(lambda f: safe(get_news_feed, *f), NEWS_FEEDS))
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


def score(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Liefert Einzelsignale von -100 (Angst) über 0 (neutral) bis +100 (überhitzt/Gier)."""
    signals = []

    def add(name: str, points: int, reason: str, hot: str, cold: str, neutral: str) -> None:
        word = hot if points >= SIGNAL_STRONG else cold if points <= -SIGNAL_STRONG else neutral
        signals.append({"name": name, "points": points, "reason": f"{reason}, {word}"})

    fng = d.get("fear_greed")
    if fng:
        v = fng["value"]
        add("Fear & Greed", scale(v, FNG_SCALE), f"{v}", "Gier", "Angst", "neutral")

    fr = d.get("funding")
    if fr:
        r = fr["avg_7d"]
        takt = fr.get("interval_h", FUNDING_BASE_HOURS)
        note = f" (Takt {takt} h, umgerechnet)" if takt != FUNDING_BASE_HOURS else ""
        add("Funding Rate", scale(r, FUNDING_SCALE), f"Ø {r:.4%} / 8h{note}",
            "Longs zahlen viel", "Shorts dominieren", "normal")

    oi = d.get("open_interest")
    if oi:
        c = oi["change_7d"]
        add("Open Interest", scale(c, OI_SCALE), f"{c:+.1%} in 7 Tagen",
            "viel neuer Hebel", "starker Hebelabbau", "normal")

    ma = d.get("price_ma200")
    if ma:
        dist = ma["distance"]
        add("Preis vs. MA200", scale(dist, MA200_SCALE), f"{dist:+.1%}",
            "stark gestreckt", "deutlich unter dem Trend", "im Trendbereich")

    rs = d.get("rsi")
    if rs:
        v = rs["value"]
        add("RSI (14)", scale(v, RSI_SCALE), f"{v:.0f}", "überkauft", "überverkauft", "neutral")

    dv = d.get("dvol")
    if dv:
        v, z = dv["value"], dv["z"]
        add("Optionsvola (DVOL)", scale(z, DVOL_SCALE), f"{v:.1f} ({z:+.1f}σ)",
            "ungewöhnlich ruhig, Sorglosigkeit", "ungewöhnlich hoch, Absicherung gefragt", "normal")

    mo = d.get("momentum")
    if mo:
        c7, c30, turnover = mo["change_7d"], mo["change_30d"], mo["turnover"]
        if c7 is not None:
            add("Momentum 7T", scale(c7, MOMENTUM_7D_SCALE), f"{c7:+.1%}",
                "starker Anstieg", "starker Rückgang", "moderat")
        if c30 is not None:
            add("Momentum 30T", scale(c30, MOMENTUM_30D_SCALE), f"{c30:+.1%}",
                "starker Anstieg", "starker Rückgang", "moderat")
        if turnover is not None and c7 is not None:
            pts = scale(turnover, TURNOVER_SCALE) * (1 if c7 >= 0 else -1)
            add("Umschlag", pts, f"{turnover:.0%} der Marktkap. pro Tag",
                "Spekulationsrausch", "Panikverkäufe", "normal")
        if not rs and mo["rsi_hourly"] is not None:
            v = mo["rsi_hourly"]
            add("RSI (14 Std.)", scale(v, RSI_SCALE), f"{v:.0f}", "überkauft", "überverkauft", "neutral")

    ls = d.get("long_short")
    if ls:
        r = ls["ratio"]
        add("Long/Short Ratio", scale(r, LS_SCALE), f"{r:.2f}",
            "Masse stark long", "Masse short", "ausgeglichen")

    return signals


def total_score(signals: List[Dict[str, Any]]) -> int:
    """Gesamtscore = Durchschnitt der Einzelsignale (-100 … +100)."""
    if not signals:
        return 0
    return round(sum(s["points"] for s in signals) / len(signals))


def verdict(total: int, count: int) -> str:
    if count == 0:
        return "Keine Daten verfügbar."
    if total >= VERDICT_HOT:
        return "ÜBERHITZT: Vorsicht, keine neuen Hebelpositionen, Gewinne sichern erwägen."
    if total >= VERDICT_WARM:
        return "Leicht gierig: Markt läuft heiß, nicht der FOMO folgen."
    if total <= -VERDICT_HOT:
        return "EXTREME ANGST: Historisch oft gute Kaufzonen (DCA fortsetzen)."
    if total <= -VERDICT_WARM:
        return "Leicht ängstlich: eher Kaufgelegenheit als Verkaufssignal."
    return "Neutral: Keine Extreme erkennbar."


def _pct_de(v: float, digits: int = 1, sign: bool = True) -> str:
    """Anteil als Prozent in deutscher Schreibweise, z. B. −12,3 %."""
    r = round(v * 100, digits)
    pre = ("+" if r > 0 else "−" if r < 0 else "") if sign else ""
    return f"{pre}{abs(r):.{digits}f} %".replace(".", ",")


# Lagebild: Stimmung (Score) × Trend (Kurs zum MA200). Titel und Einordnung je Feld sind
# Erfahrungswerte, keine Regeln und keine Anlageberatung.
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


def situation(d: Dict[str, Any], signals: List[Dict[str, Any]], total: int) -> Optional[Dict[str, Any]]:
    """Lagebild für den Kopf der Detailseite: Einordnung aus Stimmung und Trend, ein Satz zur Lage
    und was für bzw. gegen steigende Kurse spricht.

    Stimmung: Score ab ±VERDICT_WARM. Trend: Kurs über/unter dem MA200, ohne MA200 die Kursänderung
    über 30 Tage. Ausgeprägte Signale (ab ±SIGNAL_STRONG) werden antizyklisch gelesen: Angst und
    abgebauter Hebel sprechen eher für steigende Kurse, Gier und viel Hebel dagegen."""
    if not signals:
        return None
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
        trend, basis = ("up" if dist >= 0 else "down"), "MA200"
        where = f"{_pct_de(abs(dist), sign=False)} {'über' if dist >= 0 else 'unter'} dem MA200"
        trend_text = f"{'Aufwärtstrend' if dist >= 0 else 'Abwärtstrend'} ({where})"
        (pro if dist >= 0 else contra).append(
            f"Kurs {where}: langfristiger {'Aufwärtstrend' if dist >= 0 else 'Abwärtstrend'}")
    else:
        c30 = ((d.get("market_stats") or {}).get("change") or {}).get("30d")
        if c30 is None:
            c30 = (d.get("momentum") or {}).get("change_30d")
        if c30:
            trend, basis = ("up" if c30 > 0 else "down"), "30 Tage"
            trend_text = f"{'Aufwärts' if c30 > 0 else 'Abwärts'}bewegung ({_pct_de(c30)} in 30 Tagen)"
            (pro if c30 > 0 else contra).append(
                f"Kurs {_pct_de(c30)} in 30 Tagen (kein MA200 verfügbar, Trend nur grob)")

    def num(v: float, digits: int) -> str:
        return f"{v:.{digits}f}".replace(".", ",")

    fng, rs, fr, oi = d.get("fear_greed"), d.get("rsi"), d.get("funding"), d.get("open_interest")
    ls, dv, mo = d.get("long_short"), d.get("dvol"), d.get("momentum") or {}
    if fng:
        strong("Fear & Greed",
               (f"Fear & Greed {fng['value']}: Angst am Markt, historisch oft nahe an Tiefs", ""),
               (f"Fear & Greed {fng['value']}: Gier am Markt, historisch oft nahe an Hochs", ""))
    if ma:
        strong("Preis vs. MA200", ("", ""),
               ("Kurs weit über dem MA200: stark gestreckt, anfällig für Rücksetzer", "stark gestreckt"))
    if rs:
        low, high = RSI_SCALE[0][0], RSI_SCALE[-1][0]   # 30 / 70: erst dort gilt der RSI als überverkauft / überkauft
        sold = "überverkauft" if rs["value"] <= low else "schwach, nahe am überverkauften Bereich"
        bought = "überkauft" if rs["value"] >= high else "stark, nahe am überkauften Bereich"
        strong("RSI (14)",
               (f"RSI {num(rs['value'], 0)}: {sold}, Gegenbewegungen sind häufig",
                "überverkauft" if rs["value"] <= low else "RSI schwach"),
               (f"RSI {num(rs['value'], 0)}: {bought}, Rücksetzer sind häufig",
                "überkauft" if rs["value"] >= high else "RSI stark"))
    if mo.get("rsi_hourly") is not None:
        strong("RSI (14 Std.)",
               (f"RSI (Stunden) {num(mo['rsi_hourly'], 0)}: kurzfristig überverkauft", "kurzfristig überverkauft"),
               (f"RSI (Stunden) {num(mo['rsi_hourly'], 0)}: kurzfristig überkauft", "kurzfristig überkauft"))
    if fr:
        rate = f"Funding {_pct_de(fr['avg_7d'], 4)} pro 8 h"
        strong("Funding Rate",
               (f"{rate}: Shorts dominieren, ein Short-Squeeze ist möglich", "Shorts dominieren"),
               (f"{rate}: Longs zahlen viel, der Markt ist gehebelt long", "viel Long-Hebel"))
    if oi:
        chg = f"Open Interest {_pct_de(oi['change_7d'])} in 7 Tagen"
        strong("Open Interest",
               (f"{chg}: Hebel wurde abgebaut, weniger Liquidationsdruck", "Hebel wird abgebaut"),
               (f"{chg}: viel neuer Hebel, Risiko von Liquidationswellen", "viel neuer Hebel"))
    if ls:
        ratio = f"Long/Short {num(ls['ratio'], 2)}"
        strong("Long/Short Ratio",
               (f"{ratio}: ungewöhnlich viele Konten short (Kontraindikator)", "Masse short"),
               (f"{ratio}: die Masse ist stark long (Kontraindikator)", "Masse stark long"))
    if dv:
        z = f"{'+' if dv['z'] >= 0 else '−'}{num(abs(dv['z']), 1)}σ"
        strong("Optionsvola (DVOL)",
               (f"Optionsvola ungewöhnlich hoch ({z}): Absicherung gefragt, viel Angst ist eingepreist", "hohe Optionsvola"),
               (f"Optionsvola ungewöhnlich niedrig ({z}): Sorglosigkeit", "niedrige Optionsvola"))
    if mo.get("change_7d") is not None:
        c7 = _pct_de(mo["change_7d"])
        strong("Momentum 7T",
               (f"{c7} in 7 Tagen: starker Ausverkauf, eine Gegenbewegung ist möglich", "starker Ausverkauf"),
               (f"{c7} in 7 Tagen: steiler Anstieg, anfällig für Rücksetzer", "steiler Anstieg"))
    if mo.get("turnover") is not None:
        strong("Umschlag",
               ("Sehr hoher Umschlag im Rückgang: Panikverkäufe, oft nahe an Zwischentiefs", "Panikverkäufe"),
               ("Sehr hoher Umschlag im Anstieg: Spekulationsrausch", "Spekulationsrausch"))

    mood = "greed" if total >= VERDICT_WARM else "fear" if total <= -VERDICT_WARM else "neutral"
    mood_text = ("Stimmung überhitzt" if total >= VERDICT_HOT else "Stimmung leicht gierig" if total >= VERDICT_WARM
                 else "Stimmung extrem ängstlich" if total <= -VERDICT_HOT
                 else "Stimmung leicht ängstlich" if total <= -VERDICT_WARM else "Stimmung neutral")
    words = [w for w in words if w]
    summary = ", ".join(x for x in (trend_text, mood_text) if x) + ("; " + ", ".join(words[:3]) if words else "") + "."
    title, text = SITUATIONS[(mood, trend)]
    return {"mood": mood, "trend": trend, "trend_basis": basis, "title": title, "text": text,
            "summary": summary, "pro": pro, "contra": contra}


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
        for label, items in (("Für steigende Kurse spricht", sit["pro"]), ("Dagegen spricht", sit["contra"])):
            if items:
                print(f"  {label}:")
                for item in items:
                    print(f"    · {item}")
    print(f"{line}\n  Keine Anlageberatung. Schwellenwerte sind Erfahrungswerte.\n")


LIQ_MODEL_HOURS = 500    # Binance liefert OI-Historie höchstens 500 Punkte (1 h ≈ 21 Tage)


def get_liquidation_inputs(symbol: str) -> Dict[str, Any]:
    """Futures-Kerzen und Open Interest je Stunde für das Liquidations-Modell der Detailseite.

    Beides von Binance Futures (Perpetual), damit Kurs und OI zum selben Markt gehören."""
    klines = futures_data(
        f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}USDT&interval=1h&limit={LIQ_MODEL_HOURS}", symbol
    )
    oi = futures_data(
        "https://fapi.binance.com/futures/data/openInterestHist"
        f"?symbol={symbol}USDT&period=1h&limit={LIQ_MODEL_HOURS}", symbol
    )
    return {
        "candles": [{"time": k[0] // 1000, "open": float(k[1]), "high": float(k[2]),
                     "low": float(k[3]), "close": float(k[4])} for k in klines],
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
    Long/Short (Binance: nur 30 Tage), Preis vs. MA200, RSI und DVOL. Die CoinGecko-Ersatzsignale
    fehlen. None, wenn es weder Binance-Kerzen noch -Futures gibt."""
    fut = "https://fapi.binance.com/futures/data"
    jobs = {
        "fng": (fetch_json, f"https://api.alternative.me/fng/?limit={days + 2}"),
        "klines": (get_binance_klines, symbol, "1d", 1000),
        "funding": (get_funding_history, symbol, days + 8),
        "oi": (futures_data, f"{fut}/openInterestHist?symbol={symbol}USDT&period=1d&limit=30", symbol),
        "ls": (futures_data, f"{fut}/globalLongShortAccountRatio?symbol={symbol}USDT&period=1d&limit=30", symbol),
    }
    if symbol in DVOL_SYMBOLS:
        jobs["dvol"] = (get_dvol_history, symbol, days)
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


def collect_global() -> Dict[str, Any]:
    """Marktweite Daten, unabhängig vom Coin."""
    return {
        "fear_greed": safe(get_fear_greed),
        "stablecoins": safe(get_stablecoins),
        "btc_dominance": safe(get_btc_dominance),
        "trending": safe(get_trending),
    }


def collect_symbol(symbol: str, coin_id: Optional[str] = None,
                   with_market: bool = False) -> Dict[str, Any]:
    """Coin-spezifische Derivate- und Kursdaten.

    with_market: Marktkapitalisierung mit abfragen (eine CoinGecko-Abfrage).
    Fehlen Binance-Futures oder -Kursdaten, kommen Ersatz-Signale aus derselben Abfrage."""
    closes = safe(get_daily_closes, symbol)
    data = {
        "funding": safe(get_funding, symbol),
        "open_interest": safe(get_open_interest, symbol),
        "long_short": safe(get_long_short, symbol),
        "price": closes[-1] if closes else None,
        "price_ma200": price_vs_ma200(closes) if closes else None,
        "rsi": rsi(closes) if closes else None,
        "dvol": safe(get_dvol, symbol),
    }
    no_futures = not (data["funding"] or data["open_interest"] or data["long_short"])
    if with_market or no_futures or not closes:
        cg = safe(get_coingecko_coin, symbol, coin_id) or {}
        if with_market:
            data["market_cap"] = cg.get("market_cap")
            data["market_stats"] = cg.get("market_stats")
            data["name"] = cg.get("name")
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

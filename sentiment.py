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
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

TIMEOUT = 15
RATE_LIMIT_RETRIES = 2    # erneute Versuche bei HTTP 429 (v. a. CoinGecko ohne API-Key)
USER_AGENT = "Mozilla/5.0 (sentiment-script)"

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
DVOL_SYMBOLS = {"BTC", "ETH"}  # Deribit bietet DVOL nur für diese Coins
DVOL_WINDOW = 90          # Tage für Mittelwert und Standardabweichung


def fetch_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == RATE_LIMIT_RETRIES:
                raise
            retry_after = exc.headers.get("Retry-After", "")
            time.sleep(min(int(retry_after), 10) if retry_after.isdigit() else 3 * (attempt + 1))


def safe(fn, *args) -> Optional[Any]:
    """Führt eine Abfrage aus; bei Fehlern wird None geliefert statt abzubrechen."""
    try:
        return fn(*args)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError,
            IndexError, TypeError, TimeoutError) as exc:
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


def get_funding(symbol: str) -> Dict[str, Any]:
    rates = fetch_json(
        f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}USDT&limit=21"
    )
    values = [float(r["fundingRate"]) for r in rates]
    return {"last": values[-1], "avg_7d": sum(values) / len(values)}


def get_open_interest(symbol: str) -> Dict[str, Any]:
    hist = fetch_json(
        "https://fapi.binance.com/futures/data/openInterestHist"
        f"?symbol={symbol}USDT&period=1d&limit=8"
    )
    first = float(hist[0]["sumOpenInterestValue"])
    last = float(hist[-1]["sumOpenInterestValue"])
    return {"usd": last, "change_7d": (last - first) / first}


def get_long_short(symbol: str) -> Dict[str, Any]:
    data = fetch_json(
        "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
        f"?symbol={symbol}USDT&period=1d&limit=1"
    )
    return {"ratio": float(data[-1]["longShortRatio"])}


def get_klines(symbol: str, interval: str, limit: int = 1000) -> List[Dict[str, Any]]:
    """Kerzen von Binance Spot (USDT-Paar) für das Chart auf der Detailseite."""
    klines = fetch_json(
        f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}"
    )
    return [
        {
            "time": k[0] // 1000,  # Öffnungszeit in Sekunden (UTC)
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[7]),  # Quote-Volumen, also in USDT
        }
        for k in klines
    ]


def get_daily_closes(symbol: str) -> List[float]:
    klines = fetch_json(
        f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval=1d&limit=200"
    )
    return [float(k[4]) for k in klines]


def price_vs_ma200(closes: List[float]) -> Dict[str, Any]:
    ma200 = sum(closes) / len(closes)
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


def get_market_caps(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Marktkapitalisierung mehrerer Coins in einer Abfrage (CoinGecko limitiert stark)."""
    # include_tokens=top: bei mehrdeutigen Symbolen den Coin mit der höchsten Marktkapitalisierung
    coins = fetch_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&symbols={','.join(symbols).lower()}&include_tokens=top"
    )
    return {c["symbol"].upper(): {"usd": float(c["market_cap"]), "rank": c.get("market_cap_rank")}
            for c in coins}


def get_coingecko_coin(symbol: str, coin_id: Optional[str] = None) -> Dict[str, Any]:
    """Marktkapitalisierung und Kursmomentum eines Coins (CoinGecko).

    Mit coin_id eindeutig, sonst per Symbol (bei Mehrdeutigkeit der größte Coin)."""
    query = f"ids={coin_id}" if coin_id else f"symbols={symbol.lower()}&include_tokens=top"
    c = fetch_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&{query}&sparkline=true&price_change_percentage=7d,30d"
    )[0]
    mcap, volume = c.get("market_cap"), c.get("total_volume")
    change_7d = c.get("price_change_percentage_7d_in_currency")
    change_30d = c.get("price_change_percentage_30d_in_currency")
    hourly = (c.get("sparkline_in_7d") or {}).get("price") or []
    rsi_hourly = rsi(hourly)
    return {
        "market_cap": {"usd": float(mcap), "rank": c.get("market_cap_rank")} if mcap else None,
        "momentum": {
            "change_7d": change_7d / 100 if change_7d is not None else None,
            "change_30d": change_30d / 100 if change_30d is not None else None,
            "turnover": volume / mcap if mcap and volume else None,
            "rsi_hourly": rsi_hourly["value"] if rsi_hourly else None,
        },
    }


def get_markets(page: int, per_page: int = 50) -> List[Dict[str, Any]]:
    """Rangliste nach Marktkapitalisierung, seitenweise (CoinGecko)."""
    coins = fetch_json(
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&order=market_cap_desc&per_page={per_page}&page={page}"
        "&price_change_percentage=1h,24h,7d,30d,1y"
    )

    def change(c: Dict[str, Any], period: str) -> Optional[float]:
        v = c.get(f"price_change_percentage_{period}_in_currency")
        return v / 100 if v is not None else None

    return [{
        "rank": c.get("market_cap_rank"),
        "id": c["id"],
        "symbol": c["symbol"].upper(),
        "name": c["name"],
        "image": c.get("image"),
        "price": c.get("current_price"),
        "market_cap": c.get("market_cap"),
        "volume": c.get("total_volume"),
        "change": {p: change(c, p) for p in ("1h", "24h", "7d", "30d", "1y")},
    } for c in coins]


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
        add("Funding Rate", scale(r, FUNDING_SCALE), f"Ø {r:.4%} / 8h",
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
    print(f"{line}\n  Keine Anlageberatung. Schwellenwerte sind Erfahrungswerte.\n")


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
        "price_ma200": price_vs_ma200(closes) if closes else None,
        "rsi": rsi(closes) if closes else None,
        "dvol": safe(get_dvol, symbol),
    }
    no_futures = not (data["funding"] or data["open_interest"] or data["long_short"])
    if with_market or no_futures or not closes:
        cg = safe(get_coingecko_coin, symbol, coin_id) or {}
        if with_market:
            data["market_cap"] = cg.get("market_cap")
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
                          "score": total, "verdict": verdict(total, len(signals))},
                         indent=2, ensure_ascii=False))
    else:
        print_report(symbol, data, signals)


if __name__ == "__main__":
    main()

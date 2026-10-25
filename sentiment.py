#!/usr/bin/env python3
"""Tägliche Crypto-Sentiment-Übersicht.

Sammelt kostenlose, öffentliche Marktdaten (ohne API-Keys) und berechnet
einen einfachen Überhitzungs-Score für BTC.

Quellen:
  - alternative.me     Fear & Greed Index
  - Binance Futures    Funding Rate, Open Interest, Long/Short Ratio
  - Binance Spot       Tageskerzen für den 200-Tage-Durchschnitt und RSI(14)
  - Deribit            DVOL (implizite Optionsvolatilität, nur BTC und ETH)
  - DefiLlama          Stablecoin-Marktkapitalisierung
  - CoinGecko          BTC-Dominanz, Trending Coins

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
from typing import Any, Dict, List, Optional

TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (sentiment-script)"

# Schwellenwerte für den Score
FNG_GREED = 80
FNG_FEAR = 20
FUNDING_HOT = 0.0005      # 0,05 % pro 8 h
FUNDING_COLD = 0.0        # negativ = Shorts zahlen
OI_CHANGE_HOT = 0.15      # +15 % Open Interest in 7 Tagen
MA200_STRETCH_HOT = 0.35  # Preis 35 % über 200-Tage-Durchschnitt
LS_RATIO_HOT = 2.0        # mehr als doppelt so viele Long- wie Short-Accounts
LS_RATIO_COLD = 0.9
RSI_PERIOD = 14
RSI_HOT = 70              # überkauft
RSI_COLD = 30             # überverkauft
DVOL_SYMBOLS = {"BTC", "ETH"}  # Deribit bietet DVOL nur für diese Coins
DVOL_WINDOW = 90          # Tage für Mittelwert und Standardabweichung
DVOL_Z_HOT = -1.5         # ungewöhnlich niedrige Vola = Sorglosigkeit
DVOL_Z_COLD = 1.5         # ungewöhnlich hohe Vola = Panik / Absicherung


def fetch_json(url: str) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


def get_stablecoins() -> Dict[str, Any]:
    chart = fetch_json("https://stablecoins.llama.fi/stablecoincharts/all")
    now = chart[-1]["totalCirculatingUSD"]["peggedUSD"]
    month_ago = chart[-31]["totalCirculatingUSD"]["peggedUSD"]
    return {"mcap": now, "change_30d": (now - month_ago) / month_ago}


def get_btc_dominance() -> Dict[str, Any]:
    data = fetch_json("https://api.coingecko.com/api/v3/global")["data"]
    return {"btc": data["market_cap_percentage"]["btc"]}


def get_trending() -> List[str]:
    coins = fetch_json("https://api.coingecko.com/api/v3/search/trending")["coins"]
    return [c["item"]["symbol"].upper() for c in coins[:10]]


# --- Auswertung -------------------------------------------------------------

def score(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Liefert Einzelsignale: +1 = überhitzt/Gier, -1 = Angst, 0 = neutral."""
    signals = []

    def add(name: str, points: int, reason: str) -> None:
        signals.append({"name": name, "points": points, "reason": reason})

    fng = d.get("fear_greed")
    if fng:
        v = fng["value"]
        if v >= FNG_GREED:
            add("Fear & Greed", 1, f"{v} = extreme Gier")
        elif v <= FNG_FEAR:
            add("Fear & Greed", -1, f"{v} = extreme Angst")
        else:
            add("Fear & Greed", 0, f"{v} = neutral")

    fr = d.get("funding")
    if fr:
        r = fr["avg_7d"]
        if r >= FUNDING_HOT:
            add("Funding Rate", 1, f"Ø {r:.4%} / 8h, Longs zahlen viel")
        elif r < FUNDING_COLD:
            add("Funding Rate", -1, f"Ø {r:.4%} / 8h, Shorts dominieren")
        else:
            add("Funding Rate", 0, f"Ø {r:.4%} / 8h, normal")

    oi = d.get("open_interest")
    if oi:
        c = oi["change_7d"]
        if c >= OI_CHANGE_HOT:
            add("Open Interest", 1, f"{c:+.1%} in 7 Tagen, viel neuer Hebel")
        else:
            add("Open Interest", 0, f"{c:+.1%} in 7 Tagen")

    ma = d.get("price_ma200")
    if ma:
        dist = ma["distance"]
        if dist >= MA200_STRETCH_HOT:
            add("Preis vs. MA200", 1, f"{dist:+.1%}, stark gestreckt")
        elif dist < 0:
            add("Preis vs. MA200", -1, f"{dist:+.1%}, unter dem Trend")
        else:
            add("Preis vs. MA200", 0, f"{dist:+.1%}, gesunder Aufwärtstrend")

    rs = d.get("rsi")
    if rs:
        v = rs["value"]
        if v >= RSI_HOT:
            add("RSI (14)", 1, f"{v:.0f}, überkauft")
        elif v <= RSI_COLD:
            add("RSI (14)", -1, f"{v:.0f}, überverkauft")
        else:
            add("RSI (14)", 0, f"{v:.0f}, neutral")

    dv = d.get("dvol")
    if dv:
        v, z = dv["value"], dv["z"]
        if z <= DVOL_Z_HOT:
            add("Optionsvola (DVOL)", 1, f"{v:.1f} ({z:+.1f}σ), ungewöhnlich ruhig, Sorglosigkeit")
        elif z >= DVOL_Z_COLD:
            add("Optionsvola (DVOL)", -1, f"{v:.1f} ({z:+.1f}σ), ungewöhnlich hoch, Absicherung gefragt")
        else:
            add("Optionsvola (DVOL)", 0, f"{v:.1f} ({z:+.1f}σ), normal")

    ls = d.get("long_short")
    if ls:
        r = ls["ratio"]
        if r >= LS_RATIO_HOT:
            add("Long/Short Ratio", 1, f"{r:.2f}, Masse stark long")
        elif r <= LS_RATIO_COLD:
            add("Long/Short Ratio", -1, f"{r:.2f}, Masse short")
        else:
            add("Long/Short Ratio", 0, f"{r:.2f}, ausgeglichen")

    return signals


def verdict(total: int, count: int) -> str:
    if count == 0:
        return "Keine Daten verfügbar."
    if total >= 3:
        return "ÜBERHITZT: Vorsicht, keine neuen Hebelpositionen, Gewinne sichern erwägen."
    if total >= 1:
        return "Leicht gierig: Markt läuft heiß, nicht der FOMO folgen."
    if total <= -3:
        return "EXTREME ANGST: Historisch oft gute Kaufzonen (DCA fortsetzen)."
    if total <= -1:
        return "Leicht ängstlich: eher Kaufgelegenheit als Verkaufssignal."
    return "Neutral: Keine Extreme erkennbar."


def fmt_usd(v: float) -> str:
    if v >= 1e9:
        return f"${v / 1e9:,.1f} Mrd."
    if v >= 1e6:
        return f"${v / 1e6:,.1f} Mio."
    return f"${v:,.2f}"


def print_report(symbol: str, d: Dict[str, Any], signals: List[Dict[str, Any]]) -> None:
    total = sum(s["points"] for s in signals)
    line = "─" * 60
    print(f"\n{line}\n  Crypto-Sentiment {symbol}  ·  {datetime.now():%d.%m.%Y %H:%M}\n{line}")

    ma = d.get("price_ma200")
    if ma:
        trend = "AUFWÄRTS (über MA200)" if ma["distance"] >= 0 else "ABWÄRTS (unter MA200)"
        print(f"  Preis:          {fmt_usd(ma['price'])}   MA200: {fmt_usd(ma['ma200'])}")
        print(f"  Trend:          {trend}")
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
        print(f"  Trending:       {', '.join(tr)}")

    print(f"\n  Signale:")
    for s in signals:
        mark = {1: "▲ +1", -1: "▼ -1", 0: "  ·0"}[s["points"]]
        print(f"    {mark}  {s['name']:<19} {s['reason']}")

    print(f"\n  Score: {total:+d} (Bereich -{len(signals)} … +{len(signals)})")
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


def collect_symbol(symbol: str) -> Dict[str, Any]:
    """Coin-spezifische Derivate- und Kursdaten."""
    closes = safe(get_daily_closes, symbol)
    return {
        "funding": safe(get_funding, symbol),
        "open_interest": safe(get_open_interest, symbol),
        "long_short": safe(get_long_short, symbol),
        "price_ma200": price_vs_ma200(closes) if closes else None,
        "rsi": rsi(closes) if closes else None,
        "dvol": safe(get_dvol, symbol),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Crypto-Sentiment-Übersicht")
    parser.add_argument("--symbol", default="BTC", help="Coin-Symbol, z. B. BTC oder ETH")
    parser.add_argument("--json", action="store_true", help="Rohdaten als JSON ausgeben")
    args = parser.parse_args()
    symbol = args.symbol.upper()

    data = {**collect_global(), **collect_symbol(symbol)}
    signals = score(data)

    if args.json:
        total = sum(s["points"] for s in signals)
        print(json.dumps({"symbol": symbol, "data": data, "signals": signals,
                          "score": total, "verdict": verdict(total, len(signals))},
                         indent=2, ensure_ascii=False))
    else:
        print_report(symbol, data, signals)


if __name__ == "__main__":
    main()

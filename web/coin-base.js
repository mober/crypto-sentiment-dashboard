// Detailseite: gemeinsame Konstanten und Helfer für alle Abschnitte (coin-*.js).
// $, esc, usd, usd6, pct, signed und tone kommen aus format.js.
// Alle coin-*.js sind normale Skripte im selben globalen Bereich; die Reihenfolge in coin.html
// zählt nur für Code, der beim Laden sofort läuft (der Start steht deshalb am Ende von coin.html).
const compact = (v) => v.toLocaleString(LOCALE, { notation: "compact", maximumFractionDigits: 1 });

const MAIN_SYMBOLS = ["BTC", "ETH", "SOL"];  // wie SYMBOLS in server.py
const INTERVAL_LABEL = { "1d": L("1T", "1D"), "4h": "4h", "1h": "1h", "15m": "15m", "5m": "5m", "3m": "3m", "1m": "1m" };  // wie KLINE_INTERVALS in server.py
const INTERVAL_KEY = "chartInterval";
const CHART_REFRESH_MS = 60 * 1000;   // wie KLINE_CACHE_SECONDS in server.py
const VISIBLE_BARS = 150;             // so viele Kerzen beim Öffnen sichtbar, der Rest per Scrollen
const MA_PERIODS = [50, 200];         // gleitende Durchschnitte über die Kerzen des gewählten Intervalls
const RSI_PERIOD = 14;                // wie RSI_PERIOD in sentiment.py
const RSI_LEVELS = [70, 30];          // überkauft / überverkauft, wie die Enden von RSI_SCALE
const PRICE_SCALE_WIDTH = 80;         // gleiche Achsenbreite in Kurs- und RSI-Chart, damit die Kerzen übereinander stehen

const params = new URLSearchParams(location.search);
const symbol = (params.get("symbol") || "").toUpperCase();
const coinId = params.get("id") || "";
const validSymbol = /^[A-Z0-9]{1,15}$/.test(symbol);

// --- Zahlen und Kacheln (Kopfbereich, Heatmap, Gebühren) -------------------

const num1 = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(1).replace(".", DEC);
const numDe = (v, digits = 1) => v.toLocaleString(LOCALE, { maximumFractionDigits: digits });
const chgSpan = (v) => v == null ? '<b class="muted">–</b>' : `<b class="${v > 0 ? "up" : v < 0 ? "down" : ""}">${pctSigned(v)}</b>`;
function statTile(tip, label, val, det = "") {
  return `<div class="stat"><div class="label"><span${tipAttr(tip)}>${label}</span></div><div class="val">${val}</div>${det}</div>`;
}

// --- Charts (Lightweight Charts) -------------------------------------------

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const alpha = (hex, a) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
};
// Lightweight Charts rechnet in UTC. Die Zeiten bleiben UTC (ein Verschieben auf Ortszeit
// erzeugt bei der Zeitumstellung doppelte Stunden, und das Chart bleibt leer);
// nur die Beschriftung wird in Ortszeit formatiert.
const fmtDate = (t, opts) => new Date(t * 1000).toLocaleString(LOCALE, opts);

function themeOptions() {
  const up = css("--up"), down = css("--down");
  return {
    chart: {
      layout: { background: { type: "solid", color: css("--card") }, textColor: css("--muted"),
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif', fontSize: 12 },
      grid: { vertLines: { color: css("--track") }, horzLines: { color: css("--track") } },
      rightPriceScale: { borderColor: css("--border") },
      timeScale: { borderColor: css("--border") },
      crosshair: {
        vertLine: { color: css("--muted"), labelBackgroundColor: css("--text") },
        horzLine: { color: css("--muted"), labelBackgroundColor: css("--text") },
      },
    },
    candles: { upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false },
    volUp: alpha(up, 0.35),
    volDown: alpha(down, 0.35),
    ma: { 50: { color: css("--hot") }, 200: { color: css("--cold") } },
    rsi: { color: css("--text") },
    rsiLevel: { 70: css("--hot"), 30: css("--cold") },
    bb: css("--ind-bb"),          // Bollinger-Bänder
    band: css("--ind-band"),      // Bull Market Support Band
    level: css("--muted"),        // Kursmarken, MACD-Nulllinie
    macd: css("--cold"),
    macdSignal: css("--hot"),
  };
}

function miniChart(el) {
  const t = themeOptions();
  return LightweightCharts.createChart(el, {
    ...t.chart,
    autoSize: true,
    crosshair: { ...t.chart.crosshair, mode: LightweightCharts.CrosshairMode.Magnet },
    timeScale: { ...t.chart.timeScale, timeVisible: false, tickMarkFormatter: tickLabelDays },
    layout: { ...t.chart.layout, attributionLogo: false },
    localization: { locale: LOCALE, timeFormatter: (ts) => fmtDate(ts, {
      weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) },
    handleScroll: false, handleScale: false,
  });
}
const tickLabelDays = (t, type) => type === 0 ? fmtDate(t, { year: "numeric" })
  : type === 1 ? fmtDate(t, { month: "short" }) : fmtDate(t, { day: "numeric", month: "short" });

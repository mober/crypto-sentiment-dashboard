// Detailseite: Candlestick-Chart mit Indikatoren (MA50/MA200, Bollinger-Bänder, Bull Market Support Band,
// Kursmarken) und unterem Fenster (RSI oder MACD), Live-Kurs per WebSocket von Binance.
// Alle Indikatoren entstehen im Browser aus den geladenen Kerzen, keine zusätzlichen Abfragen.
// Gemeinsame Helfer in coin-base.js.

// --- Chart ---------------------------------------------------------------

// Tick-Typen von Lightweight Charts: 0 Jahr, 1 Monat, 2 Tag, 3 Uhrzeit, 4 mit Sekunden
function tickLabel(t, type) {
  if (type === 0) return fmtDate(t, { year: "numeric" });
  if (type === 1) return fmtDate(t, { month: "short" });
  if (type === 2) return fmtDate(t, { day: "numeric", month: interval === "1d" ? undefined : "short" });
  return fmtDate(t, { hour: "2-digit", minute: "2-digit" });
}
const crosshairLabel = (t) => interval === "1d"
  ? fmtDate(t, { weekday: "short", day: "2-digit", month: "2-digit", year: "2-digit", timeZone: "UTC" })
  : fmtDate(t, { weekday: "short", day: "2-digit", month: "2-digit", year: "2-digit", hour: "2-digit", minute: "2-digit" });

let interval = "1d";
try { if (INTERVAL_LABEL[localStorage.getItem(INTERVAL_KEY)]) interval = localStorage.getItem(INTERVAL_KEY); } catch (e) { /* ignorieren */ }
if (INTERVAL_LABEL[params.get("interval")]) interval = params.get("interval");  // Link mit ?interval=… hat Vorrang

// Parameter der Indikatoren
const BB_PERIOD = 20, BB_MULT = 2;                // Bollinger: Mittelwert über 20 Kerzen ± 2 Standardabweichungen
const BAND_SMA_DAYS = 140, BAND_EMA_DAYS = 147;   // Bull Market Support Band: 20-Wochen-SMA und 21-Wochen-EMA
const MACD_FAST = 12, MACD_SLOW = 26, MACD_SIGNAL = 9;
const LEVEL_DAYS = 365;                           // 52-Wochen-Hoch/-Tief aus den Tageskerzen
const INTERVAL_SECS = { "1d": 86400, "4h": 14400, "1h": 3600, "15m": 900, "5m": 300, "3m": 180, "1m": 60 };

// Was angezeigt wird, gemerkt im Browser. lower = unteres Fenster: "rsi" | "macd" | "none"
const SHOW_KEY = "chartShow";
const show = { ma50: true, ma200: true, bb: false, band: false, levels: true, lower: "rsi" };
try {
  const saved = JSON.parse(localStorage.getItem(SHOW_KEY) || "{}");
  if (saved.rsi === false && !saved.lower) saved.lower = "none";  // frühere Einstellung „RSI aus“ übernehmen
  delete saved.rsi;
  Object.assign(show, saved);
  if (localStorage.getItem("chartShowMa") === "0") show.ma200 = false;  // alte Einstellung übernehmen
} catch (e) { /* ignorieren */ }
if (!["rsi", "macd", "none"].includes(show.lower)) show.lower = "rsi";

let chart = null, candleSeries = null, volumeSeries = null;
let rsiChart = null, rsiSeries = null, rsiLines = [];   // unteres Fenster (RSI oder MACD)
let macdSeries = null, macdSignalSeries = null, macdHistSeries = null, macdZero = null;
const mainSeries = {};   // Kennung → Linienserie im Kurs-Chart
let levelLines = [];     // waagerechte Kursmarken
let candles = [];        // aktuelle Kerzen (Zeit in UTC-Sekunden)
let ind = {};            // Kennung → Wert je Kerze (null = noch kein Wert)
let idxByTime = new Map();
let precision = 2;
let loadToken = 0;       // verwirft Antworten, wenn inzwischen ein anderes Intervall gewählt wurde
let pointerOn = null;    // "main" | "rsi": welches Chart Maus/Finger gerade bedient (für den Gleichlauf)
let hoverTime = null;    // Kerze unter dem Fadenkreuz (null = keine): die OHLC-Zeile zeigt sie statt der letzten

const fmtPrice = (v) => v.toLocaleString(LOCALE, { minimumFractionDigits: precision, maximumFractionDigits: precision });
// MACD-Werte reichen je nach Coin von Tausendern bis weit unter 1: vier gültige Stellen
const fmtMacd = (v) => v.toLocaleString(LOCALE, { maximumSignificantDigits: 4 });

// Linien im Kurs-Chart: Kennung → Schalter in show, Aussehen
const MAIN_LINES = {
  ma50:    { show: "ma50", opts: (t) => ({ color: t.ma[50].color, lineWidth: 1.5, title: "MA50" }) },
  ma200:   { show: "ma200", opts: (t) => ({ color: t.ma[200].color, lineWidth: 2, title: "MA200" }) },
  bbUp:    { show: "bb", opts: (t) => ({ color: t.bb, lineWidth: 1, lastValueVisible: false }) },
  bbMid:   { show: "bb", opts: (t) => ({ color: alpha(t.bb, 0.6), lineWidth: 1, lineStyle: 2, lastValueVisible: false }) },
  bbLow:   { show: "bb", opts: (t) => ({ color: t.bb, lineWidth: 1, lastValueVisible: false }) },
  bandSma: { show: "band", opts: (t) => ({ color: t.band, lineWidth: 2, title: "20W SMA" }) },
  bandEma: { show: "band", opts: (t) => ({ color: t.band, lineWidth: 2, lineStyle: 2, title: "21W EMA" }) },
};

// --- Rechnungen (je Kerze ein Wert oder null) ---

function smaArr(vals, n) {
  const out = new Array(vals.length).fill(null);
  if (n > vals.length) return out;
  let sum = 0;
  vals.forEach((v, i) => {
    sum += v;
    if (i >= n) sum -= vals[i - n];
    if (i >= n - 1) out[i] = sum / n;
  });
  return out;
}

// Exponentieller Durchschnitt, Start mit dem einfachen Durchschnitt der ersten n Werte; null-Werte am Anfang werden übersprungen
function emaArr(vals, n) {
  const out = new Array(vals.length).fill(null);
  const k = 2 / (n + 1);
  let prev = null, seed = 0, count = 0;
  vals.forEach((v, i) => {
    if (v == null) return;
    if (prev == null) {
      seed += v;
      if (++count === n) out[i] = prev = seed / n;
      return;
    }
    out[i] = prev = v * k + prev * (1 - k);
  });
  return out;
}

// RSI nach Wilder wie rsi() in sentiment.py, aber für jede Kerze
function rsiArr(closes) {
  const out = new Array(closes.length).fill(null);
  let avgGain = 0, avgLoss = 0;
  for (let i = 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    const g = Math.max(d, 0), l = Math.max(-d, 0);
    if (i <= RSI_PERIOD) {
      avgGain += g / RSI_PERIOD;
      avgLoss += l / RSI_PERIOD;
      if (i < RSI_PERIOD) continue;
    } else {
      avgGain = (avgGain * (RSI_PERIOD - 1) + g) / RSI_PERIOD;
      avgLoss = (avgLoss * (RSI_PERIOD - 1) + l) / RSI_PERIOD;
    }
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return out;
}

// Perioden des Bull Market Support Band in Kerzen des gewählten Intervalls (1T: 140/147, 4h: 840/882)
const bandPeriods = () => [BAND_SMA_DAYS, BAND_EMA_DAYS].map((d) => Math.round(d * 86400 / INTERVAL_SECS[interval]));

function calcIndicators() {
  const closes = candles.map((c) => c.close);
  ind = {};
  for (const p of MA_PERIODS) ind["ma" + p] = smaArr(closes, p);
  const mid = smaArr(closes, BB_PERIOD);
  const dev = mid.map((m, i) => {
    if (m == null) return null;
    let s = 0;
    for (let j = i - BB_PERIOD + 1; j <= i; j++) s += (closes[j] - m) ** 2;
    return Math.sqrt(s / BB_PERIOD);
  });
  ind.bbMid = mid;
  ind.bbUp = mid.map((m, i) => (m == null ? null : m + BB_MULT * dev[i]));
  ind.bbLow = mid.map((m, i) => (m == null ? null : m - BB_MULT * dev[i]));
  const [ps, pe] = bandPeriods();
  ind.bandSma = smaArr(closes, ps);
  ind.bandEma = emaArr(closes, pe);
  ind.rsi = rsiArr(closes);
  const fast = emaArr(closes, MACD_FAST), slow = emaArr(closes, MACD_SLOW);
  ind.macd = fast.map((f, i) => (f != null && slow[i] != null ? f - slow[i] : null));
  ind.macdSignal = emaArr(ind.macd, MACD_SIGNAL);
  ind.macdHist = ind.macd.map((m, i) => (m != null && ind.macdSignal[i] != null ? m - ind.macdSignal[i] : null));
  idxByTime = new Map(candles.map((c, i) => [c.time, i]));
}

const indAt = (key, time) => {
  const i = idxByTime.get(time);
  return i == null || !ind[key] ? null : ind[key][i];
};

// Kurs-Chart: nur Punkte mit Wert. Unteres Fenster: Leerpunkte (nur time) für Kerzen ohne Wert,
// damit beide Charts gleich viele Punkte haben und synchron scrollen.
const lineData = (key) => candles.flatMap((c, i) => (ind[key][i] == null ? [] : [{ time: c.time, value: ind[key][i] }]));
const paneData = (key) => candles.map((c, i) => (ind[key][i] == null ? { time: c.time } : { time: c.time, value: ind[key][i] }));
const macdHistData = (t = themeOptions()) => candles.map((c, i) => {
  const v = ind.macdHist[i];
  return v == null ? { time: c.time } : { time: c.time, value: v, color: v >= 0 ? t.volUp : t.volDown };
});
const volumeData = (t = themeOptions()) =>
  candles.map((c) => ({ time: c.time, value: c.volume, color: c.close >= c.open ? t.volUp : t.volDown }));

// Alle Indikator-Serien neu füllen
function setIndicatorData() {
  for (const k in MAIN_LINES) mainSeries[k].setData(lineData(k));
  rsiSeries.setData(paneData("rsi"));
  macdSeries.setData(paneData("macd"));
  macdSignalSeries.setData(paneData("macdSignal"));
  macdHistSeries.setData(macdHistData());
}

// Nach einer neuen oder geänderten letzten Kerze nur die Punkte ab from nachziehen (Zoom bleibt erhalten)
function updateIndicators(from) {
  const t = themeOptions();
  candles.forEach((c, i) => {
    if (c.time < from) return;
    for (const k in MAIN_LINES) if (ind[k][i] != null) mainSeries[k].update({ time: c.time, value: ind[k][i] });
    rsiSeries.update(ind.rsi[i] == null ? { time: c.time } : { time: c.time, value: ind.rsi[i] });
    macdSeries.update(ind.macd[i] == null ? { time: c.time } : { time: c.time, value: ind.macd[i] });
    macdSignalSeries.update(ind.macdSignal[i] == null ? { time: c.time } : { time: c.time, value: ind.macdSignal[i] });
    const h = ind.macdHist[i];
    macdHistSeries.update(h == null ? { time: c.time } : { time: c.time, value: h, color: h >= 0 ? t.volUp : t.volDown });
  });
}

function clearIndicators() {
  ind = {};
  idxByTime = new Map();
  for (const k in MAIN_LINES) mainSeries[k].setData([]);
  for (const s of [rsiSeries, macdSeries, macdSignalSeries, macdHistSeries]) s.setData([]);
}

// --- Kursmarken ---
// Allzeithoch (CoinGecko, aus coin-head.js), 52-Wochen-Hoch/-Tief und Eröffnung der Woche (Montag) und
// des Monats (UTC) aus den Tageskerzen, die coin-head.js für die Kennzahlen ohnehin lädt (dailyCandles)
function levelData() {
  const out = [];
  const st = statsCoin && statsCoin.data.market_stats;
  if (st && st.ath != null) out.push({ price: st.ath, title: L("Allzeithoch", "ATH"), style: 2 });
  const d = dailyCandles;
  if (d && d.length) {
    const win = d.slice(-LEVEL_DAYS);
    const full = win.length >= LEVEL_DAYS;
    out.push({ price: Math.max(...win.map((c) => c.high)), title: full ? L("52W-Hoch", "52W high") : L("Hoch", "High"), style: 2 });
    out.push({ price: Math.min(...win.map((c) => c.low)), title: full ? L("52W-Tief", "52W low") : L("Tief", "Low"), style: 2 });
    const last = d[d.length - 1];
    const now = new Date(last.time * 1000);
    const weekStart = last.time - ((now.getUTCDay() + 6) % 7) * 86400;
    const monthStart = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1) / 1000;
    const w = d.find((c) => c.time === weekStart), m = d.find((c) => c.time === monthStart);
    if (m) out.push({ price: m.open, title: L("Monat-Open", "Month open"), style: 1 });
    if (w && w !== m) out.push({ price: w.open, title: L("Woche-Open", "Week open"), style: 1 });
  }
  return out;
}

// Wird auch von coin-head.js aufgerufen, sobald Tageskerzen oder Allzeithoch da sind
function updateLevels() {
  if (!candleSeries) return;
  levelLines.forEach((l) => candleSeries.removePriceLine(l));
  levelLines = [];
  if (!show.levels || !candles.length) return;
  const t = themeOptions();
  levelLines = levelData().map((x) => candleSeries.createPriceLine({
    price: x.price, color: t.level, lineWidth: 1, lineStyle: x.style, axisLabelVisible: true, title: x.title,
  }));
}

// --- Aufbau ---

function initChart() {
  const LC = LightweightCharts;
  const t = themeOptions();
  const common = {
    ...t.chart,
    autoSize: true,
    crosshair: { ...t.chart.crosshair, mode: LC.CrosshairMode.Normal },
    rightPriceScale: { ...t.chart.rightPriceScale, minimumWidth: PRICE_SCALE_WIDTH },
    timeScale: { ...t.chart.timeScale, timeVisible: true, secondsVisible: false, rightOffset: 5, tickMarkFormatter: tickLabel },
    layout: { ...t.chart.layout, attributionLogo: false },  // Namensnennung mit Link steht im Footer
    localization: { locale: LOCALE, priceFormatter: fmtPrice, timeFormatter: crosshairLabel },
  };
  chart = LC.createChart($("chart"), common);
  candleSeries = chart.addCandlestickSeries({ ...t.candles, priceLineVisible: true });
  candleSeries.priceScale().applyOptions({ scaleMargins: { top: 0.1, bottom: 0.22 } });
  // Volumen unten im Chart, eigene unsichtbare Skala (keine zweite Preisachse)
  volumeSeries = chart.addHistogramSeries({
    priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false,
  });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false });
  for (const [k, def] of Object.entries(MAIN_LINES)) {
    mainSeries[k] = chart.addLineSeries({
      priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false, ...def.opts(t), visible: !!show[def.show],
    });
  }

  // Unteres Fenster in eigenem Chart (Lightweight Charts 4 kennt keine Unterfenster): RSI oder MACD
  rsiChart = LC.createChart($("rsi-chart"), {
    ...common,
    localization: { ...common.localization, priceFormatter: (v) => v.toFixed(0) },
  });
  rsiSeries = rsiChart.addLineSeries({
    ...t.rsi, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
    autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 100 } }),
    priceFormat: { type: "custom", formatter: (v) => v.toFixed(1).replace(".", DEC), minMove: 0.1 },
  });
  rsiSeries.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.08 } });
  rsiLines = RSI_LEVELS.map((level) => rsiSeries.createPriceLine({
    price: level, color: t.rsiLevel[level], lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: "",
  }));
  const macdFormat = { type: "custom", formatter: fmtMacd, minMove: 1e-12 };
  macdHistSeries = rsiChart.addHistogramSeries({ priceFormat: macdFormat, priceLineVisible: false, lastValueVisible: false });
  macdSeries = rsiChart.addLineSeries({
    color: t.macd, lineWidth: 1.5, priceFormat: macdFormat, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
  });
  macdSignalSeries = rsiChart.addLineSeries({
    color: t.macdSignal, lineWidth: 1.5, priceFormat: macdFormat, priceLineVisible: false, lastValueVisible: true, crosshairMarkerVisible: false,
  });
  macdZero = macdSeries.createPriceLine({ price: 0, color: t.level, lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: false, title: "" });

  // Gleichlauf: Scrollen/Zoomen und Fadenkreuz gehen von dem Chart aus, das gerade bedient wird
  for (const [id, name] of [["chart", "main"], ["rsi-chart", "rsi"]]) {
    $(id).addEventListener("pointerenter", () => { pointerOn = name; });
    $(id).addEventListener("pointerdown", () => { pointerOn = name; });
    $(id).addEventListener("pointerleave", () => {
      if (pointerOn === name) pointerOn = null;
      (name === "main" ? rsiChart : chart).clearCrosshairPosition();
    });
  }
  chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (r && pointerOn !== "rsi") rsiChart.timeScale().setVisibleLogicalRange(r);
  });
  rsiChart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    if (r && pointerOn === "rsi") chart.timeScale().setVisibleLogicalRange(r);
  });

  chart.subscribeCrosshairMove((p) => {
    const bar = p && p.time != null ? candles.find((c) => c.time === p.time) : null;
    hoverTime = bar ? bar.time : null;
    renderOhlc(bar || candles[candles.length - 1]);
    renderLowerLegend(hoverTime);
    if (pointerOn === "main" && show.lower !== "none") {
      const [series, key] = show.lower === "macd" ? [macdSeries, "macd"] : [rsiSeries, "rsi"];
      const v = bar ? indAt(key, bar.time) : null;
      if (v != null) rsiChart.setCrosshairPosition(v, bar.time, series); else rsiChart.clearCrosshairPosition();
    }
  });
  rsiChart.subscribeCrosshairMove((p) => {
    if (pointerOn !== "rsi") return;
    const bar = p && p.time != null ? candles.find((c) => c.time === p.time) : null;
    hoverTime = bar ? bar.time : null;
    renderOhlc(bar || candles[candles.length - 1]);
    renderLowerLegend(hoverTime);
    if (bar) chart.setCrosshairPosition(bar.close, bar.time, candleSeries); else chart.clearCrosshairPosition();
  });

  buildIndMenu();
  applyShow();

  // Hell/Dunkel folgt dem System
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const n = themeOptions();
    for (const c of [chart, rsiChart]) c.applyOptions(n.chart);
    candleSeries.applyOptions(n.candles);
    for (const [k, def] of Object.entries(MAIN_LINES)) mainSeries[k].applyOptions(def.opts(n));
    rsiSeries.applyOptions(n.rsi);
    rsiLines.forEach((line, i) => line.applyOptions({ color: n.rsiLevel[RSI_LEVELS[i]] }));
    macdSeries.applyOptions({ color: n.macd });
    macdSignalSeries.applyOptions({ color: n.macdSignal });
    macdZero.applyOptions({ color: n.level });
    volumeSeries.setData(volumeData(n));
    if (candles.length) macdHistSeries.setData(macdHistData(n));
    updateLevels();
  });
}

// --- Menü „Indikatoren“ ---

const IND_ITEMS = [
  { key: "ma50", sw: "ma50", label: "MA50",
    tip: L("Gleitender Durchschnitt über die letzten 50 Kerzen.", "Moving average over the last 50 candles.") },
  { key: "ma200", sw: "ma200", label: "MA200",
    tip: L("Gleitender Durchschnitt über die letzten 200 Kerzen – bei Tageskerzen die übliche Trennlinie zwischen Auf- und Abwärtstrend.",
      "Moving average over the last 200 candles – on daily candles the usual dividing line between uptrend and downtrend.") },
  { key: "bb", sw: "bb", label: L("Bollinger-Bänder (20, 2)", "Bollinger Bands (20, 2)"),
    tip: L("Durchschnitt der letzten 20 Kerzen ± 2 Standardabweichungen. Kurs am oberen Band = kurzfristig überdehnt, am unteren = überverkauft. Sehr enge Bänder („Squeeze“) gehen oft einer großen Bewegung voraus.",
      "Average of the last 20 candles ± 2 standard deviations. Price at the upper band = stretched short term, at the lower band = oversold. Very narrow bands (“squeeze”) often precede a big move.") },
  { key: "band", sw: "band", label: "Bull Market Support Band",
    tip: L("20-Wochen-Durchschnitt (durchgezogen) und 21-Wochen-EMA (gestrichelt). In Bullenmärkten hält der Kurs meist darüber; ein Bruch nach unten war oft ein Wendepunkt. Braucht 20 Wochen Daten, daher nur bei Tages- und 4-h-Kerzen.",
      "20-week average (solid) and 21-week EMA (dashed). In bull markets the price usually holds above it; a break below has often been a turning point. Needs 20 weeks of data, so only on daily and 4 h candles.") },
  { key: "levels", sw: "levels", label: L("Kursmarken", "Price levels"),
    tip: L("Allzeithoch (CoinGecko), Hoch und Tief der letzten 52 Wochen (gestrichelt) sowie Eröffnungskurs des laufenden Monats und der Woche (gepunktet, UTC).",
      "All-time high (CoinGecko), high and low of the last 52 weeks (dashed) and the opening price of the current month and week (dotted, UTC).") },
];
const LOWER_ITEMS = [
  { value: "rsi", label: `RSI (${RSI_PERIOD})`,
    tip: L("Relative Stärke der letzten 14 Kerzen: über 70 überkauft, unter 30 überverkauft.", "Relative strength of the last 14 candles: above 70 overbought, below 30 oversold.") },
  { value: "macd", label: `MACD (${MACD_FAST}, ${MACD_SLOW}, ${MACD_SIGNAL})`,
    tip: L("Differenz zwischen 12- und 26-Kerzen-EMA (Linie) und deren 9-Kerzen-EMA (Signal, orange). Das Histogramm zeigt den Abstand beider: wechselt es das Vorzeichen, dreht das Momentum.",
      "Difference between the 12- and 26-candle EMA (line) and its 9-candle EMA (signal, orange). The histogram shows the gap between them: when it changes sign, momentum turns.") },
  { value: "none", label: L("Keins", "None"), tip: L("Kein unteres Fenster, mehr Platz für den Kurs.", "No lower pane, more room for the price.") },
];

function buildIndMenu() {
  const menu = $("ind-menu");
  menu.innerHTML =
    `<summary title="${L("Indikatoren ein- oder ausblenden", "Show or hide indicators")}">${L("Indikatoren", "Indicators")}<span class="ind-count" id="ind-count"></span></summary>`
    + `<div class="ind-pop">`
    + `<div class="label">${L("Im Kurs-Chart", "On the price chart")}</div>`
    + IND_ITEMS.map((x) => `<label title="${esc(x.tip)}"><input type="checkbox" data-show="${x.key}"><span class="swatch ${x.sw}"></span>${esc(x.label)}</label>`).join("")
    + `<div class="label">${L("Unteres Fenster", "Lower pane")}</div>`
    + LOWER_ITEMS.map((x) => `<label title="${esc(x.tip)}"><input type="radio" name="lower" value="${x.value}">${esc(x.label)}</label>`).join("")
    + `</div>`;
  menu.addEventListener("change", (e) => {
    const el = e.target;
    if (el.name === "lower") show.lower = el.value;
    else if (el.dataset.show) show[el.dataset.show] = el.checked;
    try { localStorage.setItem(SHOW_KEY, JSON.stringify(show)); } catch (err) { /* ignorieren */ }
    applyShow();
  });
  // Schließen bei Klick daneben oder Escape
  document.addEventListener("click", (e) => { if (menu.open && !menu.contains(e.target)) menu.open = false; });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") menu.open = false; });
}

// Schalter-Zustand auf Menü, Charts und Layout übertragen
function applyShow() {
  const menu = $("ind-menu");
  menu.querySelectorAll("input[data-show]").forEach((el) => { el.checked = !!show[el.dataset.show]; });
  menu.querySelectorAll("input[name=lower]").forEach((el) => { el.checked = el.value === show.lower; });
  const active = IND_ITEMS.filter((x) => show[x.key]).length + (show.lower !== "none" ? 1 : 0);
  $("ind-count").textContent = active || "";

  for (const [k, def] of Object.entries(MAIN_LINES)) mainSeries[k].applyOptions({ visible: !!show[def.show] });
  const lower = show.lower !== "none";
  rsiSeries.applyOptions({ visible: show.lower === "rsi" });
  for (const s of [macdSeries, macdSignalSeries, macdHistSeries]) s.applyOptions({ visible: show.lower === "macd" });
  rsiChart.applyOptions({ localization: { priceFormatter: show.lower === "macd" ? fmtMacd : (v) => v.toFixed(0) } });
  $("rsi-wrap").hidden = !lower;
  document.querySelector(".chart-card").classList.toggle("with-rsi", lower);
  // Zeitachse nur einmal, ganz unten
  chart.timeScale().applyOptions({ visible: !lower });
  if (lower) {
    const r = chart.timeScale().getVisibleLogicalRange();
    if (r) rsiChart.timeScale().setVisibleLogicalRange(r);
  }
  updateLevels();
  renderOhlc(candles[candles.length - 1]);
  renderLowerLegend(null);
}

function renderOhlc(c) {
  if (!c) { $("ohlc").innerHTML = ""; return; }
  const chg = (c.close - c.open) / c.open;
  const cls = chg >= 0 ? "up" : "down";
  const val = (key) => { const v = indAt(key, c.time); return v != null ? fmtPrice(v) : "–"; };
  const parts = [];
  for (const p of MA_PERIODS) if (show["ma" + p]) parts.push(`<span class="${p === 50 ? "ma50" : "ma"}">MA${p} <b>${val("ma" + p)}</b></span>`);
  if (show.bb) parts.push(`<span class="bb">BB <b>${val("bbUp")}</b> / <b>${val("bbLow")}</b></span>`);
  if (show.band) {
    parts.push(bandPeriods()[1] > candles.length
      ? `<span class="band">${L("Bull-Band: zu wenig Kerzen (braucht 20 Wochen)", "Bull band: not enough candles (needs 20 weeks)")}</span>`
      : `<span class="band">20W <b>${val("bandSma")}</b> 21W <b>${val("bandEma")}</b></span>`);
  }
  $("ohlc").innerHTML = `
    <span>O <b>${fmtPrice(c.open)}</b></span>
    <span>H <b>${fmtPrice(c.high)}</b></span>
    <span>L <b>${fmtPrice(c.low)}</b></span>
    <span>C <b>${fmtPrice(c.close)}</b></span>
    <span class="${cls}">${chg >= 0 ? "+" : "−"}${Math.abs(chg * 100).toFixed(2).replace(".", DEC)}${PCT}</span>
    <span>Vol <b>$${compact(c.volume)}</b></span>
    ${parts.join("")}`;
}

function renderLowerLegend(time) {
  const el = $("rsi-legend");
  if (!candles.length || show.lower === "none") { el.innerHTML = ""; return; }
  const t = time ?? candles[candles.length - 1].time;
  if (show.lower === "macd") {
    const m = indAt("macd", t), s = indAt("macdSignal", t), h = indAt("macdHist", t);
    const f = (v) => (v != null ? fmtMacd(v) : "–");
    el.innerHTML = `<span>MACD (${MACD_FAST}, ${MACD_SLOW}, ${MACD_SIGNAL}) <b>${f(m)}</b></span>`
      + `<span class="sig">Signal <b>${f(s)}</b></span>`
      + `<span class="${h == null ? "" : h >= 0 ? "up" : "down"}">${L("Histogramm", "Histogram")} <b>${f(h)}</b></span>`;
    return;
  }
  const v = indAt("rsi", t);
  const cls = v == null ? "" : v >= RSI_LEVELS[0] ? "hot" : v <= RSI_LEVELS[1] ? "cold" : "";
  const note = v == null ? "" : v >= RSI_LEVELS[0] ? L(" überkauft", " overbought") : v <= RSI_LEVELS[1] ? L(" überverkauft", " oversold") : "";
  el.innerHTML = `<span class="${cls}">RSI (${RSI_PERIOD}) <b>${v != null ? v.toFixed(1).replace(".", DEC) : "–"}</b>${note}</span>`
    + `<span>${L("Linien bei", "Lines at")} ${RSI_LEVELS.join(" / ")}</span>`;
}

function setPrecision(price) {
  // genug Nachkommastellen auch für Coins unter 1 $
  precision = price >= 1 ? 2 : Math.min(10, Math.max(4, Math.ceil(-Math.log10(price)) + 3));
  candleSeries.applyOptions({ priceFormat: { type: "price", precision, minMove: 10 ** -precision } });
}

function chartMessage(text, isError = false) {
  const el = $("chart-msg");
  el.textContent = text || "";
  el.hidden = !text;
  el.classList.toggle("error", isError);
}

async function fetchKlines(iv) {
  const res = await fetch(`api/klines?symbol=${encodeURIComponent(symbol)}&interval=${iv}`);
  const p = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(p.error || "HTTP " + res.status), { status: res.status });
  return p;  // { source, candles }
}

// Zeitraum-Buttons: sichtbaren Bereich auf die letzten N Tage setzen
function setRange(range) {
  if (!candles.length) return;
  const n = candles.length;
  let from = 0;
  if (range !== "all") {
    const start = candles[n - 1].time - Number(range) * 86400;
    from = Math.max(0, candles.findIndex((c) => c.time > start) - 0.5);
  }
  chart.timeScale().setVisibleLogicalRange({ from, to: n - 1 + 5 });
}

// Zeiträume abschalten, die nicht zum Intervall passen: länger als die geladenen Kerzen
// (z. B. 1J bei 1h-Kerzen) oder so kurz, dass kaum Kerzen zu sehen wären (z. B. 6h bei Tageskerzen)
const RANGE_MIN_BARS = 6;
function updateRangeButtons() {
  const n = candles.length;
  const span = n ? candles[n - 1].time - candles[0].time : 0;
  const step = n > 1 ? candles[n - 1].time - candles[n - 2].time : 0;
  document.querySelectorAll("#range-seg button").forEach((b) => {
    const days = b.dataset.range;
    const secs = Number(days) * 86400;
    b.disabled = !n || (days !== "all" && (span < secs * 0.95 || secs < step * RANGE_MIN_BARS));
  });
}

async function loadChart() {
  const token = ++loadToken;
  const iv = interval;
  closeLive();
  document.querySelectorAll("#interval-seg button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.interval === iv)));
  $("pair").innerHTML = `${esc(symbol)}/USDT <span class="note">${INTERVAL_LABEL[iv]}</span>`;
  chartMessage(L("Lade Kursdaten …", "Loading price data …"));
  try {
    const { candles: data, source } = await fetchKlines(iv);
    if (token !== loadToken) return;
    $("pair").innerHTML = `${esc(symbol)}/USDT <span class="note">${INTERVAL_LABEL[iv]} · ${esc(source)}</span>`;
    candles = data;
    setPrecision(candles[candles.length - 1].close);
    candleSeries.setData(candles.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    volumeSeries.setData(volumeData());
    calcIndicators();
    setIndicatorData();
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, candles.length - VISIBLE_BARS), to: candles.length + 5 });
    updateRangeButtons();
    updateLevels();
    renderOhlc(candles[candles.length - 1]);
    renderLowerLegend(null);
    fillHeroPrice();
    chartMessage("");
    connectLive(source);
  } catch (e) {
    if (token !== loadToken) return;
    candles = [];
    candleSeries.setData([]);
    volumeSeries.setData([]);
    clearIndicators();
    updateRangeButtons();
    updateLevels();
    renderOhlc(null);
    renderLowerLegend(null);
    chartMessage(e.status === 404
      ? L(`Für ${symbol} gibt es kein USDT-Paar auf Binance (weder Spot noch Futures) – daher vorerst kein Chart.`,
          `There is no USDT pair for ${symbol} on Binance (neither spot nor futures) – so no chart for now.`)
      : L(`Kursdaten konnten nicht geladen werden (${e.message}).`, `Price data could not be loaded (${e.message}).`), e.status !== 404);
  }
}

// Neue Stände der letzten Kerze(n) übernehmen (vom Server oder live von Binance). Nur die
// betroffenen Punkte werden nachgezogen, damit Zoom und Scrollposition erhalten bleiben.
function applyCandles(list) {
  if (!candles.length) return;
  const t = themeOptions();
  const lastTime = candles[candles.length - 1].time;
  for (const c of list) {
    if (c.time < candles[candles.length - 1].time) continue;
    candleSeries.update({ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close });
    volumeSeries.update({ time: c.time, value: c.volume, color: c.close >= c.open ? t.volUp : t.volDown });
    if (c.time === candles[candles.length - 1].time) candles[candles.length - 1] = c; else candles.push(c);
  }
  calcIndicators();
  updateIndicators(lastTime);
  // Zeile über dem Chart: die Kerze unter dem Fadenkreuz stehen lassen, sonst die letzte
  const last = candles[candles.length - 1];
  renderOhlc((hoverTime != null && candles.find((c) => c.time === hoverTime)) || last);
  renderLowerLegend(hoverTime);
  fillHeroPrice(true);
}

// Rückfall ohne Live-Kurs: die letzten Kerzen alle 60 s vom Server holen
async function refreshChart() {
  if (!candles.length || document.hidden) return;
  if (Date.now() - liveLast < LIVE_STALE_MS) return;   // Live-Kurs läuft und ist aktueller als der Server-Cache
  setLive(false);
  const iv = interval, token = loadToken;
  try {
    const { candles: data } = await fetchKlines(iv);
    if (token !== loadToken) return;
    applyCandles(data);
  } catch (e) { /* beim nächsten Intervall erneut versuchen */ }
}

// Preis im Kopf aus der letzten Kerze: immer, wenn sie neuer ist als der Score (force),
// sonst nur für Coins ohne MA200-Daten (dort steht noch „–“)
function fillHeroPrice(force = false) {
  const el = $("hero-price");
  if (el && (force || el.textContent === "–") && candles.length) el.textContent = usd(candles[candles.length - 1].close);
}

// --- Live-Kurs -------------------------------------------------------------
// Die laufende Kerze kommt direkt von Binance in den Browser (WebSocket, ohne API-Key, der Server
// ist nicht beteiligt): Spot oder Futures, je nachdem, woher die Kerzen des Charts stammen.
// Bricht die Verbindung ab, springt refreshChart wieder ein, und es wird neu verbunden.

// Futures: Kursdaten laufen über /market/ws/ (das frühere /ws/ verbindet zwar, sendet aber nichts mehr)
const LIVE_URLS = { "Binance Spot": "wss://stream.binance.com:9443/ws/", "Binance Futures": "wss://fstream.binance.com/market/ws/" };
const LIVE_DRAW_MS = 500;      // höchstens so oft neu zeichnen (Futures senden mehrmals pro Sekunde)
const LIVE_RETRY_MS = 5000;    // Pause vor dem erneuten Verbinden
const LIVE_STALE_MS = 15000;   // so lange ohne Nachricht: gilt nicht mehr als live

let liveSocket = null, liveSource = null, livePending = null, liveLast = 0;
let liveDrawTimer = null, liveRetryTimer = null;

const setLive = (on) => document.querySelector(".chart-card").classList.toggle("is-live", on);

function closeLive() {
  clearTimeout(liveRetryTimer);
  clearTimeout(liveDrawTimer);
  liveDrawTimer = null;
  livePending = null;
  liveLast = 0;
  if (liveSocket) {
    const s = liveSocket;
    liveSocket = null;   // onmessage/onclose dieser Verbindung gelten ab jetzt nicht mehr
    s.close();
  }
  setLive(false);
}

function connectLive(source) {
  closeLive();
  liveSource = source;
  const base = LIVE_URLS[source];
  if (!base || !candles.length || document.hidden || typeof WebSocket === "undefined") return;
  const iv = interval;
  let s;
  try { s = new WebSocket(`${base}${symbol.toLowerCase()}usdt@kline_${iv}`); } catch (e) { return; }
  liveSocket = s;
  s.onmessage = (ev) => {
    if (s !== liveSocket) return;
    let k;
    try { k = JSON.parse(ev.data).k; } catch (e) { return; }
    if (!k || k.i !== iv) return;
    // q = Quote-Volumen in USDT, wie bei den Kerzen vom Server
    livePending = { time: Math.floor(k.t / 1000), open: +k.o, high: +k.h, low: +k.l, close: +k.c, volume: +k.q };
    liveLast = Date.now();
    setLive(true);
    if (!liveDrawTimer) liveDrawTimer = setTimeout(drawLive, LIVE_DRAW_MS);
  };
  s.onclose = () => {
    if (s !== liveSocket) return;
    liveSocket = null;
    liveLast = 0;
    setLive(false);
    liveRetryTimer = setTimeout(resumeLive, LIVE_RETRY_MS);
  };
}

function drawLive() {
  liveDrawTimer = null;
  const c = livePending;
  livePending = null;
  if (c) applyCandles([c]);
}

// Nach einer Unterbrechung erst die verpassten Kerzen vom Server holen, dann wieder verbinden
async function resumeLive() {
  const token = loadToken;
  await refreshChart();
  if (token === loadToken && !document.hidden) connectLive(liveSource);
}

// Im Hintergrund keine Verbindung halten; beim Zurückkehren aufholen und neu verbinden
document.addEventListener("visibilitychange", () => {
  if (document.hidden) closeLive();
  else if (candles.length) resumeLive();
});


$("range-seg").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-range]");
  if (btn && !btn.disabled) setRange(btn.dataset.range);
});

$("interval-seg").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-interval]");
  if (!btn || btn.dataset.interval === interval) return;
  interval = btn.dataset.interval;
  try { localStorage.setItem(INTERVAL_KEY, interval); } catch (err) { /* ignorieren */ }
  loadChart();
});

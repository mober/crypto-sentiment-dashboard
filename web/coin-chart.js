// Detailseite: Candlestick-Chart mit MA50/MA200 und RSI-Fenster, Live-Kurs per WebSocket von Binance.
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

// Ein-/Ausblenden von MA50, MA200 und RSI-Fenster, gemerkt im Browser
const SHOW_KEY = "chartShow";
const show = { ma50: true, ma200: true, rsi: true };
try {
  Object.assign(show, JSON.parse(localStorage.getItem(SHOW_KEY) || "{}"));
  if (localStorage.getItem("chartShowMa") === "0") show.ma200 = false;  // alte Einstellung übernehmen
} catch (e) { /* ignorieren */ }

let chart = null, candleSeries = null, volumeSeries = null;
let rsiChart = null, rsiSeries = null;
const maSeries = {};     // Periode → Linienserie
let candles = [];        // aktuelle Kerzen (Zeit in UTC-Sekunden)
const maByTime = {};     // Periode → Map(Kerzenzeit → MA-Wert), für die OHLC-Zeile
let rsiByTime = new Map();
let precision = 2;
let loadToken = 0;       // verwirft Antworten, wenn inzwischen ein anderes Intervall gewählt wurde
let pointerOn = null;    // "main" | "rsi": welches Chart Maus/Finger gerade bedient (für den Gleichlauf)
let hoverTime = null;    // Kerze unter dem Fadenkreuz (null = keine): die OHLC-Zeile zeigt sie statt der letzten

const fmtPrice = (v) => v.toLocaleString(LOCALE, { minimumFractionDigits: precision, maximumFractionDigits: precision });

// Einfacher gleitender Durchschnitt der Schlusskurse; die ersten period − 1 Kerzen haben keinen Wert
function maData(period) {
  const out = [];
  let sum = 0;
  candles.forEach((c, i) => {
    sum += c.close;
    if (i >= period) sum -= candles[i - period].close;
    if (i >= period - 1) out.push({ time: c.time, value: sum / period });
  });
  maByTime[period] = new Map(out.map((p) => [p.time, p.value]));
  return out;
}

// RSI nach Wilder wie rsi() in sentiment.py, aber für jede Kerze. Die ersten Kerzen bekommen
// Leerpunkte (nur time), damit beide Charts gleich viele Punkte haben und synchron scrollen.
function rsiData() {
  const out = [];
  let avgGain = 0, avgLoss = 0;
  candles.forEach((c, i) => {
    if (i === 0) { out.push({ time: c.time }); return; }
    const d = c.close - candles[i - 1].close;
    const g = Math.max(d, 0), l = Math.max(-d, 0);
    if (i <= RSI_PERIOD) {
      avgGain += g / RSI_PERIOD;
      avgLoss += l / RSI_PERIOD;
      if (i < RSI_PERIOD) { out.push({ time: c.time }); return; }
    } else {
      avgGain = (avgGain * (RSI_PERIOD - 1) + g) / RSI_PERIOD;
      avgLoss = (avgLoss * (RSI_PERIOD - 1) + l) / RSI_PERIOD;
    }
    out.push({ time: c.time, value: avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss) });
  });
  rsiByTime = new Map(out.filter((p) => p.value != null).map((p) => [p.time, p.value]));
  return out;
}

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
  for (const p of MA_PERIODS) {
    maSeries[p] = chart.addLineSeries({
      ...t.ma[p], lineWidth: p === 200 ? 2 : 1.5, priceLineVisible: false, lastValueVisible: true,
      crosshairMarkerVisible: false, title: "MA" + p, visible: show["ma" + p],
    });
  }

  // RSI in eigenem Chart darunter (Lightweight Charts 4 kennt keine Unterfenster)
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
  const rsiLines = RSI_LEVELS.map((level) => rsiSeries.createPriceLine({
    price: level, color: t.rsiLevel[level], lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: "",
  }));

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
    renderRsiLegend(hoverTime);
    if (pointerOn === "main" && show.rsi) {
      const v = bar ? rsiByTime.get(bar.time) : null;
      if (v != null) rsiChart.setCrosshairPosition(v, bar.time, rsiSeries); else rsiChart.clearCrosshairPosition();
    }
  });
  rsiChart.subscribeCrosshairMove((p) => {
    if (pointerOn !== "rsi") return;
    const bar = p && p.time != null ? candles.find((c) => c.time === p.time) : null;
    hoverTime = bar ? bar.time : null;
    renderOhlc(bar || candles[candles.length - 1]);
    renderRsiLegend(hoverTime);
    if (bar) chart.setCrosshairPosition(bar.close, bar.time, candleSeries); else chart.clearCrosshairPosition();
  });

  applyShow();

  // Hell/Dunkel folgt dem System
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const n = themeOptions();
    for (const c of [chart, rsiChart]) c.applyOptions(n.chart);
    candleSeries.applyOptions(n.candles);
    for (const p of MA_PERIODS) maSeries[p].applyOptions(n.ma[p]);
    rsiSeries.applyOptions(n.rsi);
    rsiLines.forEach((line, i) => line.applyOptions({ color: n.rsiLevel[RSI_LEVELS[i]] }));
    volumeSeries.setData(volumeData(n));
  });
}

// Schalter-Zustand auf Charts, Buttons und Layout übertragen
function applyShow() {
  document.querySelectorAll(".toggle[data-show]").forEach((b) => b.setAttribute("aria-pressed", String(show[b.dataset.show])));
  for (const p of MA_PERIODS) maSeries[p].applyOptions({ visible: show["ma" + p] });
  $("rsi-wrap").hidden = !show.rsi;
  document.querySelector(".chart-card").classList.toggle("with-rsi", show.rsi);
  // Zeitachse nur einmal, ganz unten
  chart.timeScale().applyOptions({ visible: !show.rsi });
  if (show.rsi) {
    const r = chart.timeScale().getVisibleLogicalRange();
    if (r) rsiChart.timeScale().setVisibleLogicalRange(r);
  }
  renderOhlc(candles[candles.length - 1]);
  renderRsiLegend(null);
}

const volumeData = (t = themeOptions()) =>
  candles.map((c) => ({ time: c.time, value: c.volume, color: c.close >= c.open ? t.volUp : t.volDown }));

function renderOhlc(c) {
  if (!c) { $("ohlc").innerHTML = ""; return; }
  const chg = (c.close - c.open) / c.open;
  const cls = chg >= 0 ? "up" : "down";
  const ma = (p) => {
    if (!show["ma" + p]) return "";
    const v = maByTime[p] && maByTime[p].get(c.time);
    return `<span class="${p === 50 ? "ma50" : "ma"}">MA${p} <b>${v != null ? fmtPrice(v) : "–"}</b></span>`;
  };
  $("ohlc").innerHTML = `
    <span>O <b>${fmtPrice(c.open)}</b></span>
    <span>H <b>${fmtPrice(c.high)}</b></span>
    <span>L <b>${fmtPrice(c.low)}</b></span>
    <span>C <b>${fmtPrice(c.close)}</b></span>
    <span class="${cls}">${chg >= 0 ? "+" : "−"}${Math.abs(chg * 100).toFixed(2).replace(".", DEC)}${PCT}</span>
    <span>Vol <b>$${compact(c.volume)}</b></span>
    ${MA_PERIODS.map(ma).join("")}`;
}

function renderRsiLegend(time) {
  const t = time ?? (candles.length ? candles[candles.length - 1].time : null);
  const v = t != null ? rsiByTime.get(t) : null;
  const cls = v == null ? "" : v >= RSI_LEVELS[0] ? "hot" : v <= RSI_LEVELS[1] ? "cold" : "";
  const note = v == null ? "" : v >= RSI_LEVELS[0] ? L(" überkauft", " overbought") : v <= RSI_LEVELS[1] ? L(" überverkauft", " oversold") : "";
  $("rsi-legend").innerHTML = candles.length
    ? `<span class="${cls}">RSI (${RSI_PERIOD}) <b>${v != null ? v.toFixed(1).replace(".", DEC) : "–"}</b>${note}</span>`
      + `<span>${L("Linien bei", "Lines at")} ${RSI_LEVELS.join(" / ")}</span>`
    : "";
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
    for (const p of MA_PERIODS) maSeries[p].setData(maData(p));
    rsiSeries.setData(rsiData());
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, candles.length - VISIBLE_BARS), to: candles.length + 5 });
    updateRangeButtons();
    renderOhlc(candles[candles.length - 1]);
    renderRsiLegend(null);
    fillHeroPrice();
    chartMessage("");
    connectLive(source);
  } catch (e) {
    if (token !== loadToken) return;
    candles = [];
    candleSeries.setData([]);
    volumeSeries.setData([]);
    for (const p of MA_PERIODS) { maSeries[p].setData([]); maByTime[p] = new Map(); }
    rsiSeries.setData([]);
    rsiByTime = new Map();
    updateRangeButtons();
    renderOhlc(null);
    renderRsiLegend(null);
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
  // Indikatoren neu berechnen und nur die betroffenen Punkte nachziehen
  for (const p of MA_PERIODS) for (const pt of maData(p).filter((pt) => pt.time >= lastTime)) maSeries[p].update(pt);
  for (const pt of rsiData().filter((pt) => pt.time >= lastTime)) rsiSeries.update(pt);
  // Zeile über dem Chart: die Kerze unter dem Fadenkreuz stehen lassen, sonst die letzte
  const last = candles[candles.length - 1];
  renderOhlc((hoverTime != null && candles.find((c) => c.time === hoverTime)) || last);
  renderRsiLegend(hoverTime);
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

document.querySelectorAll(".toggle[data-show]").forEach((btn) => btn.addEventListener("click", () => {
  const key = btn.dataset.show;
  show[key] = !show[key];
  try { localStorage.setItem(SHOW_KEY, JSON.stringify(show)); } catch (err) { /* ignorieren */ }
  if (chart) applyShow();
}));

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

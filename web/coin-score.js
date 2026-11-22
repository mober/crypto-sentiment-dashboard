// Detailseite: Score-Verlauf (1 Jahr, rückwirkend berechnet).
// Gemeinsame Helfer in coin-base.js.

// --- Score-Verlauf ---------------------------------------------------------

const SHORT_NAMES = {
  "Fear & Greed": "F&G", "Funding Rate": "Funding", "Open Interest": "OI", "Preis vs. MA200": "MA200",
  "RSI (14)": "RSI", "Optionsvola (DVOL)": "DVOL", "Long/Short Ratio": "L/S",
};
const SCORE_LEVELS = [40, 15, -15, -40];  // wie VERDICT_HOT / VERDICT_WARM in sentiment.py
const scoreTone = (v) => (v >= 15 ? "hot" : v <= -15 ? "cold" : "");

let histChart = null, histSeries = null, histPrice = null, histData = [];

function histColors() {
  const hot = css("--hot"), cold = css("--cold");
  return {
    series: { topLineColor: hot, topFillColor1: alpha(hot, 0.28), topFillColor2: alpha(hot, 0.04),
      bottomLineColor: cold, bottomFillColor1: alpha(cold, 0.04), bottomFillColor2: alpha(cold, 0.28) },
    level: { 40: hot, 15: alpha(hot, 0.5), [-15]: alpha(cold, 0.5), [-40]: cold },
    price: css("--muted"),
  };
}

function initHistChart() {
  const c = histColors();
  histChart = miniChart($("hist-chart"));
  histChart.applyOptions({ leftPriceScale: { visible: true, borderColor: css("--border") },
    handleScroll: true, handleScale: true });
  histSeries = histChart.addBaselineSeries({
    ...c.series, baseValue: { type: "price", price: 0 }, lineWidth: 2, priceLineVisible: false,
    priceFormat: { type: "custom", formatter: (v) => signed(Math.round(v)), minMove: 1 },
    // mindestens ±50 zeigen, damit kleine Ausschläge nicht übertrieben wirken
    autoscaleInfoProvider: (orig) => {
      const r = orig();
      if (!r) return r;
      return { ...r, priceRange: { minValue: Math.max(-100, Math.min(-50, r.priceRange.minValue)),
        maxValue: Math.min(100, Math.max(50, r.priceRange.maxValue)) } };
    },
  });
  const lines = SCORE_LEVELS.map((lv) => histSeries.createPriceLine({
    price: lv, color: c.level[lv], lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: false, title: "",
  }));
  histPrice = histChart.addLineSeries({ priceScaleId: "left", color: c.price, lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dotted, priceLineVisible: false, crosshairMarkerVisible: false, lastValueVisible: false,
    priceFormat: { type: "custom", formatter: (v) => usd(v), minMove: 1e-8 } });

  histChart.subscribeCrosshairMove((p) => renderHistDetail(p && p.time != null ? p.time : null));

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const n = histColors();
    histChart.applyOptions({ ...themeOptions().chart, leftPriceScale: { borderColor: css("--border") } });
    histSeries.applyOptions(n.series);
    lines.forEach((l, i) => l.applyOptions({ color: n.level[SCORE_LEVELS[i]] }));
    histPrice.applyOptions({ color: n.price });
  });
}

function renderHistDetail(time) {
  if (!histData.length) { $("hist-detail").innerHTML = ""; $("hist-legend").innerHTML = ""; return; }
  const d = (time != null && histData.find((x) => x.time === time)) || histData[histData.length - 1];
  const date = fmtDate(d.time, { weekday: "short", day: "2-digit", month: "2-digit", year: "numeric", timeZone: "UTC" });
  const isToday = d === histData[histData.length - 1];
  $("hist-legend").innerHTML = `<span>${isToday ? L("Heute", "Today") : esc(date)}</span>`
    + `<span class="${scoreTone(d.score)}">Score <b>${signed(d.score)}</b></span>`
    + (d.price != null ? `<span>${L("Kurs", "Price")} <b>${usd(d.price)}</b></span>` : "");
  $("hist-detail").innerHTML = `<span>${d.signals.length} ${L("Signale", "signals")}:</span>` + d.signals.map((x) =>
    `<span class="${scoreTone(x.points)}">${esc(SHORT_NAMES[x.name] || x.name)} <b>${signed(x.points)}</b></span>`).join("");
}

// Markierung, wo Signale dazukommen oder wegfallen (z. B. OI und L/S nur 30 Tage)
function histMarkers() {
  const markers = [];
  const muted = css("--muted");
  for (let i = 1; i < histData.length; i++) {
    const before = new Set(histData[i - 1].signals.map((x) => x.name));
    const now = new Set(histData[i].signals.map((x) => x.name));
    const added = [...now].filter((n) => !before.has(n)).map((n) => "+" + (SHORT_NAMES[n] || n));
    const removed = [...before].filter((n) => !now.has(n)).map((n) => "−" + (SHORT_NAMES[n] || n));
    if (added.length || removed.length) {
      markers.push({ time: histData[i].time, position: "aboveBar", shape: "arrowDown", color: muted, text: [...added, ...removed].join(" ") });
    }
  }
  return markers;
}

// Trefferquote: Wie entwickelte sich der Kurs bisher nach Tagen in der aktuellen Score-Zone?
let trackRecord = null;
const ZONE_NAMES_EN = { cold2: "extreme fear (to −40)", cold1: "fear (−40 to −15)", neutral: "neutral (−15 to +15)",
  hot1: "greed (+15 to +40)", hot2: "overheated (from +40)" };   // deutsch kommen sie vom Server (SCORE_ZONES)
const ZONE_OF = (v) => v <= -40 ? "cold2" : v <= -15 ? "cold1" : v < 15 ? "neutral" : v < 40 ? "hot1" : "hot2";  // wie score_zone in sentiment.py
function renderTrack() {
  const el = $("score-track");
  if (!el || !trackRecord || !statsCoin || !statsCoin.signals.length) return;
  const z = trackRecord.zones[ZONE_OF(statsCoin.score)], all = trackRecord.all;
  const q = z && z["90d"], q30 = z && z["30d"];
  if (!q || q.n < 20 || !all["90d"]) { el.innerHTML = ""; return; }
  const pc = (v) => Math.round(v * 100) + PCT;
  const cls = (v, base) => v > base + 0.1 ? "up" : v < base - 0.1 ? "down" : "";
  const since = new Date(trackRecord.since * 1000).toLocaleDateString(LOCALE, { month: "2-digit", year: "numeric" });
  const zone = (EN && ZONE_NAMES_EN[ZONE_OF(statsCoin.score)]) || z.name;
  el.innerHTML = EN
    ? `<span data-tip="trackRecord" tabindex="0">So far for ${esc(symbol)}</span> after “${esc(zone)}”:
    90 days later higher in <b class="${cls(q.up, all["90d"].up)}">${pc(q.up)}</b> of cases (median ${pctSignedDe(q.median)}),
    on average across all days ${pc(all["90d"].up)}.${q30 ? ` After 30 days: ${pc(q30.up)} (all ${pc(all["30d"].up)}).` : ""}
    <span class="muted">${q.n} days since ${since}, in hindsight – not a forecast.</span>`
    : `<span data-tip="trackRecord" tabindex="0">Bisher bei ${esc(symbol)}</span> nach „${esc(zone)}“:
    90 Tage später in <b class="${cls(q.up, all["90d"].up)}">${pc(q.up)}</b> der Fälle höher (Median ${pctSignedDe(q.median)}),
    im Schnitt aller Tage ${pc(all["90d"].up)}.${q30 ? ` Nach 30 Tagen: ${pc(q30.up)} (alle ${pc(all["30d"].up)}).` : ""}
    <span class="muted">${q.n} Tage seit ${since}, rückblickend – keine Prognose.</span>`;
}
const pctSignedDe = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v * 100).toLocaleString(LOCALE, { maximumFractionDigits: 1 }) + PCT;

async function loadHistory() {
  try {
    const res = await fetch("api/score-history?symbol=" + encodeURIComponent(symbol));
    const p = await res.json().catch(() => ({}));
    if (res.status === 404) {
      $("history").innerHTML = `<div class="status">${L(`Für ${esc(symbol)} gibt es keinen Score-Verlauf (keine Kurs- oder Futures-Daten von Binance).`, `There is no score history for ${esc(symbol)} (no price or futures data from Binance).`)}</div>`;
      return;
    }
    if (!res.ok) throw new Error(p.error || "HTTP " + res.status);
    histData = p.days;
    trackRecord = p.track_record || null;
    renderTrack();
    histSeries.setData(histData.map((d) => ({ time: d.time, value: d.score })));
    histSeries.setMarkers(histMarkers());
    histPrice.setData(histData.filter((d) => d.price != null).map((d) => ({ time: d.time, value: d.price })));
    histChart.timeScale().fitContent();
    renderHistDetail(null);
    $("hist-msg").hidden = true;
  } catch (e) {
    $("hist-msg").textContent = L(`Score-Verlauf nicht verfügbar (${e.message}).`, `Score history not available (${e.message}).`);
  }
}

// Detailseite: Liquidations-Heatmap – ein Modell aus OI-Zuwachs × angenommenen Hebeln, keine echten Aufträge.
// Gemeinsame Helfer in coin-base.js.

// --- Liquidations-Heatmap (Modell) -----------------------------------------
// Steigt das Open Interest in einer Stunde, gelten die neuen Positionen als zum Durchschnittskurs
// der Stunde eröffnet, je zur Hälfte long und short, verteilt auf typische Hebel. Daraus ergeben
// sich die Liquidationskurse. Erreicht der Kurs später ein Niveau, ist es abgeräumt; fällt das OI,
// schrumpfen alle offenen Niveaus anteilig. Positionen von vor dem Zeitraum fehlen.
// Alle Annahmen sind Erfahrungswerte, wie sie auch gängige Heatmaps verwenden.

const LIQ_TIERS = [[5, 0.10], [10, 0.25], [25, 0.30], [50, 0.20], [100, 0.15]];  // [Hebel, Anteil am neuen OI]
const LIQ_MMR = 0.005;   // Wartungsmarge ≈ 0,5 %: Liquidation etwas vor dem rechnerischen Totalverlust
const LIQ_BINS = 140;    // Preisstufen über den gesamten Bereich
const LIQ_PCTL = 0.98;   // dieses Perzentil aller Zellen = volle Farbe (sonst dominiert ein Ausreißer)

let liqChart = null, liqSeries = null, liqModel = null, liqCandles = [], liqDrawPending = false;

function buildLiqModel(candles, oi) {
  const oiAt = new Map(oi.map((o) => [o.time, o.usd]));
  let lo = Infinity, hi = -Infinity;
  for (const c of candles) { lo = Math.min(lo, c.low); hi = Math.max(hi, c.high); }
  lo *= 1 - 1 / LIQ_TIERS[0][0] - 0.03;   // Platz für die Niveaus mit dem kleinsten Hebel
  hi *= 1 + 1 / LIQ_TIERS[0][0] + 0.03;
  const step = (hi - lo) / LIQ_BINS;
  let levels = [];
  const cols = [];
  for (const c of candles) {
    // erst abräumen, was diese Stunde erreicht hat (Longs am Tief, Shorts am Hoch) …
    levels = levels.filter((l) => (l.long ? l.price < c.low : l.price > c.high));
    // … dann die Positionsänderung dieser Stunde verbuchen (OI-Stand am Ende minus am Anfang)
    const o0 = oiAt.get(c.time), o1 = oiAt.get(c.time + 3600);
    if (o0 != null && o1 != null) {
      const d = o1 - o0;
      if (d > 0) {
        const p = (c.high + c.low + c.close) / 3;
        for (const [lev, w] of LIQ_TIERS) {
          levels.push({ price: p * (1 - 1 / lev + LIQ_MMR), long: true, vol: d * w / 2 });
          levels.push({ price: p * (1 + 1 / lev - LIQ_MMR), long: false, vol: d * w / 2 });
        }
      } else if (d < 0 && o0 > 0) {
        const f = Math.max(0, 1 + d / o0);
        for (const l of levels) l.vol *= f;
      }
    }
    const long = new Float64Array(LIQ_BINS), short = new Float64Array(LIQ_BINS);
    for (const l of levels) {
      const b = Math.floor((l.price - lo) / step);
      if (b >= 0 && b < LIQ_BINS) (l.long ? long : short)[b] += l.vol;
    }
    cols.push({ time: c.time, long, short });
  }
  const vals = [];
  for (const col of cols) for (let b = 0; b < LIQ_BINS; b++) { const v = col.long[b] + col.short[b]; if (v > 0) vals.push(v); }
  vals.sort((a, b) => a - b);
  const scaleMax = vals.length ? vals[Math.floor((vals.length - 1) * LIQ_PCTL)] : 1;
  return { lo, step, cols, colAt: new Map(cols.map((c, i) => [c.time, i])), scaleMax };
}

function initLiqChart() {
  const t = themeOptions();
  liqChart = LightweightCharts.createChart($("liq-chart"), {
    ...t.chart,
    layout: { ...t.chart.layout, background: { type: "solid", color: "transparent" }, attributionLogo: false },
    grid: { vertLines: { visible: false }, horzLines: { color: alpha(css("--muted"), 0.12) } },
    autoSize: true,
    crosshair: { ...t.chart.crosshair, mode: LightweightCharts.CrosshairMode.Normal },
    rightPriceScale: { ...t.chart.rightPriceScale, minimumWidth: PRICE_SCALE_WIDTH },
    timeScale: { ...t.chart.timeScale, timeVisible: true, secondsVisible: false, rightOffset: 3, tickMarkFormatter: tickLabel },
    localization: { locale: "de-DE", priceFormatter: fmtPrice, timeFormatter: (ts) => fmtDate(ts, {
      weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) },
    // Preisachse nicht von Hand ziehen: die Heatmap folgt nur automatischen Skalierungen
    handleScale: { axisPressedMouseMove: { time: true, price: false } },
  });
  liqSeries = liqChart.addCandlestickSeries({ ...t.candles, priceLineVisible: true,
    // etwas Luft über und unter den Kerzen, damit nahe Cluster sichtbar sind
    autoscaleInfoProvider: (orig) => {
      const r = orig();
      if (!r) return r;
      const pad = (r.priceRange.maxValue - r.priceRange.minValue) * 0.25;
      return { ...r, priceRange: { minValue: r.priceRange.minValue - pad, maxValue: r.priceRange.maxValue + pad } };
    },
  });
  const redraw = () => scheduleLiqDraw();
  liqChart.timeScale().subscribeVisibleLogicalRangeChange(redraw);
  new ResizeObserver(redraw).observe($("liq-chart"));
  liqChart.subscribeCrosshairMove((p) => renderLiqLegend(p));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const n = themeOptions();
    liqChart.applyOptions({ ...n.chart, layout: { ...n.chart.layout, background: { type: "solid", color: "transparent" } },
      grid: { vertLines: { visible: false }, horzLines: { color: alpha(css("--muted"), 0.12) } } });
    liqSeries.applyOptions(n.candles);
    scheduleLiqDraw();
  });
}

// Zeichnen erst nach dem Neuzeichnen des Charts (zwei Frames), damit die Preisachse schon neu skaliert ist
function scheduleLiqDraw() {
  if (liqDrawPending) return;
  liqDrawPending = true;
  requestAnimationFrame(() => requestAnimationFrame(() => { liqDrawPending = false; drawLiq(); }));
}

function drawLiq() {
  const cv = $("liq-canvas"), box = $("liq-chart");
  const dpr = window.devicePixelRatio || 1;
  const w = box.clientWidth, h = box.clientHeight;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    cv.style.width = w + "px"; cv.style.height = h + "px";
  }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!liqModel) return;
  const ts = liqChart.timeScale();
  const paneW = ts.width(), paneH = h - ts.height();
  ctx.save();
  ctx.beginPath(); ctx.rect(0, 0, paneW, paneH); ctx.clip();
  const spacing = ts.options().barSpacing;
  const ys = [];
  for (let b = 0; b <= LIQ_BINS; b++) ys.push(liqSeries.priceToCoordinate(liqModel.lo + b * liqModel.step));
  const n = parseInt(css("--hot").slice(1), 16);
  const rgb = `${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}`;
  for (const col of liqModel.cols) {
    const x = ts.timeToCoordinate(col.time);
    if (x == null || x < -spacing || x > paneW + spacing) continue;
    for (let b = 0; b < LIQ_BINS; b++) {
      const v = col.long[b] + col.short[b];
      if (!v || ys[b] == null || ys[b + 1] == null) continue;
      const top = ys[b + 1], bottom = ys[b];
      if (bottom < 0 || top > paneH) continue;
      // Wurzel-Skala: auch mittlere Cluster bleiben sichtbar
      const a = Math.min(1, Math.sqrt(v / liqModel.scaleMax)) * 0.9;
      if (a < 0.03) continue;
      ctx.fillStyle = `rgba(${rgb}, ${a.toFixed(3)})`;
      ctx.fillRect(x - spacing / 2, top, spacing + 0.6, bottom - top + 0.6);
    }
  }
  ctx.restore();
}

const usdShort = (v) => "$" + compact(v);

function renderLiqLegend(p) {
  if (!liqModel || !liqCandles.length) { $("liq-legend").innerHTML = ""; return; }
  const last = liqCandles[liqCandles.length - 1];
  if (!p || p.time == null || !p.point) {
    $("liq-legend").innerHTML = `<span>Kurs <b>${usd(last.close)}</b></span><span>Maus über die Karte: geschätztes Volumen je Niveau</span>`;
    return;
  }
  const i = liqModel.colAt.get(p.time);
  const price = liqSeries.coordinateToPrice(p.point.y);
  if (i == null || price == null) return;
  const b = Math.floor((price - liqModel.lo) / liqModel.step);
  const col = liqModel.cols[i];
  const lv = b >= 0 && b < LIQ_BINS ? col.long[b] : 0, sv = b >= 0 && b < LIQ_BINS ? col.short[b] : 0;
  const what = lv + sv > 0
    ? [lv > 0 ? `Longs ≈ <b>${usdShort(lv)}</b>` : "", sv > 0 ? `Shorts ≈ <b>${usdShort(sv)}</b>` : ""].filter(Boolean).join(" · ")
    : "kaum geschätzte Liquidationen";
  $("liq-legend").innerHTML = `<span>Niveau <b>${usd(price)}</b></span><span>${what}</span>`;
}

// Größte noch offene Cluster über und unter dem aktuellen Kurs (letzte Spalte)
function renderLiqClusters() {
  if (!liqModel || !liqCandles.length) { $("liq-clusters").innerHTML = ""; return; }
  const col = liqModel.cols[liqModel.cols.length - 1];
  const price = liqCandles[liqCandles.length - 1].close;
  const mid = (b) => liqModel.lo + (b + 0.5) * liqModel.step;
  const top = (arr, above) => [...arr].map((v, b) => ({ v, b })).filter((x) => x.v > 0 && (above ? mid(x.b) > price : mid(x.b) < price))
    .sort((a, b) => b.v - a.v).slice(0, 3).sort((a, b) => (above ? a.b - b.b : b.b - a.b));
  const fmt = (list) => list.length
    ? list.map((x) => `<b>${usd(mid(x.b))}</b> (${num1((mid(x.b) / price - 1) * 100).replace(/,0$/, "")} %, ≈ ${usdShort(x.v)})`).join(" · ")
    : "keine nennenswerten";
  $("liq-clusters").innerHTML = `
    <span class="side">Darüber (Shorts):</span><span>${fmt(top(col.short, true))}</span>
    <span class="side">Darunter (Longs):</span><span>${fmt(top(col.long, false))}</span>`;
}

async function loadLiquidations() {
  try {
    const res = await fetch("api/liquidations?symbol=" + encodeURIComponent(symbol));
    const p = await res.json().catch(() => ({}));
    if (res.status === 404) {
      $("liq").innerHTML = `<div class="status">Für ${esc(symbol)} gibt es keine Binance-Futures – daher keine Liquidations-Heatmap.</div>`;
      return;
    }
    if (!res.ok) throw new Error(p.error || "HTTP " + res.status);
    liqCandles = p.candles;
    liqModel = buildLiqModel(p.candles, p.open_interest);
    const last = liqCandles[liqCandles.length - 1].close;
    const prec = last >= 1 ? 2 : Math.min(10, Math.max(4, Math.ceil(-Math.log10(last)) + 3));
    liqSeries.applyOptions({ priceFormat: { type: "price", precision: prec, minMove: 10 ** -prec } });
    liqSeries.setData(liqCandles);
    liqChart.timeScale().fitContent();
    $("liq-msg").hidden = true;
    renderLiqLegend(null);
    renderLiqClusters();
    scheduleLiqDraw();
  } catch (e) {
    $("liq-msg").textContent = `Daten nicht verfügbar (${e.message}).`;
  }
}

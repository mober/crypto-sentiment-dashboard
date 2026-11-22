// Detailseite: Funding Rate und Open Interest (Binance Futures).
// Gemeinsame Helfer in coin-base.js.

// --- Derivate: Funding Rate und Open Interest ------------------------------

const FUNDING_AVG_DAYS = 7;   // wie avg_7d in get_funding (sentiment.py)
const OI_CHANGE_DAYS = 7;     // wie change_7d in get_open_interest
const pct4 = (v) => pct(v, 4);
// Schwellen wie FUNDING_SCALE: ab 0,05 % viel Long-Hebel, unter −0,01 % Shorts dominieren
const fundTone = (r) => (r >= 0.0005 ? "hot" : r <= -0.0001 ? "cold" : "");

let fundChart = null, fundBars = null, fundAvg = null, fundData = [], fundAvgByTime = new Map();
let oiChart = null, oiSeries = null, oiPrice = null, oiData = [];

// Achsenformat kommt aus priceFormat der Serien (OI-Chart hat zwei Achsen mit verschiedenen Einheiten)

function derivColors() {
  return { hot: css("--hot"), cold: css("--cold"), text: css("--text"), muted: css("--muted") };
}

// Gleitender 7-Tage-Durchschnitt nach Zeit (manche Coins zahlen alle 4 h oder 1 h statt 8 h)
function fundingAvg() {
  const out = [];
  let start = 0, sum = 0;
  fundData.forEach((f, i) => {
    sum += f.rate_8h;
    while (fundData[start].time <= f.time - FUNDING_AVG_DAYS * 86400) sum -= fundData[start++].rate_8h;
    if (f.time - fundData[0].time >= FUNDING_AVG_DAYS * 86400 - 3600) out.push({ time: f.time, value: sum / (i - start + 1) });
  });
  fundAvgByTime = new Map(out.map((p) => [p.time, p.value]));
  return out;
}

function renderFundLegend(time) {
  if (!fundData.length) { $("fund-legend").innerHTML = ""; return; }
  const f = (time != null && fundData.find((x) => x.time === time)) || fundData[fundData.length - 1];
  const avg = fundAvgByTime.get(f.time);
  const takt = f.interval_h !== 8 ? `<span>${L("Takt", "Every")} ${f.interval_h} h, ${L("gezahlt", "paid")} <b>${pct4(f.rate)}</b></span>` : "";
  $("fund-legend").innerHTML = `<span class="${fundTone(f.rate_8h)}">${f.interval_h !== 8 ? "≙ 8 h" : L("Zahlung", "Payment")} <b>${pct4(f.rate_8h)}</b></span>${takt}`
    + (avg != null ? `<span class="${fundTone(avg)}">${L("Ø", "avg")} ${FUNDING_AVG_DAYS} ${L("T", "d")} <b>${pct4(avg)}</b></span>` : "");
}

function renderOiLegend(time) {
  if (!oiData.length) { $("oi-legend").innerHTML = ""; return; }
  const i = time != null ? oiData.findIndex((x) => x.time === time) : -1;
  const o = oiData[i >= 0 ? i : oiData.length - 1];
  const ref = oiData.find((x) => x.time >= o.time - OI_CHANGE_DAYS * 86400);
  const chg = ref && ref !== o && o.time - ref.time >= (OI_CHANGE_DAYS - 0.5) * 86400 ? (o.usd - ref.usd) / ref.usd : null;
  $("oi-legend").innerHTML = `<span>OI <b>$${compact(o.usd)}</b></span>`
    + (chg != null ? `<span>${OI_CHANGE_DAYS} ${L("T", "d")} <b>${pct(chg)}</b></span>` : "")
    + (o.price != null ? `<span>${L("Kurs", "Price")} <b>${usd(o.price)}</b></span>` : "");
}

function initDerivCharts() {
  const c = derivColors();
  fundChart = miniChart($("fund-chart"));
  fundBars = fundChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false,
    priceFormat: { type: "custom", formatter: (v) => pct4(v), minMove: 1e-7 } });
  fundAvg = fundChart.addLineSeries({ color: c.text, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerVisible: false, priceFormat: { type: "custom", formatter: (v) => pct4(v), minMove: 1e-7 } });
  // Binance-Standard 0,01 % als Bezug
  fundBars.createPriceLine({ price: 0.0001, color: c.muted, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dashed,
    axisLabelVisible: true, title: "Standard" });

  oiChart = miniChart($("oi-chart"));
  oiChart.applyOptions({ leftPriceScale: { visible: true, borderColor: css("--border") } });
  oiSeries = oiChart.addAreaSeries({ lineColor: c.cold, topColor: alpha(c.cold, 0.25), bottomColor: alpha(c.cold, 0.02),
    lineWidth: 2, priceLineVisible: false, priceFormat: { type: "custom", formatter: (v) => "$" + compact(v), minMove: 1 } });
  oiPrice = oiChart.addLineSeries({ priceScaleId: "left", color: c.muted, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
    priceLineVisible: false, crosshairMarkerVisible: false, title: L("Kurs", "Price"),
    priceFormat: { type: "custom", formatter: (v) => usd(v), minMove: 1e-8 } });

  fundChart.subscribeCrosshairMove((p) => renderFundLegend(p && p.time != null ? p.time : null));
  oiChart.subscribeCrosshairMove((p) => renderOiLegend(p && p.time != null ? p.time : null));

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const t = themeOptions(), n = derivColors();
    fundChart.applyOptions(t.chart);
    oiChart.applyOptions({ ...t.chart, leftPriceScale: { borderColor: css("--border") } });
    fundAvg.applyOptions({ color: n.text });
    setFundBars();
    oiSeries.applyOptions({ lineColor: n.cold, topColor: alpha(n.cold, 0.25), bottomColor: alpha(n.cold, 0.02) });
    oiPrice.applyOptions({ color: n.muted });
  });
}

function setFundBars() {
  const c = derivColors();
  fundBars.setData(fundData.map((f) => ({ time: f.time, value: f.rate_8h, color: alpha(f.rate_8h >= 0 ? c.hot : c.cold, 0.6) })));
}

function derivMessage(text) {
  for (const id of ["fund-msg", "oi-msg"]) { $(id).textContent = text || ""; $(id).hidden = !text; }
}

async function loadDerivs() {
  try {
    const res = await fetch("api/derivatives?symbol=" + encodeURIComponent(symbol));
    const p = await res.json().catch(() => ({}));
    if (res.status === 404) {
      derivMessage("");
      $("derivs").innerHTML = `<div class="status">${L(`Für ${esc(symbol)} gibt es keine USDT-Perpetuals auf Binance Futures – daher kein Funding und Open Interest.`, `There are no USDT perpetuals for ${esc(symbol)} on Binance Futures – so no funding and open interest.`)}</div>`;
      return;
    }
    if (!res.ok) throw new Error(p.error || "HTTP " + res.status);
    fundData = p.funding;
    oiData = p.open_interest;
    derivMessage("");
    if (fundData.length) {
      setFundBars();
      fundAvg.setData(fundingAvg());
      fundChart.timeScale().fitContent();
    } else { $("fund-msg").hidden = false; $("fund-msg").textContent = L("Keine Funding-Daten.", "No funding data."); }
    if (oiData.length) {
      oiSeries.setData(oiData.map((o) => ({ time: o.time, value: o.usd })));
      oiPrice.setData(oiData.filter((o) => o.price != null).map((o) => ({ time: o.time, value: o.price })));
      oiChart.timeScale().fitContent();
    } else { $("oi-msg").hidden = false; $("oi-msg").textContent = L("Keine Open-Interest-Daten.", "No open interest data."); }
    renderFundLegend(null);
    renderOiLegend(null);
  } catch (e) {
    derivMessage(L(`Daten nicht verfügbar (${e.message}).`, `Data not available (${e.message}).`));
  }
}

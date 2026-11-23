// Detailseite: On-Chain-Daten von Coin Metrics – Börsenzu-/abflüsse, Bestand auf Börsen (nur BTC/ETH), MVRV.
// Gemeinsame Helfer in coin-base.js. MVRV zählt auch im Score (sentiment.py), Flüsse und Bestand nur Anzeige.

const OC_FLOW_DAYS = 365;          // Netto-Zufluss: letztes Jahr (Tagesbalken)
const OC_FLOW_AVG = 30;            // gleitender Durchschnitt über die Balken
const OC_MVRV_LEVELS = [1, 3.5];   // Bezugslinien: unter 1 Halter im Verlust, über 3,5 weit im Gewinn (BTC-Erfahrung)

let ocData = null;
let ocFlowChart = null, ocFlowBars = null, ocFlowAvg = null;
let ocSupplyChart = null, ocSupplySeries = null, ocSupplyPrice = null;
let ocMvrvChart = null, ocMvrvSeries = null, ocMvrvPrice = null;
const ocByTime = new Map();

const ocColors = () => ({ up: css("--up"), down: css("--down"), cold: css("--cold"), hot: css("--hot"), text: css("--text"), muted: css("--muted") });
const ocUsd = (v) => (v < 0 ? "−" : "") + "$" + compact(Math.abs(v));
const ocNum = (v, d = 2) => v.toLocaleString(LOCALE, { minimumFractionDigits: d, maximumFractionDigits: d });

function initOnchainCharts() {
  const c = ocColors();
  ocFlowChart = miniChart($("oc-flow-chart"));
  ocFlowBars = ocFlowChart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false,
    priceFormat: { type: "custom", formatter: ocUsd, minMove: 1 } });
  ocFlowAvg = ocFlowChart.addLineSeries({ color: c.text, lineWidth: 1.5, priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerVisible: false, priceFormat: { type: "custom", formatter: ocUsd, minMove: 1 } });

  const withPrice = (el, series) => {
    const ch = miniChart(el);
    ch.applyOptions({ leftPriceScale: { visible: true, borderColor: css("--border") } });
    const main = ch.addAreaSeries(series);
    const price = ch.addLineSeries({ priceScaleId: "left", color: c.muted, lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted,
      // Kurs nur als Linie, ohne Label an der Achse (überdeckte sonst die Achsenzahlen; der Wert steht in der Legende)
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, priceFormat: { type: "custom", formatter: (v) => usd(v), minMove: 1e-10 },
      priceFormat: { type: "custom", formatter: (v) => usd(v), minMove: 1e-8 } });
    return [ch, main, price];
  };
  [ocSupplyChart, ocSupplySeries, ocSupplyPrice] = withPrice($("oc-supply-chart"), { lineColor: c.cold,
    topColor: alpha(c.cold, 0.25), bottomColor: alpha(c.cold, 0.02), lineWidth: 2, priceLineVisible: false,
    priceFormat: { type: "custom", formatter: (v) => compact(v), minMove: 1 } });
  [ocMvrvChart, ocMvrvSeries, ocMvrvPrice] = withPrice($("oc-mvrv-chart"), { lineColor: c.hot,
    topColor: alpha(c.hot, 0.18), bottomColor: alpha(c.hot, 0.02), lineWidth: 2, priceLineVisible: false,
    priceFormat: { type: "custom", formatter: (v) => ocNum(v), minMove: 0.01 } });
  for (const lvl of OC_MVRV_LEVELS) {
    ocMvrvSeries.createPriceLine({ price: lvl, color: lvl < 2 ? c.cold : c.hot, lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: lvl < 2 ? "Einstand" : "heiß" });
  }

  ocFlowChart.subscribeCrosshairMove((p) => renderOcLegends(p && p.time != null ? p.time : null));
  ocSupplyChart.subscribeCrosshairMove((p) => renderOcLegends(p && p.time != null ? p.time : null));
  ocMvrvChart.subscribeCrosshairMove((p) => renderOcLegends(p && p.time != null ? p.time : null));

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    const t = themeOptions(), n = ocColors();
    ocFlowChart.applyOptions(t.chart);
    for (const ch of [ocSupplyChart, ocMvrvChart]) ch.applyOptions({ ...t.chart, leftPriceScale: { borderColor: css("--border") } });
    ocFlowAvg.applyOptions({ color: n.text });
    ocSupplySeries.applyOptions({ lineColor: n.cold, topColor: alpha(n.cold, 0.25), bottomColor: alpha(n.cold, 0.02) });
    ocMvrvSeries.applyOptions({ lineColor: n.hot, topColor: alpha(n.hot, 0.18), bottomColor: alpha(n.hot, 0.02) });
    for (const s of [ocSupplyPrice, ocMvrvPrice]) s.applyOptions({ color: n.muted });
    if (ocData) setOcFlow();
  });
}

const ocNet = (d) => (d.inflow != null && d.outflow != null ? d.inflow - d.outflow : null);

function setOcFlow() {
  const c = ocColors(), days = ocData.days.filter((d) => ocNet(d) != null).slice(-OC_FLOW_DAYS);
  ocFlowBars.setData(days.map((d) => ({ time: d.time, value: ocNet(d), color: alpha(ocNet(d) > 0 ? c.down : c.up, 0.55) })));
  const avg = [];
  for (let i = OC_FLOW_AVG - 1; i < days.length; i++) {
    let s = 0;
    for (let j = i - OC_FLOW_AVG + 1; j <= i; j++) s += ocNet(days[j]);
    avg.push({ time: days[i].time, value: s / OC_FLOW_AVG });
  }
  ocFlowAvg.setData(avg);
  ocFlowChart.timeScale().fitContent();
}

// Wert vor n Tagen (oder der nächstältere vorhandene)
function ocAgo(key, n) {
  const days = ocData.days, last = days[days.length - 1];
  for (let i = days.length - 1; i >= 0; i--) if (days[i].time <= last.time - n * 86400 && days[i][key] != null) return days[i][key];
  return null;
}

function renderOcStats() {
  const days = ocData.days, last = [...days].reverse().find((d) => d.mvrv != null || d.inflow != null) || days[days.length - 1];
  const tiles = [];
  if (ocData.has_flows) {
    const recent = days.slice(-7).filter((d) => ocNet(d) != null);
    const net = recent.reduce((s, d) => s + ocNet(d), 0), inSum = recent.reduce((s, d) => s + d.inflow, 0), outSum = recent.reduce((s, d) => s + d.outflow, 0);
    tiles.push(statTile("ocFlow", L("Netto-Zufluss 7 T", "Net inflow 7 d"), `<span class="${net > 0 ? "down" : "up"}">${net > 0 ? "+" : ""}${ocUsd(net)}</span>`,
      `<div class="det">${net > 0 ? L("mehr eingezahlt als abgezogen", "more deposited than withdrawn") : L("mehr abgezogen als eingezahlt", "more withdrawn than deposited")} · ${L("zu", "in")} ${ocUsd(inSum)}, ${L("ab", "out")} ${ocUsd(outSum)}</div>`));
    const sup = last.supply_ex, s30 = ocAgo("supply_ex", 30), s365 = ocAgo("supply_ex", 365);
    if (sup != null) {
      const ch = (ref) => ref ? `${pctSignedDe(sup / ref - 1)}` : "–";
      tiles.push(statTile("ocSupply", L("Bestand auf Börsen", "Exchange balance"), `${compact(sup)} ${esc(symbol)}`,
        `<div class="det">${L("30 T", "30 d")}: <b>${ch(s30)}</b> · ${L("1 J", "1 y")}: <b>${ch(s365)}</b>
          · ${last.price ? "≈ " + ocUsd(sup * last.price) : ""}</div>`));
    }
  }
  if (last.mvrv != null) {
    const m = last.mvrv;
    // Stufen wie mvrv_word in sentiment.py (gleiche Wörter wie in der Score-Zusammensetzung)
    const zone = m < 1 ? ["cold", L("Halter im Schnitt im Verlust – bei BTC lagen frühere Tiefs hier", "holders at a loss on average – earlier BTC lows were here")]
      : m < 1.6 ? ["", L("Halter leicht im Gewinn, nahe am Einstand", "holders slightly in profit, near cost basis")]
      : m < 2.5 ? ["", L("Halter moderat im Gewinn", "holders moderately in profit")]
      : m < 3.5 ? ["hot", L("Halter deutlich im Gewinn", "holders well in profit")]
      : ["hot", L("Halter weit im Gewinn – bei BTC lagen frühere Hochs hier", "holders deep in profit – earlier BTC tops were here")];
    const m365 = ocData.days.slice(-365).map((d) => d.mvrv).filter((v) => v != null);
    tiles.push(statTile("ocMvrv", "MVRV", `<span class="${zone[0] === "cold" ? "up" : zone[0] === "hot" ? "down" : ""}">${ocNum(m)}</span>`,
      `<div class="det">${zone[1]} · ${L("1 J", "1 y")}: ${ocNum(Math.min(...m365))} – ${ocNum(Math.max(...m365))}</div>`));
  }
  $("oc-stats").innerHTML = tiles.join("");
  const when = new Date(last.time * 1000).toLocaleDateString(LOCALE);
  $("oc-note").textContent = `${L("Stand", "As of")} ${when}` + (ocData.has_flows ? "" : L(" · Börsenflüsse gibt es kostenlos nur für BTC und ETH", " · exchange flows are free only for BTC and ETH"));
}

function renderOcLegends(time) {
  if (!ocData) return;
  const d = (time != null && ocByTime.get(time)) || [...ocData.days].reverse().find((x) => x.mvrv != null || x.inflow != null);
  if (!d) return;
  const date = `<span>${new Date(d.time * 1000).toLocaleDateString(LOCALE)}</span>`;
  if (ocData.has_flows) {
    const n = ocNet(d);
    $("oc-flow-legend").innerHTML = n == null ? "" : date
      + `<span class="${n > 0 ? "hot" : "cold"}">${L("netto", "net")} <b>${n > 0 ? "+" : ""}${ocUsd(n)}</b></span><span>${L("zu", "in")} <b>${ocUsd(d.inflow)}</b> · ${L("ab", "out")} <b>${ocUsd(d.outflow)}</b></span>`;
    $("oc-supply-legend").innerHTML = d.supply_ex == null ? "" : date
      + `<span>${L("Bestand", "Balance")} <b>${compact(d.supply_ex)} ${esc(symbol)}</b></span>` + (d.price ? `<span>${L("Kurs", "Price")} <b>${usd(d.price)}</b></span>` : "");
  }
  $("oc-mvrv-legend").innerHTML = d.mvrv == null ? "" : date
    + `<span>MVRV <b>${ocNum(d.mvrv)}</b></span>` + (d.price ? `<span>${L("Kurs", "Price")} <b>${usd(d.price)}</b></span>` : "");
}

async function loadOnchain() {
  try {
    const res = await fetch("api/onchain?symbol=" + encodeURIComponent(symbol));
    const p = await res.json().catch(() => ({}));
    if (res.status === 404) {
      $("onchain").innerHTML = `<div class="status">${L(`Für ${esc(symbol)} gibt es bei Coin Metrics (kostenlose Version) keine On-Chain-Daten.`, `Coin Metrics (free version) has no on-chain data for ${esc(symbol)}.`)}</div>`;
      return;
    }
    if (!res.ok) throw new Error(p.error || "HTTP " + res.status);
    ocData = p;
    const lastDay = p.days.length ? new Date(p.days[p.days.length - 1].time * 1000).toISOString().slice(0, 10) : null;
    setDataStatus("ds-onchain", { src: "Coin Metrics", kind: "daily", day: lastDay, lag: 1,
      note: L("Abruf beim Server: stündlich", "Fetched by the server: hourly") });
    ocByTime.clear();
    for (const d of p.days) ocByTime.set(d.time, d);
    $("onchain").classList.toggle("mvrv-only", !p.has_flows);
    if (p.has_flows) {
      setOcFlow();
      const sup = p.days.filter((d) => d.supply_ex != null);
      ocSupplySeries.setData(sup.map((d) => ({ time: d.time, value: d.supply_ex })));
      ocSupplyPrice.setData(sup.filter((d) => d.price != null).map((d) => ({ time: d.time, value: d.price })));
      ocSupplyChart.timeScale().fitContent();
    }
    const mv = p.days.filter((d) => d.mvrv != null);
    ocMvrvSeries.setData(mv.map((d) => ({ time: d.time, value: d.mvrv })));
    ocMvrvPrice.setData(mv.filter((d) => d.price != null).map((d) => ({ time: d.time, value: d.price })));
    ocMvrvChart.timeScale().fitContent();
    $("oc-msg").hidden = true;
    renderOcStats();
    renderOcLegends(null);
  } catch (e) {
    setDataStatus("ds-onchain", { src: "Coin Metrics", na: true });
    $("oc-msg").textContent = L(`On-Chain-Daten nicht verfügbar (${e.message}).`, `On-chain data not available (${e.message}).`);
  }
}

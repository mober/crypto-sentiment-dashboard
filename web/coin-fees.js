// Detailseite: Gebühren und Einnahmen (DefiLlama).
// Gemeinsame Helfer in coin-base.js.

// --- Gebühren & Einnahmen (DefiLlama) ---------------------------------------

const FEE_SERIES = [  // [Schlüssel, Name, Farbe (CSS-Variable), gestrichelt]
  ["fees", L("Gebühren", "Fees"), "--muted", false],
  ["revenue", L("Einnahmen", "Revenue"), "--cold", false],
  ["holders", L("An Token-Halter", "To token holders"), "--hot", true],
];
let feeData = null, feeChart = null;
const feeSeries = {};

// usd6 ist für große Beträge gedacht: 0 würde „$0,00000“, 950 würde „$950,000“
const money = (v) => (!v ? "$0" : Math.abs(v) < 1e6 ? usd(v) : usd6(v));

function feeTiles() {
  const f = feeData;
  if (!f) return "";
  const mc = statsCoin && statsCoin.data.market_cap ? statsCoin.data.market_cap.usd : null;
  const fdv = statsCoin && statsCoin.data.market_stats ? statsCoin.data.market_stats.fdv : null;
  const tiles = [];
  const vs = (x) => x && x.prev30d > 0 ? `${chgSpan(x.total30d / x.prev30d - 1)} ${L("zu den 30 T davor", "vs. the 30 d before")}` : "";
  if (f.fees) {
    tiles.push(statTile("feesTotal", L("Gebühren 30 T", "Fees 30 d"), money(f.fees.total30d),
      `<div class="det">${vs(f.fees)}</div>`));
  }
  if (f.revenue) {
    const share = f.fees && f.fees.total30d ? f.revenue.total30d / f.fees.total30d : null;
    tiles.push(statTile("feesRevenue", L("Einnahmen 30 T", "Revenue 30 d"), money(f.revenue.total30d),
      `<div class="det">${share != null ? `<b>${numDe(share * 100, 0)}${PCT}</b> ${L("der Gebühren", "of fees")} · ` : ""}${vs(f.revenue)}</div>`));
  }
  tiles.push(statTile("feesHolders", L("An Token-Halter 30 T", "To token holders 30 d"), f.holders ? money(f.holders.total30d) : "–",
    `<div class="det">${f.holders ? L("Rückkäufe, Burns oder Ausschüttungen", "buybacks, burns or distributions") : L("keine Weitergabe an Halter erfasst", "no pass-through to holders recorded")}</div>`));
  if (f.revenue || f.fees) {
    const x = f.revenue || f.fees;
    tiles.push(statTile("feesYear", `${f.revenue ? L("Einnahmen", "Revenue") : L("Gebühren", "Fees")} ${L("1 J", "1 y")}`, x.total1y ? money(x.total1y) : "–",
      `<div class="det">${x.totalAllTime ? `${L("gesamt seit Start", "total since start")} ${money(x.totalAllTime)}` : ""}</div>`));
  }
  // Bewertung: Marktkap. im Verhältnis zu den hochgerechneten Einnahmen der letzten 30 T
  const annual = f.revenue ? f.revenue.total30d * 365 / 30 : null;
  if (mc && annual > 0) {
    const mult = mc / annual;
    tiles.push(statTile("feesMultiple", L("Marktkap. / Einnahmen", "Market cap / revenue"), `${numDe(mult, mult < 10 ? 1 : 0)}x`,
      `<div class="det">${L("30 T hochgerechnet", "annualised from 30 d")}${fdv ? ` · ${L("FDV-Basis", "FDV basis")} ${numDe(fdv / annual, 0)}x` : ""}</div>`));
  }
  const holdersAnnual = f.holders ? f.holders.total30d * 365 / 30 : null;
  if (mc && holdersAnnual > 0) {
    tiles.push(statTile("feesYield", L("Rendite für Halter", "Holder yield"), `${numDe(holdersAnnual / mc * 100, 1)}${PCT} ${L("p.a.", "p.a.")}`,
      `<div class="det">${L("Weitergabe an Halter / Marktkap., 30 T hochgerechnet", "pass-through to holders / market cap, annualised from 30 d")}</div>`));
  }
  return tiles.join("");
}

function renderFees() {
  if (!feeData) return;
  $("fee-tiles").innerHTML = feeTiles();
}

function renderFeeLegend(time) {
  if (!feeData) return;
  $("fee-legend").innerHTML = FEE_SERIES.filter(([k]) => feeData[k]).map(([k, label, color, dashed]) => {
    const w = feeData[k].weekly;
    const pt = (time != null && w.find((x) => x.time === time)) || w[w.length - 1];
    return `<span><span class="sw${dashed ? " dash" : ""}" style="background:var(${color});border-color:var(${color})"></span>${label} <b>${pt ? money(pt.value) : "–"}</b></span>`;
  }).join("") + `<span>je Woche${time != null ? ", ab " + fmtDate(time, { day: "2-digit", month: "2-digit", year: "2-digit", timeZone: "UTC" }) : ", letzte volle Woche"}</span>`;
}

function initFeeChart() {
  feeChart = miniChart($("fee-chart"));
  for (const [k, , color, dashed] of FEE_SERIES) {
    feeSeries[k] = feeChart.addLineSeries({
      color: css(color), lineWidth: 2, lineStyle: dashed ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: true,
      priceFormat: { type: "custom", formatter: (v) => "$" + compact(v), minMove: 1 },
    });
  }
  feeChart.subscribeCrosshairMove((p) => renderFeeLegend(p && p.time != null ? p.time : null));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    feeChart.applyOptions(themeOptions().chart);
    for (const [k, , color] of FEE_SERIES) feeSeries[k].applyOptions({ color: css(color) });
  });
}

async function loadFees() {
  try {
    const res = await fetch("api/fees?symbol=" + encodeURIComponent(symbol) + (coinId ? "&id=" + encodeURIComponent(coinId) : ""));
    if (!res.ok) return;   // keine Gebührendaten: Karte bleibt ausgeblendet
    feeData = await res.json();
    $("fees").hidden = false;
    const feeLast = Math.max(0, ...FEE_SERIES.map(([k]) => feeData[k] && feeData[k].weekly.length ? feeData[k].weekly[feeData[k].weekly.length - 1].time : 0));
    setDataStatus("ds-fees", feeLast
      ? { src: "DefiLlama", kind: "daily", day: new Date(feeLast * 1000).toISOString().slice(0, 10), lag: 8,
        note: L("Chart in Wochensummen (Beginn der letzten Woche); Abruf beim Server alle 30 Min.", "Chart in weekly sums (start of the latest week); fetched by the server every 30 min") }
      : { src: "DefiLlama", kind: "poll", at: feeData.fetched_at, every: 1800 });
    $("fee-source").textContent = `DefiLlama · ${feeData.name} (${feeData.kind === "chain" ? "Chain" : L("Protokoll", "protocol")}) · ${L("täglich aktualisiert", "updated daily")}`;
    initFeeChart();
    for (const [k] of FEE_SERIES) {
      feeSeries[k].setData(feeData[k] ? feeData[k].weekly.map((x) => ({ time: x.time, value: x.value })) : []);
    }
    feeChart.timeScale().fitContent();
    renderFees();
    renderFeeLegend(null);
  } catch (e) { /* ohne Gebührenblock weiter */ }
}

// Detailseite: Gebühren und Einnahmen (DefiLlama).
// Gemeinsame Helfer in coin-base.js.

// --- Gebühren & Einnahmen (DefiLlama) ---------------------------------------

const FEE_SERIES = [  // [Schlüssel, Name, Farbe (CSS-Variable), gestrichelt]
  ["fees", "Gebühren", "--muted", false],
  ["revenue", "Einnahmen", "--cold", false],
  ["holders", "An Token-Halter", "--hot", true],
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
  const vs = (x) => x && x.prev30d > 0 ? `${chgSpan(x.total30d / x.prev30d - 1)} zu den 30 T davor` : "";
  if (f.fees) {
    tiles.push(statTile("feesTotal", "Gebühren 30 T", money(f.fees.total30d),
      `<div class="det">${vs(f.fees)}</div>`));
  }
  if (f.revenue) {
    const share = f.fees && f.fees.total30d ? f.revenue.total30d / f.fees.total30d : null;
    tiles.push(statTile("feesRevenue", "Einnahmen 30 T", money(f.revenue.total30d),
      `<div class="det">${share != null ? `<b>${numDe(share * 100, 0)} %</b> der Gebühren · ` : ""}${vs(f.revenue)}</div>`));
  }
  tiles.push(statTile("feesHolders", "An Token-Halter 30 T", f.holders ? money(f.holders.total30d) : "–",
    `<div class="det">${f.holders ? "Rückkäufe, Burns oder Ausschüttungen" : "keine Weitergabe an Halter erfasst"}</div>`));
  if (f.revenue || f.fees) {
    const x = f.revenue || f.fees;
    tiles.push(statTile("feesYear", `${f.revenue ? "Einnahmen" : "Gebühren"} 1 J`, x.total1y ? money(x.total1y) : "–",
      `<div class="det">${x.totalAllTime ? `gesamt seit Start ${money(x.totalAllTime)}` : ""}</div>`));
  }
  // Bewertung: Marktkap. im Verhältnis zu den hochgerechneten Einnahmen der letzten 30 T
  const annual = f.revenue ? f.revenue.total30d * 365 / 30 : null;
  if (mc && annual > 0) {
    const mult = mc / annual;
    tiles.push(statTile("feesMultiple", "Marktkap. / Einnahmen", `${numDe(mult, mult < 10 ? 1 : 0)}x`,
      `<div class="det">30 T hochgerechnet${fdv ? ` · FDV-Basis ${numDe(fdv / annual, 0)}x` : ""}</div>`));
  }
  const holdersAnnual = f.holders ? f.holders.total30d * 365 / 30 : null;
  if (mc && holdersAnnual > 0) {
    tiles.push(statTile("feesYield", "Rendite für Halter", `${numDe(holdersAnnual / mc * 100, 1)} % p.a.`,
      `<div class="det">Weitergabe an Halter / Marktkap., 30 T hochgerechnet</div>`));
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
    $("fee-source").textContent = `DefiLlama · ${feeData.name} (${feeData.kind === "chain" ? "Chain" : "Protokoll"}) · täglich aktualisiert`;
    initFeeChart();
    for (const [k] of FEE_SERIES) {
      feeSeries[k].setData(feeData[k] ? feeData[k].weekly.map((x) => ({ time: x.time, value: x.value })) : []);
    }
    feeChart.timeScale().fitContent();
    renderFees();
    renderFeeLegend(null);
  } catch (e) { /* ohne Gebührenblock weiter */ }
}

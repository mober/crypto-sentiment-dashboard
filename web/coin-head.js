// Detailseite: Kopf (Kurs, Lagebild, News), Kennzahlen, Zusammensetzung des Scores, Ein-/Ausklappen der Karten.
// Gemeinsame Helfer in coin-base.js.

// --- Kopfbereich ---------------------------------------------------------

// Lagebild unter Kurs und Score: Einordnung, ein Satz zur Lage, Empfehlung, Für und Wider (kommt fertig vom Server,
// siehe situation() in sentiment.py)
const MOOD_TONE = { fear: "tone-cold", greed: "tone-hot", neutral: "tone-neutral" };

function lageBlock(c) {
  const s = c.situation;
  if (!s) return "";
  const list = (items) => items.length ? items.map((x) => `<li>${esc(x)}</li>`).join("") : '<li class="none">nichts Ausgeprägtes</li>';
  // die zwei Signale mit dem größten Ausschlag
  const drivers = [...c.signals].sort((a, b) => Math.abs(b.points) - Math.abs(a.points)).slice(0, 2)
    .map((x) => `${esc(x.name)} <b>${signed(x.points)}</b>`).join(", ");
  return `
    <div class="lage" id="hero-lage">
      <div class="lage-head">
        <span class="label"><span${tipAttr("situation")}>Lage</span></span>
        <span class="verdict ${MOOD_TONE[s.mood] || "tone-neutral"}">${esc(s.title)}</span>
      </div>
      <div class="lage-sum">${esc(s.summary)}</div>
      <div class="lage-text">${esc(s.text)}</div>
      ${s.action ? `<div class="lage-action act-${esc(s.action_tone || "wait")}">
        <div><span class="label"><span${tipAttr("action")}>Empfehlung</span></span> <span class="act">${esc(s.action)}</span></div>
        <div class="why">${esc(s.action_text)}</div>
      </div>` : ""}
      <div class="lage-cols">
        <div class="pro"><div class="label">Für steigende Kurse spricht</div><ul>${list(s.pro)}</ul></div>
        <div class="contra"><div class="label">Dagegen spricht</div><ul>${list(s.contra)}</ul></div>
      </div>
      <div class="lage-foot">${drivers ? `Stärkste Signale im Score: ${drivers} · ` : ""}Einordnung nach Erfahrungswerten.</div>
    </div>`;
}

// News zum Coin: Schlagzeilen deutschsprachiger Krypto-Medien, vom Server aus RSS-Feeds gefiltert
const NEWS_REFRESH_MS = 15 * 60 * 1000;   // wie NEWS_CACHE_SECONDS in server.py
let newsData = null, newsName = "";

const ago = (t) => {
  const min = Math.max(0, (Date.now() / 1000 - t) / 60);
  return min < 60 ? `vor ${Math.round(min)} Min.` : min < 48 * 60 ? `vor ${Math.round(min / 60)} Std.` : `vor ${Math.round(min / 1440)} Tagen`;
};

function renderNews() {
  const el = $("hero-news");
  if (!el) return;
  const items = newsData ? newsData.items : [];
  const who = esc(newsName || symbol);
  el.hidden = !items.length;
  el.innerHTML = !items.length ? "" : `
    <div class="label news-head"><span${tipAttr("news")}>${newsData.general ? `Keine aktuellen Meldungen zu ${who} – neueste Krypto-News` : `News zu ${who}`}</span></div>
    <ul>${items.map((i) => `<li><a href="${esc(i.link)}" target="_blank" rel="noopener noreferrer" title="${esc(i.teaser)}">${esc(i.title)}</a>`
      + `<span class="meta">${esc(i.source)} · ${ago(i.time)}</span></li>`).join("")}</ul>`;
  setupCollapse(el);
}

async function loadNews(name) {
  // Name nur mitschicken, wenn der Server ihn annimmt (siehe NAME_RE in server.py)
  newsName = /^[\w .&'+-]{1,40}$/.test(name || "") ? name : "";
  try {
    const res = await fetch("api/news?symbol=" + encodeURIComponent(symbol) + (newsName ? "&name=" + encodeURIComponent(newsName) : ""));
    if (!res.ok) return;   // Feeds nicht erreichbar: Kopf bleibt ohne News
    newsData = await res.json();
    renderNews();
  } catch (e) { /* ohne News weiter */ }
}

function renderHero(c) {
  const ma = c.data.price_ma200;
  const mc = c.data.market_cap;
  const st = c.data.market_stats || {};
  const ch = st.change || {};
  const fng = c.fear_greed;
  const changes = [["24 h", ch["24h"]], ["7 T", ch["7d"]]].filter(([, v]) => v != null)
    .map(([label, v]) => `<span>${label} ${change(v)}</span>`).join("");
  const facts = [
    mc ? `Marktkap. ${usd6(mc.usd)}${mc.rank ? ` · Rang ${mc.rank}` : ""}` : "Marktkap. n/a",
    st.ath_change == null ? "" : st.ath_change < 0 ? `<b>${pct(-st.ath_change).replace("+", "")}</b> unter dem Allzeithoch` : "<b>auf dem Allzeithoch</b>",
    fng ? `Fear &amp; Greed <b>${fng.value}</b> (${fngZone(fng.value).name})` : "",
  ].filter(Boolean).join(" · ");
  const coinName = c.data.name || symbol;
  $("title-star").innerHTML = watchStar({ id: coinId || c.data.coin_id, symbol, name: c.data.name });
  const aboutHref = "about.html?symbol=" + encodeURIComponent(symbol) + (coinId ? "&id=" + encodeURIComponent(coinId) : "");
  $("hero").className = "card hero";
  $("hero").innerHTML = `
    <div>
      <a class="about-link" href="${esc(aboutHref)}" title="Beschreibung, Kategorie, Fakten und Einschätzung">Was ist ${esc(coinName)}? Profil &amp; Einschätzung →</a>
      <div class="price-line">
        <div class="price" id="hero-price">${ma ? usd(ma.price) : c.data.price != null ? usd(c.data.price) : "–"}</div>
        ${changes ? `<div class="chg">${changes}</div>` : ""}
      </div>
      <div class="mcap">${facts}</div>
    </div>
    <div class="score-box">
      <div class="label">Überhitzungs-Score</div>
      <div class="score">${signed(c.score)}</div>
      <div class="verdict ${tone(c.score)}">${esc(c.verdict)}</div>
      <div class="track" id="score-track"></div>
    </div>
    ${lageBlock(c)}
    <div class="news" id="hero-news" hidden></div>`;
  if ($("hero-lage")) setupCollapse($("hero-lage"));
  renderNews();
  fillHeroPrice();  // falls das Chart schon vor dem Kopf geladen war
  renderStats(c);
  renderBreakdown(c);
  renderTrack();   // Trefferquote, falls der Score-Verlauf schon geladen ist
}

function bdRow(name, reason, points, share, total = false, n = 1, weight = 1) {
  const w = Math.min(100, Math.abs(points)) / 2;  // halbe Spurbreite = 100 Punkte
  const bar = points === 0 ? "" : `<div class="bd-bar ${points > 0 ? "pos" : "neg"}" style="width:${w}%"></div>`;
  // Tooltip: allgemeine Erklärung (tips.js) plus Einordnung des aktuellen Werts
  const key = total ? "score" : SIGNAL_TIPS[name];
  const now = total
    ? `Aktuell: ${signed(points)} = gewichteter Durchschnitt aus ${n} Signalen.`
    : `Aktuell: ${reason} → ${signed(points)} Punkte, Gewicht ${weight.toLocaleString("de-DE")}. Das macht ${num1(share)} im Score aus.`;
  const tip = key && TIPS[key] ? ` data-tip="${key}" data-tip-now="${esc(now)}" tabindex="0"` : "";
  return `
    <div class="bd-row${total ? " total" : ""}">
      <div class="bd-label"><div class="bd-name"><span${tip}>${esc(name)}</span></div>${reason ? `<div class="bd-reason">${esc(reason)}</div>` : ""}</div>
      <div class="bd-track" role="img" aria-label="${esc(name)}: ${signed(points)} von −100 bis +100">${bar}</div>
      <div class="bd-val">${signed(points)}${share != null ? `<span class="share">${num1(share)} im Score</span>` : ""}</div>
    </div>`;
}

// Score = gewichteter Durchschnitt (Gewichte SIGNAL_WEIGHTS in sentiment.py), Anteil = Punkte × Gewicht / Summe der Gewichte
function renderBreakdown(c) {
  const n = c.signals.length;
  const w = (s) => s.weight ?? 1, sumW = c.signals.reduce((a, s) => a + w(s), 0);
  if (!n) {
    $("breakdown").innerHTML = '<div class="status">Keine Signale für diesen Coin.</div>';
    return;
  }
  $("breakdown").innerHTML = `
    <div class="breakdown-head">
      <div class="label">Zusammensetzung des Überhitzungs-Scores</div>
      <div class="note">${n} Signale · Score = gewichteter Durchschnitt (Trend und Optionsvola zählen mehr, Fear &amp; Greed und RSI weniger)</div>
    </div>
    ${c.signals.map((s) => bdRow(s.name + (w(s) !== 1 ? ` ×${w(s).toLocaleString("de-DE")}` : ""), s.reason, s.points, s.points * w(s) / sumW, false, n, w(s))).join("")}
    ${bdRow("Überhitzungs-Score", c.verdict, c.score, null, true, n)}
    <div class="bd-axis"><span></span><div class="ticks"><span>−100 Angst</span><span>0</span><span>Gier +100</span></div><span></span></div>`;
  setupCollapse($("breakdown"));  // Kopfzeile wurde neu geschrieben
}

// Kennzahlen aus den CoinGecko-Marktdaten (kommen mit dem Score, keine eigene Abfrage)
const big = (v) => v.toLocaleString("de-DE", { notation: "compact", compactDisplay: "long", maximumSignificantDigits: 4 })
  .replace(/ Millionen?/, " Mio.").replace(/ Milliarden?/, " Mrd.").replace(/ Billionen?/, " Bio.");
const pctSigned = (v) => {
  const a = Math.abs(v * 100);
  const txt = a >= 1000 ? numDe(a, 0) : a.toFixed(1).replace(".", ",");
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${txt} %`;
};
const dateDe = (iso) => new Date(iso).toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
const yearsAgo = (iso) => {
  const days = (Date.now() - new Date(iso)) / 864e5;
  return days < 60 ? `vor ${Math.round(days)} Tagen` : days < 730 ? `vor ${Math.round(days / 30.4)} Monaten` : `vor ${numDe(days / 365.25)} Jahren`;
};

// Kennzahlen aus Tageskerzen (Binance): unabhängig vom im Chart gewählten Intervall eigens geladen,
// der Server hat sie ohnehin im Cache
const WEEKS52_DAYS = 365;
const VOL_DAYS = 30;
let statsCoin = null;     // Coin-Daten aus loadHero
let dailyCandles = null;  // Tageskerzen für 52 Wochen und Volatilität

// Annualisierte Volatilität: Standardabweichung der täglichen Log-Renditen × √365 (Krypto handelt täglich)
function volatility(closes) {
  const r = [];
  for (let i = 1; i < closes.length; i++) r.push(Math.log(closes[i] / closes[i - 1]));
  if (r.length < 2) return null;
  const mean = r.reduce((a, b) => a + b, 0) / r.length;
  const variance = r.reduce((a, b) => a + (b - mean) ** 2, 0) / (r.length - 1);
  return Math.sqrt(variance) * Math.sqrt(365);
}

// Gleitender Durchschnitt der Schlusskurse je Tag (null, solange noch nicht genug Kerzen)
function smaSeries(closes, n) {
  const out = new Array(closes.length).fill(null);
  let sum = 0;
  closes.forEach((c, i) => {
    sum += c;
    if (i >= n) sum -= closes[i - n];
    if (i >= n - 1) out[i] = sum / n;
  });
  return out;
}

function candleTiles() {
  if (!dailyCandles || dailyCandles.length < 2) return { w52: "", vol: "", dd: "", ma: "" };
  const d = dailyCandles;
  const price = d[d.length - 1].close;

  // 52 Wochen: Hoch und Tief der Tageskerzen (Dochte), bei jüngeren Coins seit Handelsstart
  const win = d.slice(-WEEKS52_DAYS);
  let hi = win[0], lo = win[0];
  for (const k of win) { if (k.high > hi.high) hi = k; if (k.low < lo.low) lo = k; }
  const pos = hi.high > lo.low ? Math.min(100, Math.max(0, (price - lo.low) / (hi.high - lo.low) * 100)) : 50;
  const day = (k) => fmtDate(k.time, { day: "2-digit", month: "2-digit", year: "2-digit", timeZone: "UTC" });
  const full = win.length >= WEEKS52_DAYS;
  const w52 = statTile("w52", full ? "Hoch / Tief 52 Wochen" : `Hoch / Tief seit Handelsstart (${win.length} T)`,
    `${usd(lo.low)} – ${usd(hi.high)}`,
    `<div class="det">${chgSpan(price / hi.high - 1)} vom Hoch (${day(hi)}) · ${chgSpan(price / lo.low - 1)} vom Tief (${day(lo)})</div>`
    + `<div class="range-bar" title="Aktueller Kurs innerhalb der 52-Wochen-Spanne"><div style="left:${pos}%"></div></div>`);

  // Volatilität 30 T, eingeordnet gegen den Durchschnitt der 30-T-Werte im letzten Jahr
  let vol = "";
  const closes = d.map((k) => k.close);
  const now = closes.length > VOL_DAYS ? volatility(closes.slice(-VOL_DAYS - 1)) : null;
  if (now != null) {
    const hist = [];
    for (let end = Math.max(VOL_DAYS + 1, closes.length - WEEKS52_DAYS); end <= closes.length; end++) {
      const v = volatility(closes.slice(end - VOL_DAYS - 1, end));
      if (v != null) hist.push(v);
    }
    const avg = hist.length >= 60 ? hist.reduce((a, b) => a + b, 0) / hist.length : null;
    const rel = avg ? now / avg : null;
    // Einordnung: Erfahrungswerte, ±25 % Abweichung vom eigenen Jahresdurchschnitt
    const word = rel == null ? "" : rel >= 1.25 ? "deutlich höher als üblich" : rel <= 0.75 ? "deutlich ruhiger als üblich" : "im üblichen Rahmen";
    const pct0 = (v) => numDe(v * 100, 0) + " %";
    vol = statTile("vol30", "Volatilität 30 T", pct0(now),
      `<div class="det">≈ ±${(now / Math.sqrt(365) * 100).toFixed(1).replace(".", ",")} % pro Tag`
      + (avg ? ` · Ø 1 J <b>${pct0(avg)}</b>, ${word}` : "") + "</div>");
  }
  // Größter Rückgang im letzten Jahr (Schlusskurse, Hoch → späteres Tief)
  const dayC = (i) => fmtDate(d[i].time, { day: "2-digit", month: "2-digit", year: "2-digit", timeZone: "UTC" });
  const start = Math.max(0, d.length - WEEKS52_DAYS);
  let peak = start, ddMax = 0, ddPeak = start, ddTrough = start;
  for (let i = start; i < d.length; i++) {
    if (closes[i] > closes[peak]) peak = i;
    const dd = closes[i] / closes[peak] - 1;
    if (dd < ddMax) { ddMax = dd; ddPeak = peak; ddTrough = i; }
  }
  const curDd = price / closes[peak] - 1;  // peak = höchster Schlusskurs im Zeitraum
  const recovered = ddTrough < d.length - 1 ? price / closes[ddTrough] - 1 : null;
  const dd = ddMax < 0 ? statTile("drawdown", full ? "Max. Rückgang 1 J" : "Max. Rückgang seit Start", chgSpan(ddMax),
    `<div class="det">${dayC(ddPeak)} → ${dayC(ddTrough)}${recovered != null ? ` · seither ${chgSpan(recovered)}` : ""}</div>`
    + `<div class="det">Aktuell ${curDd < 0 ? `${chgSpan(curDd)} unter dem Hoch (${dayC(peak)})` : "<b>auf dem Hoch</b>"}</div>`) : "";

  // Abstand zu MA50 und MA200 der Tages-Schlusskurse, dazu Lage der beiden zueinander
  let ma = "";
  const ma50 = smaSeries(closes, 50), ma200 = smaSeries(closes, 200);
  const last = closes.length - 1;
  if (ma50[last] != null) {
    const rows = [[50, ma50[last]], [200, ma200[last]]].filter(([, v]) => v != null)
      .map(([n, v]) => `<span>MA${n} ${usd(v)}</span>${chgSpan(price / v - 1)}`).join("");
    let cross = "";
    if (ma200[last] != null) {
      const above = ma50[last] > ma200[last];
      let i = last;
      while (i > 0 && ma200[i - 1] != null && (ma50[i - 1] > ma200[i - 1]) === above) i--;
      const since = ma200[i - 1] != null ? ` seit ${dayC(i)}` : "";
      cross = `<div class="det">MA50 ${above ? "über" : "unter"} MA200${since} (${above ? "„Golden Cross“" : "„Death Cross“"})</div>`;
    }
    ma = statTile("maDist", "Abstand zu den Durchschnitten", "",
      `<div class="ma-rows">${rows}</div>${cross}`).replace('<div class="val"></div>', "");
  }
  return { w52, vol, dd, ma };
}

function renderStats(c) {
  statsCoin = c;
  drawStats();
  renderFees();   // Bewertung braucht die Marktkap.
}

function drawStats() {
  const c = statsCoin;
  const st = c && c.data.market_stats;
  const mc = c && c.data.market_cap;
  const { w52, vol, dd, ma } = candleTiles();
  const tiles = [];

  if (st && st.ath != null) {
    tiles.push(statTile("ath", "Allzeithoch", usd(st.ath),
      `<div class="det">${st.ath_change != null ? `${chgSpan(st.ath_change)} darunter · ` : ""}${st.ath_date ? `${dateDe(st.ath_date)} (${yearsAgo(st.ath_date)})` : ""}</div>`));
  }
  if (st && st.atl != null) {
    tiles.push(statTile("atl", "Allzeittief", usd(st.atl),
      `<div class="det">${st.atl_change != null ? `${chgSpan(st.atl_change)} darüber · ` : ""}${st.atl_date ? dateDe(st.atl_date) : ""}</div>`));
  }
  if (w52) tiles.push(w52);
  if (st && st.low_24h != null && st.high_24h != null && st.high_24h > st.low_24h) {
    const price = c.data.price ?? (dailyCandles && dailyCandles.length ? dailyCandles[dailyCandles.length - 1].close : null);
    const pos = price != null ? Math.min(100, Math.max(0, (price - st.low_24h) / (st.high_24h - st.low_24h) * 100)) : null;
    tiles.push(statTile("range24", "Spanne 24 h", `${usd(st.low_24h)} – ${usd(st.high_24h)}`,
      `<div class="det">Schwankung ${pctSigned((st.high_24h - st.low_24h) / st.low_24h).replace("+", "")}</div>`
      + (pos != null ? `<div class="range-bar" title="Aktueller Kurs innerhalb der Spanne"><div style="left:${pos}%"></div></div>` : "")));
  }
  if (dd) tiles.push(dd);
  if (ma) tiles.push(ma);
  if (vol) tiles.push(vol);
  if (st) {
    const ch = st.change || {};
    tiles.push(statTile("changes", "Kursänderung", "",
      `<div class="changes"><span>24 h</span><span>7 T</span><span>30 T</span><span>1 J</span>${["24h", "7d", "30d", "1y"].map((k) => chgSpan(ch[k])).join("")}</div>`)
      .replace('<div class="val"></div>', ""));
  }
  if (st && st.volume_24h != null) {
    const turn = mc && mc.usd ? st.volume_24h / mc.usd : null;
    tiles.push(statTile("volume", "Handelsvolumen 24 h", usd6(st.volume_24h),
      turn != null ? `<div class="det">Umschlag <b>${(turn * 100).toFixed(1).replace(".", ",")} %</b> der Marktkap.</div>` : ""));
  }
  if (st && st.fdv != null && mc && mc.usd) {
    const ratio = mc.usd / st.fdv;
    tiles.push(statTile("fdv", "Voll verwässert (FDV)", usd6(st.fdv),
      `<div class="det">Marktkap. = <b>${numDe(Math.min(1, ratio) * 100, 0)} %</b> davon</div>`));
  }
  if (st && st.circulating_supply != null) {
    const max = st.max_supply || null;
    const share = max ? st.circulating_supply / max : null;
    const det = max
      ? `<div class="det"><b>${numDe(share * 100, 1)} %</b> von max. ${big(max)}</div><div class="meter"><div style="width:${Math.min(100, share * 100)}%"></div></div>`
      : `<div class="det">kein Maximum festgelegt${st.total_supply ? ` · ausgegeben ${big(st.total_supply)}` : ""}</div>`;
    tiles.push(statTile("supply", "Umlaufmenge", `${big(st.circulating_supply)} ${esc(c.symbol)}`, det));
  }
  $("stats-grid").innerHTML = tiles.join("");
  $("stats").hidden = !tiles.length;
  applyStatsOpen();
}

// Kennzahlen ein- und ausklappen, gemerkt im Browser
const STATS_KEY = "statsOpen";
let statsOpen = true;
try { statsOpen = localStorage.getItem(STATS_KEY) !== "0"; } catch (e) { /* ignorieren */ }

function applyStatsOpen() {
  $("stats-grid").hidden = !statsOpen;
  $("stats-toggle").setAttribute("aria-expanded", String(statsOpen));
  const n = $("stats-grid").children.length;
  $("stats-hint").textContent = statsOpen ? "" : `${n} Kennzahlen ausgeblendet`;
}

$("stats-toggle").addEventListener("click", () => {
  statsOpen = !statsOpen;
  try { localStorage.setItem(STATS_KEY, statsOpen ? "1" : "0"); } catch (e) { /* ignorieren */ }
  hideTip();
  applyStatsOpen();
});

// Übrige Karten ein- und ausklappen; eingeklappte Karten (IDs) werden im Browser gemerkt
const COLLAPSED_KEY = "collapsedCards";
let collapsedCards = new Set();
try { collapsedCards = new Set(JSON.parse(localStorage.getItem(COLLAPSED_KEY) || "[]")); } catch (e) { /* ignorieren */ }
// Lage und News sind bei jedem Öffnen der Seite erst einmal zu; Aufklappen gilt nur bis zum Neuladen (nicht gemerkt)
const START_COLLAPSED = new Set(["hero-lage", "hero-news"]);
const expandedNow = new Set();
const isCollapsed = (id) => (START_COLLAPSED.has(id) ? !expandedNow.has(id) : collapsedCards.has(id));
// Kopfzeilen: Karten, dazu Lage und News im Kopf (dort bleiben Kurs und Score immer sichtbar)
const CARD_HEAD = ":scope > .breakdown-head, :scope > .chart-head, :scope > .lage-head, :scope > .news-head";

// Pfeil in die Kopfzeile setzen (falls noch nicht da) und gemerkten Zustand anwenden
function setupCollapse(sec) {
  const head = sec.querySelector(CARD_HEAD);
  if (!head) { sec.classList.remove("collapsible", "collapsed"); return; }  // z. B. nur Statuszeile
  sec.classList.add("collapsible");
  head.classList.add("card-head");
  let btn = head.querySelector(".collapse-btn");
  if (!btn) {
    // Pfeil vor den Titel; ist die Kopfzeile selbst das Label (News), direkt hinein
    (head.classList.contains("label") ? head : head.firstElementChild).insertAdjacentHTML("afterbegin",
      '<button class="collapse-btn" type="button" title="Karte ein- oder ausklappen"><span class="chev" aria-hidden="true">▼</span></button>');
    btn = head.querySelector(".collapse-btn");
  }
  const open = !isCollapsed(sec.id);
  sec.classList.toggle("collapsed", !open);
  btn.setAttribute("aria-expanded", String(open));
}

// Klick auf Pfeil oder freie Fläche der Kopfzeile; Schalter, Links und Tooltips behalten ihre Funktion
document.addEventListener("click", (e) => {
  const head = e.target.closest(".collapsible > .card-head");
  if (!head) return;
  if (!e.target.closest(".collapse-btn") && e.target.closest("button, a, select, label, [data-tip]")) return;
  const sec = head.parentElement;
  if (START_COLLAPSED.has(sec.id)) {
    if (expandedNow.has(sec.id)) expandedNow.delete(sec.id); else expandedNow.add(sec.id);
  } else {
    if (collapsedCards.has(sec.id)) collapsedCards.delete(sec.id); else collapsedCards.add(sec.id);
    try { localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...collapsedCards])); } catch (e) { /* ignorieren */ }
  }
  hideTip();
  setupCollapse(sec);
});

["chart-card", "book", "fees", "derivs", "onchain", "liq", "history"].forEach((id) => setupCollapse($(id)));

async function loadDailyStats() {
  try {
    const res = await fetch(`api/klines?symbol=${encodeURIComponent(symbol)}&interval=1d`);
    if (!res.ok) return;  // kein Binance-Paar: nur die CoinGecko-Kennzahlen
    dailyCandles = (await res.json()).candles;
    drawStats();
  } catch (e) { /* ohne Kerzen-Kennzahlen weiter */ }
}

async function loadHero() {
  try {
    // Hauptcoins stecken im gecachten Dashboard-Payload, alle anderen im Coin-Cache des Servers
    let coin, updated;
    if (MAIN_SYMBOLS.includes(symbol)) {
      const res = await fetch("api/sentiment");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const p = await res.json();
      coin = p.coins.find((c) => c.symbol === symbol);
      updated = p.updated;
    } else {
      const res = await fetch("api/coin?symbol=" + encodeURIComponent(symbol) + (coinId ? "&id=" + encodeURIComponent(coinId) : ""));
      if (!res.ok) throw new Error("HTTP " + res.status);
      coin = await res.json();
    }
    if (!coin) throw new Error("keine Daten");
    renderHero(coin);
    // Name von CoinGecko, sonst aus der Coin-ID abgeleitet (z. B. "avalanche-2" → "avalanche")
    const name = coin.data.name || coinId.replace(/-\d+$/, "").replace(/-/g, " ");
    loadNews(name);
    setInterval(() => loadNews(name), NEWS_REFRESH_MS);
    $("updated").textContent = updated ? "Score-Stand: " + new Date(updated).toLocaleString("de-DE") : "";
  } catch (e) {
    $("hero").className = "card status error";
    $("hero").textContent = `${symbol} konnte nicht geladen werden (${e.message}). Läuft server.py?`;
    $("updated").textContent = "";
    $("breakdown").innerHTML = '<div class="status error">Score nicht verfügbar.</div>';
  }
}

// Detailseite: Kopf (Kurs, Lagebild, News), Kennzahlen, Zusammensetzung des Scores. Alle Karten sind immer offen.
// Gemeinsame Helfer in coin-base.js.

// --- Kopfbereich ---------------------------------------------------------

// Lagebild unter Kurs und Score: Einordnung, ein Satz zur Lage, Empfehlung, Für und Wider (kommt fertig vom Server,
// siehe situation() in sentiment.py)
const MOOD_TONE = { fear: "tone-cold", greed: "tone-hot", neutral: "tone-neutral" };

function lageBlock(c) {
  const s = c.situation;
  if (!s) return "";
  const list = (items) => items.length ? items.map((x) => `<li>${esc(x)}</li>`).join("") : `<li class="none">${L("nichts Ausgeprägtes", "nothing pronounced")}</li>`;
  // die zwei Signale mit dem größten Anteil am Score (Punkte × Gewicht / Summe der Gewichte), nicht den meisten Rohpunkten
  const sumW = c.signals.reduce((a, x) => a + (x.weight ?? 1), 0) || 1;
  const share = (x) => x.points * (x.weight ?? 1) / sumW;
  const drivers = [...c.signals].sort((a, b) => Math.abs(share(b)) - Math.abs(share(a))).slice(0, 2)
    .map((x) => `${esc(signalName(x.name))} <b>${num1(share(x))}</b>`).join(", ");
  return `
    <div class="lage" id="hero-lage">
      <div class="lage-head">
        <span class="label"><span${tipAttr("situation")}>${L("Lage", "Situation")}</span></span>
        <span class="verdict ${MOOD_TONE[s.mood] || "tone-neutral"}">${esc(s.title)}</span>
      </div>
      <div class="lage-sum">${esc(s.summary)}</div>
      <div class="lage-text">${esc(s.text)}</div>
      ${s.action ? `<div class="lage-action act-${esc(s.action_tone || "wait")}">
        <div><span class="label"><span${tipAttr("action")}>${L("Empfehlung", "Suggestion")}</span></span> <span class="act">${esc(s.action)}</span></div>
        <div class="why">${esc(s.action_text)}</div>
      </div>` : ""}
      <div class="lage-cols">
        <div class="pro"><div class="label">${L("Für steigende Kurse spricht", "In favour of rising prices")}</div><ul>${list(s.pro)}</ul></div>
        <div class="contra"><div class="label">${L("Dagegen spricht", "Against")}</div><ul>${list(s.contra)}</ul></div>
      </div>
      <div class="lage-foot">${drivers ? `${L("Größter Anteil am Score:", "Largest share of the score:")} ${drivers} ${L("Punkte", "points")} · ` : ""}${L("Einordnung nach Erfahrungswerten.", "Assessment based on rules of thumb.")}</div>
    </div>`;
}

// News zum Coin: Schlagzeilen deutschsprachiger Krypto-Medien, vom Server aus RSS-Feeds gefiltert
const NEWS_REFRESH_MS = 15 * 60 * 1000;   // wie NEWS_CACHE_SECONDS in server.py
let newsData = null, newsName = "";

const ago = (t) => {
  const min = Math.max(0, (Date.now() / 1000 - t) / 60);
  if (EN) return min < 60 ? `${Math.round(min)} min ago` : min < 48 * 60 ? `${Math.round(min / 60)} h ago` : `${Math.round(min / 1440)} days ago`;
  return min < 60 ? `vor ${Math.round(min)} Min.` : min < 48 * 60 ? `vor ${Math.round(min / 60)} Std.` : `vor ${Math.round(min / 1440)} Tagen`;
};

function renderNews() {
  const el = $("hero-news");
  if (!el) return;
  const items = newsData ? newsData.items : [];
  const who = esc(newsName || symbol);
  el.hidden = !items.length;
  el.innerHTML = !items.length ? "" : `
    <div class="label news-head"><span${tipAttr("news")}>${newsData.general ? L(`Keine aktuellen Meldungen zu ${who} – neueste Krypto-News`, `No recent news on ${who} – latest crypto news`) : L(`News zu ${who}`, `News on ${who}`)}</span></div>
    ${dataStatus({ src: L("RSS-Feeds", "RSS feeds"), kind: "poll", at: newsData.fetched_at || newsData.updated, every: NEWS_REFRESH_MS / 1000, small: true, quiet: true })}
    <ul>${items.map((i) => `<li><a href="${esc(i.link)}" target="_blank" rel="noopener noreferrer" title="${esc(i.teaser)}">${esc(i.title)}</a>`
      + `<span class="meta">${esc(i.source)} · ${ago(i.time)}</span></li>`).join("")}</ul>`;
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
  const changes = [["24 h", ch["24h"]], [L("7 T", "7 d"), ch["7d"]]].filter(([, v]) => v != null)
    .map(([label, v]) => `<span>${label} ${change(v)}</span>`).join("");
  const facts = [
    mc ? `${L("Marktkap.", "Market cap")} ${usd6(mc.usd)}${mc.rank ? ` · ${L("Rang", "Rank")} ${mc.rank}` : ""}` : L("Marktkap. n/a", "Market cap n/a"),
    st.ath_change == null ? "" : st.ath_change < 0 ? `<b>${pct(-st.ath_change).replace("+", "")}</b> ${L("unter dem Allzeithoch", "below the all-time high")}` : `<b>${L("auf dem Allzeithoch", "at the all-time high")}</b>`,
    fng ? `Fear &amp; Greed <b>${fng.value}</b> (${fngZone(fng.value).name})` : "",
  ].filter(Boolean).join(" · ");
  const coinName = c.data.name || symbol;
  // Überschrift mit Namen, das Kürzel daneben (vorher nur „BTC“)
  if (c.data.name && c.data.name.toUpperCase() !== symbol) {
    $("title").innerHTML = `${esc(c.data.name)} <span class="title-sym">${esc(symbol)}</span>`;
    document.title = `${c.data.name} (${symbol}) – ${L("Details", "Details")}`;
  }
  setCoinCrumbs(coinName);
  $("title-star").innerHTML = watchStar({ id: coinId || c.data.coin_id, symbol, name: c.data.name });
  const aboutHref = "about.html?symbol=" + encodeURIComponent(symbol) + (coinId ? "&id=" + encodeURIComponent(coinId) : "");
  $("hero").className = "card hero";
  $("hero").innerHTML = `
    <div>
      <a class="about-link" href="${esc(aboutHref)}" title="${L("Beschreibung, Kategorie, Fakten und Einschätzung", "Description, category, facts and assessment")}">${L(`Was ist ${esc(coinName)}? Profil &amp; Einschätzung →`, `What is ${esc(coinName)}? Profile &amp; assessment →`)}</a>
      <div class="price-line">
        <div class="price" id="hero-price">${ma ? usd(ma.price) : c.data.price != null ? usd(c.data.price) : "–"}</div>
        ${changes ? `<div class="chg">${changes}</div>` : ""}
      </div>
      <div class="mcap">${facts}</div>
      <div class="ds-line"><span class="ds" id="ds-price"></span><span class="ds" id="ds-score"></span></div>
    </div>
    <div class="score-box">
      <div class="label">${L("Stimmungs-Score", "Sentiment score")}</div>
      <div class="score">${signed(c.score)}</div>
      <div class="verdict ${tone(c.score)}" title="${esc(c.verdict)}">${zoneName(c.score)}</div>
      <div class="track" id="score-track"></div>
    </div>
    ${lageBlock(c)}
    <div class="news" id="hero-news" hidden></div>`;
  setDataStatus("ds-score", { src: L("Score: Server (mehrere Quellen)", "Score: server (several sources)"), kind: "poll", at: scoreAt, every: 300,
    note: L("Quellen je Signal stehen in der Score-Zusammensetzung", "Sources per signal are listed in the score breakdown") });
  if (typeof chartStatus === "function") chartStatus();
  renderCoinBrief(c);
  renderNews();
  fillHeroPrice();  // falls das Chart schon vor dem Kopf geladen war
  renderStats(c);
  renderBreakdown(c);
  renderTrack();   // Trefferquote, falls der Score-Verlauf schon geladen ist
  if (typeof syncTodayScore === "function") syncTodayScore();
}

// Kurzfassung über den Reitern (in jedem Reiter sichtbar): Lage in einem Satz und Empfehlung aus dem Lagebild
function renderCoinBrief(c) {
  const s = c.situation;
  if (!s) { renderBrief($("brief"), null); return; }
  // knapp: Lage, Score und Empfehlung in einer Zeile; die Begründung steht im Lagebild der Übersicht
  renderBrief($("brief"), {
    title: `${esc(s.title)} · Score ${signed(c.score)}${s.action ? ` · ${L("Empfehlung", "Suggestion")}: ${esc(s.action)}` : ""}` + whyTip("situation"),
    tone: s.action_tone || "neutral",
  });
}

// Quelle und Art je Signal (Schlüssel = Signalname vom Server); Tageswerte mit eigenem Datum, sonst Stand des Scores
const SIGNAL_SRC = {
  "Fear & Greed": "alternative.me", "Funding Rate": "Binance Futures", "Open Interest": "Binance Futures",
  "Preis vs. MA200": L("Binance Tageskerzen", "Binance daily candles"), "RSI (14)": L("Binance Tageskerzen", "Binance daily candles"),
  "Long/Short Ratio": "Binance Futures", "Optionsvola (DVOL)": "Deribit", "Momentum 7T": "CoinGecko", "Momentum 30T": "CoinGecko",
  "Umschlag": "CoinGecko", "RSI (14 Std.)": "CoinGecko", "MVRV": "Coin Metrics",
};
let scoreAt = null, scoreCoin = null;
function signalStatus(rawName) {
  const src = SIGNAL_SRC[rawName];
  if (!src) return "";
  const day = (t) => (t ? new Date(t * 1000).toISOString().slice(0, 10) : null);
  if (rawName === "Fear & Greed") return dataStatus({ src, kind: "daily", day: day(scoreCoin && scoreCoin.fear_greed && scoreCoin.fear_greed.time), lag: 0, small: true, quiet: true });
  if (rawName === "MVRV") return dataStatus({ src, kind: "daily", day: day(scoreCoin && scoreCoin.data.mvrv && scoreCoin.data.mvrv.time), lag: 1, small: true, quiet: true });
  return dataStatus({ src, kind: "poll", at: scoreAt, every: 300, small: true, quiet: true });
}

function bdRow(name, reason, points, share, total = false, n = 1, weight = 1, rawName = "") {
  const w = Math.min(100, Math.abs(points)) / 2;  // halbe Spurbreite = 100 Punkte
  const bar = points === 0 ? "" : `<div class="bd-bar ${points > 0 ? "pos" : "neg"}" style="width:${w}%"></div>`;
  // Tooltip: allgemeine Erklärung (tips.js) plus Einordnung des aktuellen Werts
  const key = total ? "score" : SIGNAL_TIPS[name];
  const now = total
    ? L(`Aktuell: ${signed(points)} = gewichteter Durchschnitt aus ${n} Signalen.`, `Now: ${signed(points)} = weighted average of ${n} signals.`)
    : L(`Aktuell: ${reason} → ${signed(points)} Punkte, Gewicht ${weight.toLocaleString(LOCALE)}. Das macht ${num1(share)} im Score aus.`,
      `Now: ${reason} → ${signed(points)} points, weight ${weight.toLocaleString(LOCALE)}. That accounts for ${num1(share)} of the score.`);
  const tip = key && TIPS[key] ? ` data-tip="${key}" data-tip-now="${esc(now)}" tabindex="0"` : "";
  return `
    <div class="bd-row${total ? " total" : ""}">
      <div class="bd-label"><div class="bd-name"><span${tip}>${esc(name)}</span></div>${reason ? `<div class="bd-reason">${esc(reason)}</div>` : ""}${rawName ? `<div class="bd-src">${signalStatus(rawName)}</div>` : ""}</div>
      <div class="bd-track" role="img" aria-label="${esc(name)}: ${signed(points)} ${L("von −100 bis +100", "from −100 to +100")}">${bar}</div>
      <div class="bd-val">${signed(points)}${share != null ? `<span class="share">${num1(share)} ${L("im Score", "in score")}</span>` : ""}</div>
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
      <div class="label">${L("Zusammensetzung des Stimmungs-Scores", "Breakdown of the sentiment score")}${whyTip("breakdown")}</div>
      <div class="note">${n} ${L("Signale · Score = gewichteter Durchschnitt (Trend und Optionsvola zählen mehr, Fear &amp; Greed und RSI weniger)", "signals · score = weighted average (trend and options volatility count more, Fear &amp; Greed and RSI less)")} · <a href="methodology.html#signals">${L("Methodik", "Methodology")} →</a></div>
    </div>
    ${c.signals.map((s) => bdRow(signalName(s.name) + (w(s) !== 1 ? ` ×${w(s).toLocaleString(LOCALE)}` : ""), s.reason, s.points, s.points * w(s) / sumW, false, n, w(s), s.name)).join("")}
    ${bdRow(L("Stimmungs-Score", "Sentiment score"), c.verdict, c.score, null, true, n)}
    <div class="bd-axis"><span></span><div class="ticks"><span>−100 ${L("Angst", "Fear")}</span><span>0</span><span>${L("Gier", "Greed")} +100</span></div><span></span></div>`;
}

// Kennzahlen aus den CoinGecko-Marktdaten (kommen mit dem Score, keine eigene Abfrage)
const big = (v) => v.toLocaleString(LOCALE, { notation: "compact", compactDisplay: EN ? "short" : "long", maximumSignificantDigits: 4 })
  .replace(/ Millionen?/, " Mio.").replace(/ Milliarden?/, " Mrd.").replace(/ Billionen?/, " Bio.");
const pctSigned = (v) => {
  const a = Math.abs(v * 100);
  const txt = a >= 1000 ? numDe(a, 0) : a.toFixed(1).replace(".", DEC);
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${txt}${PCT}`;
};
const dateDe = (iso) => new Date(iso).toLocaleDateString(LOCALE, { day: "2-digit", month: "2-digit", year: "numeric" });
const yearsAgo = (iso) => {
  const days = (Date.now() - new Date(iso)) / 864e5;
  if (EN) return days < 60 ? `${Math.round(days)} days ago` : days < 730 ? `${Math.round(days / 30.4)} months ago` : `${numDe(days / 365.25)} years ago`;
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
  const w52 = statTile("w52", full ? L("Hoch / Tief 52 Wochen", "High / low 52 weeks") : L(`Hoch / Tief seit Handelsstart (${win.length} T)`, `High / low since listing (${win.length} d)`),
    `${usd(lo.low)} – ${usd(hi.high)}`,
    `<div class="det">${chgSpan(price / hi.high - 1)} ${L("vom Hoch", "from high")} (${day(hi)}) · ${chgSpan(price / lo.low - 1)} ${L("vom Tief", "from low")} (${day(lo)})</div>`
    + `<div class="range-bar" title="${L("Aktueller Kurs innerhalb der 52-Wochen-Spanne", "Current price within the 52-week range")}"><div style="left:${pos}%"></div></div>`);

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
    const word = rel == null ? "" : rel >= 1.25 ? L("deutlich höher als üblich", "much higher than usual") : rel <= 0.75 ? L("deutlich ruhiger als üblich", "much calmer than usual") : L("im üblichen Rahmen", "within the usual range");
    const pct0 = (v) => numDe(v * 100, 0) + PCT;
    vol = statTile("vol30", L("Volatilität 30 T", "Volatility 30 d"), pct0(now),
      `<div class="det">≈ ±${(now / Math.sqrt(365) * 100).toFixed(1).replace(".", DEC)}${PCT} ${L("pro Tag", "per day")}`
      + (avg ? ` · ${L("Ø 1 J", "avg 1 y")} <b>${pct0(avg)}</b>, ${word}` : "") + "</div>");
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
  const dd = ddMax < 0 ? statTile("drawdown", full ? L("Max. Rückgang 1 J (Schlusskurse)", "Max. drawdown 1 y (closes)") : L("Max. Rückgang seit Start (Schlusskurse)", "Max. drawdown since listing (closes)"), chgSpan(ddMax),
    `<div class="det">${dayC(ddPeak)} → ${dayC(ddTrough)}${recovered != null ? ` · ${L("seither", "since then")} ${chgSpan(recovered)}` : ""}</div>`
    + `<div class="det">${L("Aktuell", "Now")} ${curDd < 0 ? `${chgSpan(curDd)} ${L("unter dem Hoch", "below the high")} (${dayC(peak)})` : `<b>${L("auf dem Hoch", "at the high")}</b>`}</div>`) : "";

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
      const since = ma200[i - 1] != null ? ` ${L("seit", "since")} ${dayC(i)}` : "";
      // Kreuz ist träge: liegt der Kurs schon auf der anderen Seite beider Linien, dazuschreiben
      const below = price < ma50[last] && price < ma200[last], over = price > ma50[last] && price > ma200[last];
      const caveat = above && below ? L(" – Kurs aber unter beiden Linien", " – but price below both lines")
        : !above && over ? L(" – Kurs aber über beiden Linien", " – but price above both lines") : "";
      cross = `<div class="det">MA50 ${above ? L("über", "above") : L("unter", "below")} MA200${since} (${above ? L("„Golden Cross“", "“golden cross”") : L("„Death Cross“", "“death cross”")})${caveat}</div>`;
    }
    ma = statTile("maDist", L("Abstand zu den Durchschnitten", "Distance to moving averages"), "",
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
    tiles.push(statTile("ath", L("Allzeithoch", "All-time high"), usd(st.ath),
      `<div class="det">${st.ath_change != null ? `${chgSpan(st.ath_change)} ${L("darunter", "below")} · ` : ""}${st.ath_date ? `${dateDe(st.ath_date)} (${yearsAgo(st.ath_date)})` : ""}</div>`));
  }
  if (st && st.atl != null) {
    tiles.push(statTile("atl", L("Allzeittief", "All-time low"), usd(st.atl),
      `<div class="det">${st.atl_change != null ? `${chgSpan(st.atl_change)} ${L("darüber", "above")} · ` : ""}${st.atl_date ? dateDe(st.atl_date) : ""}</div>`));
  }
  if (w52) tiles.push(w52);
  if (st && st.low_24h != null && st.high_24h != null && st.high_24h > st.low_24h) {
    const price = c.data.price ?? (dailyCandles && dailyCandles.length ? dailyCandles[dailyCandles.length - 1].close : null);
    const pos = price != null ? Math.min(100, Math.max(0, (price - st.low_24h) / (st.high_24h - st.low_24h) * 100)) : null;
    tiles.push(statTile("range24", L("Spanne 24 h", "Range 24 h"), `${usd(st.low_24h)} – ${usd(st.high_24h)}`,
      `<div class="det">${L("Schwankung", "Swing")} ${pctSigned((st.high_24h - st.low_24h) / st.low_24h).replace("+", "")}</div>`
      + (pos != null ? `<div class="range-bar" title="${L("Aktueller Kurs innerhalb der Spanne", "Current price within the range")}"><div style="left:${pos}%"></div></div>` : "")));
  }
  if (dd) tiles.push(dd);
  if (ma) tiles.push(ma);
  if (vol) tiles.push(vol);
  if (st) {
    const ch = st.change || {};
    tiles.push(statTile("changes", L("Kursänderung", "Price change"), "",
      // je Zeile Zeitraum | Änderung (vier Spalten nebeneinander passten nicht in die Kachel)
      `<div class="changes">${[["24h", "24 h"], ["7d", L("7 T", "7 d")], ["30d", L("30 T", "30 d")], ["1y", L("1 J", "1 y")]]
        .map(([k, label]) => `<span>${label}</span>${chgSpan(ch[k])}`).join("")}</div>`)
      .replace('<div class="val"></div>', ""));
  }
  if (st && st.volume_24h != null) {
    const turn = mc && mc.usd ? st.volume_24h / mc.usd : null;
    tiles.push(statTile("volume", L("Handelsvolumen 24 h", "Trading volume 24 h"), usd6(st.volume_24h),
      turn != null ? `<div class="det">${L("Umschlag", "Turnover")} <b>${(turn * 100).toFixed(1).replace(".", DEC)}${PCT}</b> ${L("der Marktkap.", "of market cap")}</div>` : ""));
  }
  if (st && st.fdv != null && mc && mc.usd) {
    const ratio = mc.usd / st.fdv;
    tiles.push(statTile("fdv", L("Voll verwässert (FDV)", "Fully diluted (FDV)"), usd6(st.fdv),
      `<div class="det">${L("Marktkap.", "Market cap")} = <b>${numDe(Math.min(1, ratio) * 100, 0)}${PCT}</b> ${L("davon", "of it")}</div>`));
  }
  if (st && st.circulating_supply != null) {
    const max = st.max_supply || null;
    const share = max ? st.circulating_supply / max : null;
    const det = max
      ? `<div class="det"><b>${numDe(share * 100, 1)}${PCT}</b> ${L("von max.", "of max.")} ${big(max)}</div><div class="meter"><div style="width:${Math.min(100, share * 100)}%"></div></div>`
      : `<div class="det">${L("kein Maximum festgelegt", "no maximum set")}${st.total_supply ? ` · ${L("ausgegeben", "issued")} ${big(st.total_supply)}` : ""}</div>`;
    tiles.push(statTile("supply", L("Umlaufmenge", "Circulating supply"), `${big(st.circulating_supply)} ${esc(c.symbol)}`, det));
  }
  $("stats-grid").innerHTML = tiles.join("")
    + `<div class="ds-line stats-ds">${dataStatus({ src: L("Marktdaten: CoinGecko", "Market data: CoinGecko"), kind: "poll", at: scoreAt, every: 300 })}`
    + (dailyCandles ? dataStatus({ src: L("Kerzen-Kennzahlen: Binance", "Candle figures: Binance"), kind: "daily", day: new Date(dailyCandles[dailyCandles.length - 1].time * 1000).toISOString().slice(0, 10), lag: 0 }) : "")
    + `</div>`;
  $("stats").hidden = !tiles.length;
  if (typeof updateLevels === "function") updateLevels();  // Kursmarken im Chart (coin-chart.js)
}

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
    let coin, updated, fetchedAt;
    if (MAIN_SYMBOLS.includes(symbol)) {
      const res = await fetch("api/sentiment");
      if (!res.ok) throw new Error("HTTP " + res.status);
      const p = await res.json();
      coin = p.coins.find((c) => c.symbol === symbol);
      updated = p.updated;
      fetchedAt = p.fetched_at;
    } else {
      const res = await fetch("api/coin?symbol=" + encodeURIComponent(symbol) + (coinId ? "&id=" + encodeURIComponent(coinId) : ""));
      if (!res.ok) throw new Error("HTTP " + res.status);
      coin = await res.json();
    }
    if (!coin) throw new Error(L("keine Daten", "no data"));
    scoreAt = coin.fetched_at || fetchedAt || updated || Date.now();
    scoreCoin = coin;
    renderHero(coin);
    // Name von CoinGecko, sonst aus der Coin-ID abgeleitet (z. B. "avalanche-2" → "avalanche")
    const name = coin.data.name || coinId.replace(/-\d+$/, "").replace(/-/g, " ");
    loadNews(name);
    setInterval(() => loadNews(name), NEWS_REFRESH_MS);
    $("updated").textContent = updated ? L("Score-Stand: ", "Score as of: ") + new Date(updated).toLocaleString(LOCALE) : "";
  } catch (e) {
    $("hero").className = "card status error";
    $("hero").textContent = L(`${symbol} konnte nicht geladen werden (${e.message}). Läuft server.py?`, `${symbol} could not be loaded (${e.message}). Is server.py running?`);
    $("updated").textContent = "";
    $("breakdown").innerHTML = `<div class="status error">${L("Score nicht verfügbar.", "Score not available.")}</div>`;
  }
}

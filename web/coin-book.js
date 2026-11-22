// Detailseite: Orderbuch live von Binance – Tiefenchart, Aufträge je Preisstufe, Spread, Ungleichgewicht, Wände.
// Gemeinsame Helfer in coin-base.js.
//
// Ablauf nach Binance-Vorgabe („lokales Orderbuch“): WebSocket mit den Änderungen öffnen und puffern,
// dann eine Momentaufnahme per REST holen, gepufferte Änderungen ab deren lastUpdateId anwenden und
// danach jede Änderung. Reißt die Folge ab (Lücke in den Update-IDs), wird neu synchronisiert.
// Alles läuft direkt im Browser (Binance erlaubt CORS); das Gewicht zählt für die IP des Nutzers.
//
// Die Momentaufnahme reicht nur so weit wie ihre Stufen (Spot 5.000, Futures 1.000 je Seite). Was
// außerhalb liegt, ist unvollständig und wird nicht mitgezählt (book.floor / book.ceil).

const BOOK_MARKETS = {
  spot: {
    name: "Binance Spot",
    rest: (pair) => `https://api.binance.com/api/v3/depth?symbol=${pair}&limit=5000`,
    ws: (pair) => `wss://stream.binance.com:9443/ws/${pair.toLowerCase()}@depth@1000ms`,
    limit: 5000,
  },
  futures: {
    name: "Binance Futures",
    rest: (pair) => `https://fapi.binance.com/fapi/v1/depth?symbol=${pair}&limit=1000`,
    ws: (pair) => `wss://fstream.binance.com/public/ws/${pair.toLowerCase()}@depth@500ms`,
    limit: 1000,
  },
};
const BOOK_MARKET_KEY = "bookMarket", BOOK_RANGE_KEY = "bookRange";
const BOOK_DRAW_MS = 1000;            // höchstens so oft neu zeichnen
const BOOK_RESNAP_MS = 5 * 60 * 1000; // Momentaufnahme regelmäßig erneuern (Reichweite wandert mit dem Kurs)
const BOOK_RETRY_MS = 5000;
const BOOK_ROWS = 10;                 // Stufen je Seite in der Tabelle
const BOOK_WALL_STEP = 0.001;         // Wände: Aufträge in Stufen von 0,1 % des Kurses zusammengefasst

let bookMarket = "spot", bookRange = 0.01, bookGroup = null;
try {
  if (localStorage.getItem(BOOK_MARKET_KEY) === "futures") bookMarket = "futures";
  const r = +localStorage.getItem(BOOK_RANGE_KEY);
  if ([0.005, 0.01, 0.02].includes(r)) bookRange = r;
} catch (e) { /* ignorieren */ }

let book = null;            // { bids: Map(Preis-Text → Menge), asks: Map, lastU, floor, ceil }
let bookSocket = null, bookBuffer = [], bookToken = 0, bookDirty = false;
let bookDrawTimer = null, bookRetryTimer = null, bookResnapTimer = null;
let bookAvail = { spot: null, futures: null };   // null = unbekannt, false = kein Paar
let bookView = null;        // zuletzt gezeichnete Werte, für die Maus-Anzeige im Tiefenchart

const bookPair = () => symbol + "USDT";
// kurz: „$51,2 Mio.“, „$798 Tsd.“, „$412“
const bookUsd = (v) => {
  const f = (x, d) => x.toLocaleString(LOCALE, { maximumFractionDigits: d });
  return "$" + (v >= 1e9 ? f(v / 1e9, 1) + L(" Mrd.", "B") : v >= 1e6 ? f(v / 1e6, 1) + L(" Mio.", "M") : v >= 1e3 ? f(v / 1e3, 0) + L(" Tsd.", "K") : f(v, 0));
};
let bookOff = false;        // kein Paar auf Binance: Karte zeigt nur noch den Hinweis

// --- Verbindung und Synchronisierung --------------------------------------

function closeBook() {
  bookToken++;
  if (bookSocket) { bookSocket.onclose = null; bookSocket.close(); bookSocket = null; }
  clearTimeout(bookRetryTimer); clearTimeout(bookResnapTimer);
  bookBuffer = [];
  setBookLive(false);
}

function setBookLive(on) { if (!bookOff) $("book-live").classList.toggle("on", on); }

function bookMessage(text) {
  $("book-msg").textContent = text;
  $("book-msg").hidden = !text;
}

async function connectBook() {
  closeBook();
  if (bookOff || document.hidden || typeof WebSocket === "undefined") return;
  const token = bookToken, m = BOOK_MARKETS[bookMarket], pair = bookPair();
  $("book-src").textContent = m.name + " · " + symbol + "/USDT";
  let ws;
  try { ws = new WebSocket(m.ws(pair)); } catch (e) { return scheduleBookRetry(token); }
  bookSocket = ws;
  ws.onmessage = (ev) => {
    if (token !== bookToken) return;
    let d;
    try { d = JSON.parse(ev.data); } catch (e) { return; }
    if (d.e !== "depthUpdate") return;
    if (!book) { bookBuffer.push(d); return; }
    applyBookEvent(d, token);
  };
  ws.onclose = () => { if (token === bookToken) { setBookLive(false); scheduleBookRetry(token); } };
  await new Promise((r) => { ws.onopen = r; setTimeout(r, 3000); });
  if (token === bookToken) await snapshotBook(token);
}

async function snapshotBook(token) {
  const m = BOOK_MARKETS[bookMarket];
  try {
    const res = await fetch(m.rest(bookPair()));
    const d = await res.json().catch(() => ({}));
    if (token !== bookToken) return;
    // Paar gibt es auf diesem Markt nicht (400) oder es ist leer (z. B. delistet, aber noch abfragbar)
    if (res.status === 400 || (res.ok && d.bids && !d.bids.length && !d.asks.length)) {
      bookAvail[bookMarket] = false;
      updateBookMarketButtons();
      const other = bookMarket === "spot" ? "futures" : "spot";
      if (bookAvail[other] !== false) { bookMarket = other; updateBookMarketButtons(); connectBook(); return; }
      closeBook();
      bookOff = true;
      $("book").innerHTML = `<div class="status">${L(`Für ${esc(symbol)} gibt es kein USDT-Paar auf Binance – daher kein Orderbuch.`, `There is no USDT pair for ${esc(symbol)} on Binance – so no order book.`)}</div>`;
      return;
    }
    if (!res.ok || !d.bids) throw new Error(d.msg || "HTTP " + res.status);
    bookAvail[bookMarket] = true;
    updateBookMarketButtons();
    const bids = new Map(d.bids.map(([p, q]) => [p, +q])), asks = new Map(d.asks.map(([p, q]) => [p, +q]));
    // Reichweite: volle Seite (weniger Stufen als angefragt) = ganzes Buch, sonst bis zur letzten Stufe
    const floor = d.bids.length < m.limit ? 0 : +d.bids[d.bids.length - 1][0];
    const ceil = d.asks.length < m.limit ? Infinity : +d.asks[d.asks.length - 1][0];
    book = { bids, asks, lastU: d.lastUpdateId, floor, ceil, synced: false };
    const buffered = bookBuffer; bookBuffer = [];
    for (const e of buffered) applyBookEvent(e, token);
    bookDirty = true; drawBookSoon();
    bookResnapTimer = setTimeout(() => { if (token === bookToken) { book = null; snapshotBook(token); } }, BOOK_RESNAP_MS);
  } catch (e) {
    if (token !== bookToken) return;
    if (!book) bookMessage(L(`Orderbuch nicht verfügbar (${e.message}).`, `Order book not available (${e.message}).`));
    scheduleBookRetry(token);
  }
}

function applyBookEvent(e, token) {
  if (!book || token !== bookToken) return;
  if (e.u <= book.lastU) return;   // schon in der Momentaufnahme enthalten
  const spot = bookMarket === "spot";
  if (!book.synced) {
    // erste passende Änderung: muss an die Momentaufnahme anschließen
    if (spot ? e.U > book.lastU + 1 : e.U > book.lastU) return resyncBook(token);
    book.synced = true;
  } else if (spot ? e.U !== book.lastU + 1 : e.pu !== book.lastU) {
    return resyncBook(token);    // Lücke: neu synchronisieren
  }
  for (const [p, q] of e.b) { if (+q === 0) book.bids.delete(p); else book.bids.set(p, +q); }
  for (const [p, q] of e.a) { if (+q === 0) book.asks.delete(p); else book.asks.set(p, +q); }
  book.lastU = e.u;
  setBookLive(true);
  bookDirty = true; drawBookSoon();
}

function resyncBook(token) {
  book = null;
  clearTimeout(bookResnapTimer);
  snapshotBook(token);
}

function scheduleBookRetry(token) {
  clearTimeout(bookRetryTimer);
  bookRetryTimer = setTimeout(() => { if (token === bookToken) connectBook(); }, BOOK_RETRY_MS);
}

function drawBookSoon() {
  if (!bookDrawTimer) bookDrawTimer = setTimeout(() => { bookDrawTimer = null; if (bookDirty) drawBook(); }, BOOK_DRAW_MS);
}

// --- Auswertung -----------------------------------------------------------

// Seiten als sortierte Listen [Preis, Menge], nur innerhalb der Reichweite der Momentaufnahme
function bookSides() {
  const bids = [], asks = [];
  for (const [p, q] of book.bids) { const v = +p; if (v >= book.floor) bids.push([v, q]); }
  for (const [p, q] of book.asks) { const v = +p; if (v <= book.ceil) asks.push([v, q]); }
  bids.sort((a, b) => b[0] - a[0]);
  asks.sort((a, b) => a[0] - b[0]);
  return { bids, asks };
}

// „Runde“ Schrittweite: 1, 2 oder 5 × 10^n, höchstens x
function niceStep(x) {
  const e = 10 ** Math.floor(Math.log10(x)), f = x / e;
  return (f >= 5 ? 5 : f >= 2 ? 2 : 1) * e;
}
const stepDecimals = (s) => Math.max(0, -Math.floor(Math.log10(s) + 1e-9));

// Aufträge in Preisstufen zusammenfassen (Bids abrunden, Asks aufrunden), Ergebnis [Preis, Menge, USD]
function groupSide(levels, step, down) {
  const out = new Map();
  for (const [p, q] of levels) {
    const k = (down ? Math.floor(p / step + 1e-9) : Math.ceil(p / step - 1e-9)) * step;
    const g = out.get(k) || [k, 0, 0];
    g[1] += q; g[2] += p * q;
    out.set(k, g);
  }
  return [...out.values()];
}

function setupBookGroups(mid) {
  const steps = [1e-5, 1e-4, 1e-3, 1e-2].map((f) => niceStep(mid * f));
  const sel = $("book-group"), prev = bookGroup;
  if (sel.dataset.steps === steps.join()) return;
  sel.dataset.steps = steps.join();
  sel.innerHTML = steps.map((s) => `<option value="${s}">${s.toLocaleString(LOCALE, { maximumFractionDigits: 10 })}</option>`).join("");
  bookGroup = prev && steps.includes(prev) ? prev : steps[1];
  sel.value = String(bookGroup);
}

// --- Darstellung ----------------------------------------------------------

function drawBook() {
  bookDirty = false;
  if (!book) return;
  const { bids, asks } = bookSides();
  if (!bids.length || !asks.length) { bookMessage(L("Orderbuch ist leer.", "Order book is empty.")); return; }
  bookMessage("");
  const bestBid = bids[0][0], bestAsk = asks[0][0], mid = (bestBid + bestAsk) / 2;
  setupBookGroups(mid);
  const lo = mid * (1 - bookRange), hi = mid * (1 + bookRange);
  const inBids = bids.filter(([p]) => p >= lo), inAsks = asks.filter(([p]) => p <= hi);
  const sumUsd = (l) => l.reduce((s, [p, q]) => s + p * q, 0);
  const bidUsd = sumUsd(inBids), askUsd = sumUsd(inAsks);
  renderBookStats({ bestBid, bestAsk, mid, bidUsd, askUsd, inBids, inAsks });
  renderBookTable(bids, asks, mid);
  drawDepth({ inBids, inAsks, mid, lo, hi });
  // Hinweis, wenn die Momentaufnahme den gewählten Bereich nicht ganz abdeckt
  const notes = [];
  const fmtPct = (v) => (v * 100).toLocaleString(LOCALE, { maximumFractionDigits: 2 }) + PCT;
  if (book.floor > lo) notes.push(`${L("Kaufseite nur bis", "Buy side only down to")} −${fmtPct(1 - book.floor / mid)}`);
  if (book.ceil < hi) notes.push(`${L("Verkaufsseite nur bis", "Sell side only up to")} +${fmtPct(book.ceil / mid - 1)}`);
  $("book-note").textContent = notes.length
    ? (EN ? `${notes.join(", ")} – Binance provides no more levels ${bookMarket === "futures" ? "for futures (1,000 per side)" : "(5,000 per side)"}; the rest of the range is missing.`
      : `${notes.join(", ")} – mehr Stufen liefert Binance ${bookMarket === "futures" ? "für Futures (1.000 je Seite)" : "nicht (5.000 je Seite)"}; der Rest des Bereichs fehlt.`)
    : "";
}

function renderBookStats({ bestBid, bestAsk, mid, bidUsd, askUsd, inBids, inAsks }) {
  const spread = bestAsk - bestBid, rangeTxt = "±" + (bookRange * 100).toLocaleString(LOCALE) + PCT;
  const imb = bidUsd + askUsd > 0 ? (bidUsd - askUsd) / (bidUsd + askUsd) : 0;
  const wall = (levels, down) => {
    const g = groupSide(levels, niceStep(mid * BOOK_WALL_STEP), down).sort((a, b) => b[2] - a[2])[0];
    return g ? { price: g[0], usd: g[2], dist: (g[0] / mid - 1) * 100 } : null;
  };
  const bw = wall(inBids, true), aw = wall(inAsks, false);
  const wallTile = (w, label) => statTile("bookWall", label, w ? usd(w.price) : "–",
    w ? `<div class="det"><b>${bookUsd(w.usd)}</b> · ${num1(w.dist)}${PCT} ${L("vom Kurs", "from price")}</div>` : "");
  const share = bidUsd + askUsd > 0 ? bidUsd / (bidUsd + askUsd) * 100 : 50;
  $("book-stats").innerHTML =
    statTile("bookSpread", "Spread", usd(spread),
      `<div class="det">${(spread / mid * 100).toLocaleString(LOCALE, { maximumSignificantDigits: 2 })}${PCT} · ${L("Mitte", "mid")} ${usd(mid)}</div>`) +
    statTile("bookImbalance", `${L("Kauf vs. Verkauf", "Buy vs. sell")} ${rangeTxt}`,
      `<span class="${imb > 0.05 ? "up" : imb < -0.05 ? "down" : ""}">${imb > 0 ? "+" : imb < 0 ? "−" : ""}${Math.abs(imb * 100).toFixed(0)}${PCT} ${imb >= 0 ? L("Kaufseite", "buy side") : L("Verkaufsseite", "sell side")}</span>`,
      `<div class="book-bar"><i class="b" style="width:${share.toFixed(1)}%"></i><i class="a"></i></div>
       <div class="det">${L("Kauf", "Buy")} <b>${bookUsd(bidUsd)}</b> · ${L("Verkauf", "Sell")} <b>${bookUsd(askUsd)}</b></div>`) +
    wallTile(bw, `${L("Größte Kaufwand", "Largest buy wall")} ${rangeTxt}`) +
    wallTile(aw, `${L("Größte Verkaufswand", "Largest sell wall")} ${rangeTxt}`);
}

function renderBookTable(bids, asks, mid) {
  const step = bookGroup, dec = stepDecimals(step);
  const gb = groupSide(bids, step, true).sort((a, b) => b[0] - a[0]).slice(0, BOOK_ROWS);
  const ga = groupSide(asks, step, false).sort((a, b) => a[0] - b[0]).slice(0, BOOK_ROWS);
  const max = Math.max(...gb.map((g) => g[2]), ...ga.map((g) => g[2]), 1);
  const price = (v) => v.toLocaleString(LOCALE, { minimumFractionDigits: dec, maximumFractionDigits: dec });
  const qty = (q) => q.toLocaleString(LOCALE, { maximumFractionDigits: q >= 100 ? 0 : q >= 1 ? 2 : 4 });
  const rows = (list, side) => {
    let cum = 0;
    return list.map((g) => {
      cum += g[2];
      return `<tr class="${side}" style="--w:${(g[2] / max * 100).toFixed(1)}%"><td>${price(g[0])}</td><td>${qty(g[1])}</td><td>${bookUsd(cum)}</td></tr>`;
    });
  };
  $("book-asks").innerHTML = rows(ga, "ask").reverse().join("");   // höchster Preis oben, bester Ask direkt über der Mitte
  $("book-bids").innerHTML = rows(gb, "bid").join("");
  $("book-mid").innerHTML = `<tr class="mid"><td colspan="3"><b>${usd(mid)}</b> <span>${L("Mitte", "Mid")} · Spread ${usd(asks[0][0] - bids[0][0])}</span></td></tr>`;
}

// Tiefenchart: kumulierte USD-Summe vom Kurs nach außen, Kauf links (grün), Verkauf rechts (rot)
function drawDepth({ inBids, inAsks, mid, lo, hi }) {
  const cv = $("book-depth"), box = cv.parentElement, dpr = window.devicePixelRatio || 1;
  const W = box.clientWidth, H = box.clientHeight;
  if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
    cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
    cv.style.width = W + "px"; cv.style.height = H + "px";
  }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  const pad = { l: 8, r: 64, t: 10, b: 24 }, pw = W - pad.l - pad.r, ph = H - pad.t - pad.b;
  const cumB = [], cumA = [];
  let s = 0; for (const [p, q] of inBids) { s += p * q; cumB.push([p, s]); }
  s = 0; for (const [p, q] of inAsks) { s += p * q; cumA.push([p, s]); }
  const maxY = Math.max(cumB.length ? cumB[cumB.length - 1][1] : 0, cumA.length ? cumA[cumA.length - 1][1] : 0, 1) * 1.08;
  const x = (p) => pad.l + (p - lo) / (hi - lo) * pw, y = (v) => pad.t + ph - v / maxY * ph;
  const up = css("--up"), down = css("--down"), muted = css("--muted"), grid = css("--track");

  ctx.font = "11px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
  ctx.fillStyle = muted; ctx.strokeStyle = grid; ctx.lineWidth = 1;
  for (let i = 1; i <= 4; i++) {   // waagrechte Linien mit USD-Beschriftung rechts
    const v = maxY / 1.08 * i / 4, yy = Math.round(y(v)) + 0.5;
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(pad.l + pw, yy); ctx.stroke();
    ctx.textAlign = "left"; ctx.fillText(bookUsd(v), pad.l + pw + 6, yy + 4);
  }
  ctx.textAlign = "center";
  for (const f of [-1, -0.5, 0, 0.5, 1]) {   // Preisachse: Rand, Hälfte, Mitte
    const p = mid * (1 + f * bookRange), xx = x(p);
    const lbl = f === 0 ? usd(mid) : (f > 0 ? "+" : "−") + (Math.abs(f) * bookRange * 100).toLocaleString(LOCALE) + PCT;
    ctx.fillText(lbl, Math.min(Math.max(xx, pad.l + 24), pad.l + pw - 24), H - 6);
  }

  const area = (pts, color, left) => {
    if (!pts.length) return;
    ctx.beginPath();
    ctx.moveTo(x(pts[0][0]), y(0));
    let prev = 0;
    for (const [p, v] of pts) { ctx.lineTo(x(p), y(prev)); ctx.lineTo(x(p), y(v)); prev = v; }
    ctx.lineTo(left ? pad.l : pad.l + pw, y(prev));
    ctx.strokeStyle = color; ctx.lineWidth = 1.6; ctx.stroke();
    ctx.lineTo(left ? pad.l : pad.l + pw, y(0));
    ctx.closePath();
    ctx.fillStyle = alpha(color, 0.18); ctx.fill();
  };
  area(cumB, up, true);
  area(cumA, down, false);
  // Bereich außerhalb der Momentaufnahme grau markieren
  ctx.fillStyle = alpha(muted, 0.12);
  if (book.floor > lo) ctx.fillRect(pad.l, pad.t, x(book.floor) - pad.l, ph);
  if (book.ceil < hi) ctx.fillRect(x(book.ceil), pad.t, pad.l + pw - x(book.ceil), ph);
  ctx.strokeStyle = muted; ctx.setLineDash([3, 3]);
  ctx.beginPath(); ctx.moveTo(Math.round(x(mid)) + 0.5, pad.t); ctx.lineTo(Math.round(x(mid)) + 0.5, pad.t + ph); ctx.stroke();
  ctx.setLineDash([]);
  bookView = { cumB, cumA, mid, lo, hi, pad, pw, x };
}

// Maus über dem Tiefenchart: Preis, Abstand zum Kurs und kumulierte Summe bis dorthin
function bookHover(e) {
  const tip = $("book-hover");
  if (!bookView) { tip.hidden = true; return; }
  const { cumB, cumA, mid, lo, hi, pad, pw } = bookView;
  const r = $("book-depth").getBoundingClientRect(), px = e.clientX - r.left;
  if (px < pad.l || px > pad.l + pw) { tip.hidden = true; return; }
  const p = lo + (px - pad.l) / pw * (hi - lo), isBid = p < mid;
  const list = isBid ? cumB : cumA;
  let v = 0;
  for (const [lp, c] of list) { if (isBid ? lp >= p : lp <= p) v = c; else break; }
  tip.innerHTML = `<div><b>${usd(p)}</b> · ${num1((p / mid - 1) * 100)}${PCT}</div>
    <div>${isBid ? L("Kaufaufträge bis hier", "Buy orders up to here") : L("Verkaufsaufträge bis hier", "Sell orders up to here")}: <b>${bookUsd(v)}</b></div>`;
  tip.hidden = false;
  const w = tip.offsetWidth;
  tip.style.left = Math.min(Math.max(4, px - w / 2), r.width - w - 4) + "px";
}

function updateBookMarketButtons() {
  $("book-market").querySelectorAll("button").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.market === bookMarket));
    b.disabled = bookAvail[b.dataset.market] === false;
    if (b.disabled) b.title = L(`Kein ${symbol}/USDT-Paar auf ${BOOK_MARKETS[b.dataset.market].name}`, `No ${symbol}/USDT pair on ${BOOK_MARKETS[b.dataset.market].name}`);
  });
}

function startBook() {
  updateBookMarketButtons();
  $("book-range").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(+b.dataset.range === bookRange)));
  $("book-market").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-market]");
    if (!b || b.disabled || b.dataset.market === bookMarket) return;
    bookMarket = b.dataset.market;
    try { localStorage.setItem(BOOK_MARKET_KEY, bookMarket); } catch (err) { /* ignorieren */ }
    updateBookMarketButtons();
    book = null; bookView = null; $("book-group").dataset.steps = "";
    bookMessage(L("Lade Orderbuch …", "Loading order book …"));
    connectBook();
  });
  $("book-range").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-range]");
    if (!b) return;
    bookRange = +b.dataset.range;
    try { localStorage.setItem(BOOK_RANGE_KEY, String(bookRange)); } catch (err) { /* ignorieren */ }
    $("book-range").querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    if (book) drawBook();
  });
  $("book-group").addEventListener("change", () => { bookGroup = +$("book-group").value; if (book) drawBook(); });
  $("book-depth").addEventListener("mousemove", bookHover);
  $("book-depth").addEventListener("mouseleave", () => { $("book-hover").hidden = true; });
  new ResizeObserver(() => { if (book) drawBook(); }).observe($("book-depth").parentElement);
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (book) drawBook(); });
  // Im Hintergrund-Tab trennen, beim Zurückkehren neu synchronisieren
  document.addEventListener("visibilitychange", () => {
    if (bookOff) return;
    if (document.hidden) closeBook(); else { book = null; connectBook(); }
  });
  connectBook();
}

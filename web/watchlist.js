// Watchlist: eigene Coins, nur im Browser gespeichert (localStorage "watchlist"), gemeinsam für alle Seiten.
// BTC, ETH und SOL stehen immer vorne (WATCH_FIXED) und lassen sich nicht entfernen; gespeichert werden nur die übrigen.
// Einträge { id, symbol, name }; die CoinGecko-ID ist Pflicht (Symbole sind nicht eindeutig) und dient als Schlüssel.
// Marktdaten liefert /api/watchlist?ids=… (eine CoinGecko-Abfrage für alle Coins).
// Stern-Buttons: watchStar(coin) einsetzen; ein Klick schaltet um, ohne den Klick an Zeile oder Kachel weiterzugeben.
// Jede Änderung (auch aus einem anderen Tab) löst das Ereignis "watchlist" auf document aus.

const WATCH_KEY = "watchlist";
const WATCH_MAX = 100;   // wie WATCHLIST_MAX in sentiment.py
const WATCH_ID_RE = /^[a-z0-9-]{1,100}$/;
const WATCH_SYMBOL_RE = /^[A-Z0-9]{1,15}$/;
const WATCH_FIXED = [
  { id: "bitcoin", symbol: "BTC", name: "Bitcoin" },
  { id: "ethereum", symbol: "ETH", name: "Ethereum" },
  { id: "solana", symbol: "SOL", name: "Solana" },
];
const WATCH_ADD = L("Zur Watchlist hinzufügen", "Add to watchlist");
const WATCH_REMOVE = L("Aus der Watchlist entfernen", "Remove from watchlist");
const isFixed = (id) => WATCH_FIXED.some((c) => c.id === id);

// Nur die selbst gemerkten Coins (ohne die festen)
function watchOwn() {
  try {
    const list = JSON.parse(localStorage.getItem(WATCH_KEY));
    return Array.isArray(list)
      ? list.filter((c) => c && WATCH_ID_RE.test(c.id) && WATCH_SYMBOL_RE.test(c.symbol) && !isFixed(c.id)) : [];
  } catch (e) {
    return [];
  }
}

// Ganze Watchlist: feste Coins zuerst, dann in der Reihenfolge des Hinzufügens
const watchList = () => [...WATCH_FIXED, ...watchOwn()];

function watchSave(list) {
  try { localStorage.setItem(WATCH_KEY, JSON.stringify(list.filter((c) => !isFixed(c.id)))); } catch (e) { /* Speicher blockiert: nicht gemerkt */ }
  document.dispatchEvent(new CustomEvent("watchlist"));
}

const isWatched = (id) => !!id && watchList().some((c) => c.id === id);

// Schaltet um und liefert den neuen Zustand (true = in der Watchlist)
function toggleWatch(coin) {
  if (isFixed(coin.id)) return true;
  const list = watchOwn();
  const i = list.findIndex((c) => c.id === coin.id);
  if (i >= 0) list.splice(i, 1);
  else if (list.length + WATCH_FIXED.length < WATCH_MAX) list.push({ id: coin.id, symbol: coin.symbol, name: coin.name || coin.symbol });
  else { alert(L(`Die Watchlist ist voll (höchstens ${WATCH_MAX} Coins).`, `The watchlist is full (at most ${WATCH_MAX} coins).`)); return false; }
  watchSave(list);
  return i < 0;
}

// Stern-Button; ohne gültige ID oder Symbol leer (dann kann der Coin nicht gemerkt werden)
function watchStar(coin, cls = "") {
  if (!coin || !WATCH_ID_RE.test(coin.id || "") || !WATCH_SYMBOL_RE.test(coin.symbol || "")) return "";
  if (isFixed(coin.id)) {
    return `<button type="button" class="star on${cls ? " " + cls : ""}" disabled aria-pressed="true"`
      + ` title="${esc(coin.symbol)} ${L("gehört fest zur Watchlist", "is always in the watchlist")}" aria-label="Watchlist: ${esc(coin.symbol)} (${L("fest", "fixed")})">★</button>`;
  }
  const on = isWatched(coin.id);
  return `<button type="button" class="star${on ? " on" : ""}${cls ? " " + cls : ""}" aria-pressed="${on}"`
    + ` data-watch-id="${esc(coin.id)}" data-watch-symbol="${esc(coin.symbol)}" data-watch-name="${esc(coin.name || coin.symbol)}"`
    + ` title="${on ? WATCH_REMOVE : WATCH_ADD}" aria-label="Watchlist: ${esc(coin.symbol)}">${on ? "★" : "☆"}</button>`;
}

// Alle Sterne auf der Seite an den gespeicherten Stand anpassen
function syncStars() {
  const ids = new Set(watchList().map((c) => c.id));
  document.querySelectorAll("button.star[data-watch-id]:not(:disabled)").forEach((b) => {
    const on = ids.has(b.dataset.watchId);
    b.classList.toggle("on", on);
    b.setAttribute("aria-pressed", on);
    b.textContent = on ? "★" : "☆";
    b.title = on ? WATCH_REMOVE : WATCH_ADD;
  });
}

// Capture-Phase: der Klick auf den Stern öffnet nicht zusätzlich Zeile, Kachel oder Chip darunter
document.addEventListener("click", (e) => {
  const b = e.target.closest("button.star[data-watch-id]");
  if (!b) return;
  e.preventDefault();
  e.stopPropagation();
  toggleWatch({ id: b.dataset.watchId, symbol: b.dataset.watchSymbol, name: b.dataset.watchName });
}, true);
document.addEventListener("watchlist", syncStars);
window.addEventListener("storage", (e) => {
  if (e.key === WATCH_KEY || e.key === null) document.dispatchEvent(new CustomEvent("watchlist"));
});

// Detailseite: Reiter (Übersicht, Chart & Technik, …) und Breadcrumbs Märkte → Coin → Reiter.
// Jeder Reiter ist ein <div class="tab-panel" data-tab="…"> in coin.html. Inaktive Reiter sind nur unsichtbar
// (CSS in coin.css), nicht display:none: so behalten Charts und Canvas ihre Breite, fitContent und Zeitbereiche
// stimmen beim Umschalten, und alle Abschnitte laden wie bisher gleich beim Öffnen der Seite.
// Gewählter Reiter steht im Hash (#derivs), damit Neuladen und geteilte Links ihn behalten;
// ein Hash mit der ID eines Abschnitts (#book) öffnet dessen Reiter und springt hin.

const TABS = [
  { key: "overview", label: L("Übersicht", "Overview") },
  { key: "chart", label: L("Chart & Technik", "Chart & technicals") },
  { key: "derivs", label: L("Derivate", "Derivatives") },
  { key: "onchain", label: "On-Chain" },
  { key: "liq", label: L("Liquidationen", "Liquidations") },
  { key: "fundamentals", label: L("Fundamentaldaten", "Fundamentals") },
  { key: "score", label: L("Score & Historie", "Score & history") },
];
let activeTab = "overview";
let crumbCoinName = "";

function setCoinCrumbs(name) {
  if (name) crumbCoinName = name;
  const items = [{ label: L("Märkte", "Markets"), href: "markets.html" }];
  if (validSymbol) {
    items.push({ label: crumbCoinName || symbol, href: "coin.html" + location.search });
    items.push({ label: TABS.find((t) => t.key === activeTab).label });
  }
  setBreadcrumb(items);
}

function showTab(key, { focus = false, updateHash = true } = {}) {
  if (!TABS.some((t) => t.key === key)) key = "overview";
  activeTab = key;
  for (const btn of document.querySelectorAll("#tabs [role=tab]")) {
    const on = btn.dataset.tab === key;
    btn.setAttribute("aria-selected", String(on));
    btn.tabIndex = on ? 0 : -1;
    if (on && focus) btn.focus();
  }
  for (const panel of document.querySelectorAll(".tab-panel")) panel.classList.toggle("on", panel.dataset.tab === key);
  if (updateHash) history.replaceState(null, "", key === "overview" ? location.pathname + location.search : "#" + key);
  setCoinCrumbs();
}

// Reiter ohne Inhalt (alle Abschnitte hidden, z. B. keine Futures oder kein On-Chain) gedimmt, im Reiter ein Hinweis
function updateEmptyTabs() {
  for (const panel of document.querySelectorAll(".tab-panel")) {
    const empty = [...panel.querySelectorAll(":scope > section")].every((s) => s.hidden);
    const tab = TABS.find((t) => t.key === panel.dataset.tab);
    const note = panel.querySelector(".tab-empty");
    if (note.hidden === !empty) continue;
    note.hidden = !empty;
    note.textContent = L(`Für ${symbol} gibt es hier keine Daten (${tab.label}).`, `No data for ${symbol} here (${tab.label}).`);
    const btn = document.querySelector(`#tabs [data-tab="${tab.key}"]`);
    btn.classList.toggle("empty", empty);
    btn.title = empty ? L("Keine Daten für diesen Coin", "No data for this coin") : "";
  }
}

function initTabs() {
  const bar = $("tabs");
  bar.innerHTML = TABS.map((t) =>
    `<button type="button" role="tab" id="tabbtn-${t.key}" data-tab="${t.key}" aria-controls="tab-${t.key}">${esc(t.label)}</button>`).join("");
  for (const t of TABS) $("tab-" + t.key).setAttribute("aria-labelledby", "tabbtn-" + t.key);
  bar.addEventListener("click", (e) => {
    const btn = e.target.closest("[role=tab]");
    if (btn) showTab(btn.dataset.tab);
  });
  // Pfeiltasten, Pos1/Ende wie bei Reitern üblich
  bar.addEventListener("keydown", (e) => {
    const i = TABS.findIndex((t) => t.key === activeTab);
    const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1 }[e.key];
    if (next == null) return;
    e.preventDefault();
    showTab(TABS[(next + TABS.length) % TABS.length].key, { focus: true });
  });

  const fromHash = () => {
    const h = decodeURIComponent(location.hash.slice(1));
    const section = h && document.getElementById(h);
    const panel = section && section.closest(".tab-panel");
    if (panel && section.tagName === "SECTION") {
      showTab(panel.dataset.tab, { updateHash: false });
      requestAnimationFrame(() => section.scrollIntoView());
    } else {
      showTab(h || "overview", { updateHash: false });
    }
  };
  addEventListener("hashchange", fromHash);
  fromHash();

  new MutationObserver(updateEmptyTabs).observe($("tabs").parentElement,
    { subtree: true, attributes: true, attributeFilter: ["hidden"] });
  updateEmptyTabs();
}

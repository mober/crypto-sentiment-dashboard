// Gemeinsame Navigationsleiste aller Seiten (direkt nach i18n.js im <head> laden).
// Wird beim Laden oben in <main> gesetzt: Name links, Seiten und Sprachumschalter rechts.
// Auf schmalen Bildschirmen (≤ 640 px) klappen die Seiten in ein Menü hinter „☰“; der Umschalter bleibt sichtbar.
// Seitenspezifische Knöpfe (z. B. „Aktualisieren“) bleiben im <header> der jeweiligen Seite.
// Detailseiten setzen darunter Breadcrumbs mit setBreadcrumb() (z. B. Märkte → Bitcoin → Derivate).

const NAV_PAGES = [
  { href: "./", files: ["", "index.html"], label: () => L("Dashboard", "Dashboard") },
  // Detailseite und Profil eines Coins (section) gehören zu dem Bereich, aus dem man kam (navOrigin), sonst zu „Märkte“
  { href: "markets.html", files: ["markets.html"], section: ["coin.html", "about.html"], label: () => L("Märkte", "Markets") },
  { href: "sectors.html", files: ["sectors.html"], label: () => L("Sparten", "Sectors") },
  { href: "liquidations.html", files: ["liquidations.html"], label: () => L("Liquidationen", "Liquidations") },
  { href: "alerts.html", files: ["alerts.html"], label: () => L("Alarme", "Alerts") },
  { href: "methodology.html", files: ["methodology.html"], label: () => L("Methodik", "Methodology") },
];

// Herkunft für Detailseiten: Von welchem Hauptbereich kam man (Dashboard, Märkte, …)? Jede Hauptseite merkt sich
// in sessionStorage; Coin-Detailseite und Profil markieren dann diesen Bereich und beginnen die Breadcrumbs dort.
const NAV_FILE = location.pathname.split("/").pop();
(() => {
  const own = NAV_PAGES.find((p) => p.files.includes(NAV_FILE));
  if (own) { try { sessionStorage.setItem("navFrom", own.href); } catch (e) { /* ignorieren */ } }
})();
function navOrigin() {
  let from = null;
  try { from = sessionStorage.getItem("navFrom"); } catch (e) { /* ignorieren */ }
  return NAV_PAGES.find((p) => p.href === from) || NAV_PAGES.find((p) => p.href === "markets.html");
}

function buildNav() {
  if (document.querySelector(".topbar")) return;
  const file = NAV_FILE;
  const origin = NAV_PAGES.some((p) => (p.section || []).includes(file)) ? navOrigin() : null;
  const other = EN ? "de" : "en";
  const links = NAV_PAGES.map((p) => {
    // aria-current: "page" für genau diese Seite, "true" für eine Unterseite des Bereichs
    const cur = p.files.includes(file) ? "page" : origin === p ? "true" : "";
    return `<a href="${p.href}"${cur ? ` class="on" aria-current="${cur}"` : ""}>${p.label()}</a>`;
  }).join("");
  const bar = document.createElement("nav");
  bar.className = "topbar";
  bar.innerHTML =
    `<a class="brand" href="./">Crypto Sentiment</a>`
    + `<div class="topnav" id="topnav">${links}</div>`
    + `<button type="button" class="lang-switch" title="${EN ? "Auf Deutsch umschalten" : "Switch to English"}"`
    + ` aria-label="${EN ? "Deutsch" : "English"}">${other.toUpperCase()}</button>`
    + `<button type="button" class="menu-btn" aria-controls="topnav" aria-expanded="false"`
    + ` aria-label="${L("Menü", "Menu")}">☰</button>`;
  (document.querySelector("main") || document.body).prepend(bar);

  bar.querySelector(".lang-switch").addEventListener("click", () => setLang(other));
  const btn = bar.querySelector(".menu-btn");
  const setOpen = (open) => {
    bar.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", String(open));
    btn.textContent = open ? "✕" : "☰";
  };
  btn.addEventListener("click", () => setOpen(!bar.classList.contains("open")));
  // Menü schließen bei Klick daneben oder Escape
  document.addEventListener("click", (e) => { if (!bar.contains(e.target)) setOpen(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });
}

// --- Breadcrumbs (Detailseiten) ---------------------------------------------
// setBreadcrumb([{ label, href }, …]): letzter Eintrag ist die aktuelle Stelle (ohne Link); [] entfernt die Leiste.
// Genutzt auf Coin-Detailseite, Profil und in der Marktübersicht, wenn sie eine Sparte zeigt (Sparten → Layer 1).
// Kann vor oder nach buildNav laufen; erneuter Aufruf ersetzt die Leiste.
let crumbItems = [];

function setBreadcrumb(items) {
  crumbItems = items || [];
  if (!crumbItems.length) {   // leere Liste: Leiste entfernen (z. B. Marktübersicht ohne Sparte)
    document.querySelector("main .crumbs")?.remove();
    return;
  }
  renderCrumbs();
}

function renderCrumbs() {
  const main = document.querySelector("main");
  if (!main) return;
  let bar = main.querySelector(".crumbs");
  if (!bar) {
    bar = document.createElement("nav");
    bar.className = "crumbs";
    bar.setAttribute("aria-label", L("Brotkrümelnavigation", "Breadcrumb"));
    const top = main.querySelector(".topbar");
    top ? top.after(bar) : main.prepend(bar);
  }
  const e = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  bar.innerHTML = "<ol>" + crumbItems.map((x, i) => i === crumbItems.length - 1
    ? `<li><span aria-current="page">${e(x.label)}</span></li>`
    : `<li><a href="${e(x.href)}">${e(x.label)}</a></li>`).join("") + "</ol>";
}

document.addEventListener("DOMContentLoaded", buildNav);

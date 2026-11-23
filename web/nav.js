// Gemeinsame Navigationsleiste aller Seiten (direkt nach i18n.js im <head> laden).
// Wird beim Laden oben in <main> gesetzt: Name links, Seiten und Sprachumschalter rechts.
// Auf schmalen Bildschirmen (≤ 640 px) klappen die Seiten in ein Menü hinter „☰“; der Umschalter bleibt sichtbar.
// Seitenspezifische Knöpfe (z. B. „Aktualisieren“) bleiben im <header> der jeweiligen Seite.

const NAV_PAGES = [
  { href: "./", files: ["", "index.html"], label: () => L("Dashboard", "Dashboard") },
  { href: "markets.html", files: ["markets.html"], label: () => L("Märkte", "Markets") },
  { href: "sectors.html", files: ["sectors.html"], label: () => L("Sparten", "Sectors") },
  { href: "liquidations.html", files: ["liquidations.html"], label: () => L("Liquidationen", "Liquidations") },
  { href: "alerts.html", files: ["alerts.html"], label: () => L("Alarme", "Alerts") },
];

function buildNav() {
  if (document.querySelector(".topbar")) return;
  const file = location.pathname.split("/").pop();
  const other = EN ? "de" : "en";
  const links = NAV_PAGES.map((p) => {
    const on = p.files.includes(file);
    return `<a href="${p.href}"${on ? ' class="on" aria-current="page"' : ""}>${p.label()}</a>`;
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

document.addEventListener("DOMContentLoaded", buildNav);

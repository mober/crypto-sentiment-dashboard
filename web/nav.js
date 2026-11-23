// Gemeinsame Navigationsleiste aller Seiten (direkt nach i18n.js im <head> laden).
// Wird beim Laden oben in <main> gesetzt: Name links, Seiten und Sprachumschalter rechts.
// Auf schmalen Bildschirmen (≤ 640 px) klappen die Seiten in ein Menü hinter „☰“; der Umschalter bleibt sichtbar.
// Seitenspezifische Knöpfe (z. B. „Aktualisieren“) bleiben im <header> der jeweiligen Seite.
// Detailseiten setzen darunter Breadcrumbs mit setBreadcrumb() (z. B. Märkte → Bitcoin → Derivate).

const NAV_PAGES = [
  { href: "./", files: ["", "index.html"], label: () => L("Dashboard", "Dashboard") },
  // Detailseite und Profil eines Coins gehören zum Bereich „Märkte“ (section)
  { href: "markets.html", files: ["markets.html"], section: ["coin.html", "about.html"], label: () => L("Märkte", "Markets") },
  { href: "sectors.html", files: ["sectors.html"], label: () => L("Sparten", "Sectors") },
  { href: "liquidations.html", files: ["liquidations.html"], label: () => L("Liquidationen", "Liquidations") },
  { href: "alerts.html", files: ["alerts.html"], label: () => L("Alarme", "Alerts") },
];

function buildNav() {
  if (document.querySelector(".topbar")) return;
  const file = location.pathname.split("/").pop();
  const other = EN ? "de" : "en";
  const links = NAV_PAGES.map((p) => {
    // aria-current: "page" für genau diese Seite, "true" für eine Unterseite des Bereichs
    const cur = p.files.includes(file) ? "page" : (p.section || []).includes(file) ? "true" : "";
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
// setBreadcrumb([{ label, href }, …], sections): letzter Eintrag ist die aktuelle Seite.
// sections (optional, { id: Name }): Abschnitt der Seite, in dem man gerade ist, kommt als letzte Stufe dazu
// und folgt beim Scrollen. Die Leiste klebt deshalb oben (sticky). Kann vor oder nach buildNav laufen.
let crumbItems = [], crumbSections = null, crumbSection = null;

function setBreadcrumb(items, sections) {
  crumbItems = items;
  if (sections && !crumbSections) {
    crumbSections = sections;
    let queued = false;
    window.addEventListener("scroll", () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => { queued = false; updateCrumbSection(); });
    }, { passive: true });
  }
  if (sections) crumbSections = sections;
  if (crumbSections) crumbSection = currentSection();
  renderCrumbs();
}

// Letzter sichtbarer Abschnitt, dessen Oberkante schon unter der Leiste durch ist
function currentSection() {
  const bar = document.querySelector(".crumbs");
  const line = (bar ? bar.getBoundingClientRect().bottom : 0) + 60;
  const atEnd = innerHeight + scrollY >= document.documentElement.scrollHeight - 4;
  let cur = null;
  for (const el of document.querySelectorAll("main > section[id]")) {
    if (el.hidden || !el.offsetParent) continue;
    if (atEnd || el.getBoundingClientRect().top <= line) cur = el.id;
  }
  return cur && crumbSections[cur] ? cur : null;
}

function updateCrumbSection() {
  const cur = currentSection();
  if (cur !== crumbSection) { crumbSection = cur; renderCrumbs(); }
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
  const items = crumbItems.slice();
  // Mit Abschnitt verweist die Seite selbst nach oben, der Abschnitt ist die aktuelle Stelle
  if (crumbSection) {
    items[items.length - 1] = { ...items[items.length - 1], href: "#" };
    items.push({ label: crumbSections[crumbSection], href: "#" + crumbSection, current: "location" });
  }
  const e = (t) => String(t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  bar.innerHTML = "<ol>" + items.map((x, i) => {
    const last = i === items.length - 1;
    if (last) return `<li><a href="${e(x.href || "#")}" aria-current="${x.current || "page"}">${e(x.label)}</a></li>`;
    return `<li><a href="${e(x.href)}">${e(x.label)}</a></li>`;
  }).join("") + "</ol>";
  bar.querySelector('a[href="#"]')?.addEventListener("click", (ev) => { ev.preventDefault(); scrollTo({ top: 0, behavior: "smooth" }); });
}

document.addEventListener("DOMContentLoaded", buildNav);

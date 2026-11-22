// Zweisprachige Oberfläche (deutsch/englisch). Als erstes Skript jeder Seite laden (vor format.js).
// - LANG: ?lang=de|en, sonst gespeicherte Wahl (localStorage "lang"), sonst Browsersprache (deutsch → de, sonst en)
// - Cookie "lang" für den Server: Texte aus /api/… (Einschätzung, Lage, Profil, News) kommen in derselben Sprache
// - L(deutsch, englisch) für Texte im Code; LOCALE und DEC für Zahlen und Datumsangaben
// - Statisches HTML (Text, title, placeholder, aria-label, <title>) wird beim Laden über STATIC_EN übersetzt;
//   Schlüssel ist der deutsche Text mit zusammengefassten Leerzeichen. Fehlt ein Eintrag, bleibt es deutsch.
// - Umschalter DE/EN sitzt in der gemeinsamen Navigationsleiste (nav.js).

const LANGS = ["de", "en"];
const LANG = (() => {
  const q = new URLSearchParams(location.search).get("lang");
  if (LANGS.includes(q)) {
    try { localStorage.setItem("lang", q); } catch (e) { /* ignorieren */ }
    return q;
  }
  try { const s = localStorage.getItem("lang"); if (LANGS.includes(s)) return s; } catch (e) { /* ignorieren */ }
  return (navigator.language || "").toLowerCase().startsWith("de") ? "de" : "en";
})();
const EN = LANG === "en";
const LOCALE = EN ? "en-US" : "de-DE";
const DEC = EN ? "." : ",";     // Dezimaltrenner für Zahlen, die per toFixed entstehen
const L = (de, en) => (EN ? en : de);

document.documentElement.lang = LANG;
document.cookie = `lang=${LANG}; path=/; max-age=31536000; SameSite=Lax`;

function setLang(lang) {
  try { localStorage.setItem("lang", lang); } catch (e) { /* ignorieren */ }
  document.cookie = `lang=${lang}; path=/; max-age=31536000; SameSite=Lax`;
  const url = new URL(location.href);
  url.searchParams.delete("lang");
  location.replace(url.toString());
}

// Deutsch → Englisch für statische Texte der Seiten (Überschriften, Knöpfe, Fußzeilen …)
const STATIC_EN = {};
function addStatic(map) { Object.assign(STATIC_EN, map); }

function translateStatic(root = document.body) {
  if (!EN || !root) return;
  const norm = (s) => s.replace(/\s+/g, " ").trim();
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement && /^(SCRIPT|STYLE)$/.test(n.parentElement.tagName) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((n) => {
    const key = norm(n.nodeValue);
    if (key && STATIC_EN[key] != null) {
      const lead = n.nodeValue.match(/^\s*/)[0], trail = n.nodeValue.match(/\s*$/)[0];
      n.nodeValue = lead + STATIC_EN[key] + trail;
    }
  });
  root.querySelectorAll("[title], [placeholder], [aria-label], [alt]").forEach((el) => {
    ["title", "placeholder", "aria-label", "alt"].forEach((a) => {
      const v = el.getAttribute(a);
      if (v && STATIC_EN[norm(v)] != null) el.setAttribute(a, STATIC_EN[norm(v)]);
    });
  });
  const title = norm(document.title);
  if (STATIC_EN[title] != null) document.title = STATIC_EN[title];
}

document.addEventListener("DOMContentLoaded", () => {
  translateStatic();
});

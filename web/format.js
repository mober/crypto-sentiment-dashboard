// Gemeinsame Hilfsfunktionen für alle Seiten: Element-Zugriff, Escaping und Zahlenformate.
// Hier ändern statt in den einzelnen Seiten, damit die Darstellung überall gleich bleibt.
// Braucht die Klassen .up, .down und .muted aus base.css und LOCALE, DEC, L aus i18n.js.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const dash = '<span class="muted">–</span>';   // Platzhalter für fehlende Werte

// Ganze Zahl mit Vorzeichen (echtes Minuszeichen), z. B. Score-Punkte
const signed = (v) => (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v);
// Farbklasse für Scores; Schwellen wie in sentiment.py: VERDICT_WARM (Gesamtscore, 15) und SIGNAL_STRONG (Einzelsignal, 50)
const tone = (score, limit = 15) => (score >= limit ? "tone-hot" : score <= -limit ? "tone-cold" : "tone-neutral");

// Anteil (0,05 = 5 %) als Prozent mit Vorzeichen. Das Vorzeichen richtet sich nach dem
// gerundeten Wert, sonst wird z. B. −0,004 % als „−0,0 %“ angezeigt.
const PCT = L(" %", "%");   // deutsch mit Leerzeichen vor dem Prozentzeichen
const pct = (v, d = 1) => {
  const r = Number((v * 100).toFixed(d));
  return (r > 0 ? "+" : r < 0 ? "−" : "") + Math.abs(r).toFixed(d).replace(".", DEC) + PCT;
};
// Prozentänderung farbig (grün/rot), fehlende Werte als Strich
const change = (v, d = 1) => {
  if (v == null) return dash;
  const r = Number((v * 100).toFixed(d));
  return `<span class="${r > 0 ? "up" : r < 0 ? "down" : "muted"}">${pct(v, d)}</span>`;
};

// Kurse und kleinere Beträge: unter 1 $ nach signifikanten Stellen (sonst wird 0,0033 zu „$0“),
// unter 100 $ mit Cent, darüber ganzzahlig, ab 1 Mio. gekürzt
const usd = (v) => {
  if (v >= 1e9) return "$" + (v / 1e9).toLocaleString(LOCALE, { maximumFractionDigits: 1 }) + L(" Mrd.", "B");
  if (v >= 1e6) return "$" + (v / 1e6).toLocaleString(LOCALE, { maximumFractionDigits: 1 }) + L(" Mio.", "M");
  if (v !== 0 && Math.abs(v) < 1) return "$" + v.toLocaleString(LOCALE, { maximumSignificantDigits: 4 });
  return "$" + v.toLocaleString(LOCALE, v < 100 ? { minimumFractionDigits: 2, maximumFractionDigits: 2 } : { maximumFractionDigits: 0 });
};
// Große Beträge (Marktkap., Volumen, TVL) auf 6 signifikante Stellen mit Einheit, z. B. „$1,61238 Bio.“
const usd6 = (v) => {
  if (v == null || v === 0) return dash;
  const [div, unit] = v >= 1e12 ? [1e12, L(" Bio.", "T")] : v >= 1e9 ? [1e9, L(" Mrd.", "B")] : v >= 1e6 ? [1e6, L(" Mio.", "M")] : [1, ""];
  return "$" + (v / div).toLocaleString(LOCALE, { minimumSignificantDigits: 6, maximumSignificantDigits: 6 }) + unit;
};

// Fear & Greed (0–100): Zonen wie auf alternative.me / CNN; Farben von Rot (Angst) über Grau zu Grün (Gier)
const FNG_ZONES = [
  { to: 25, name: L("Extreme Angst", "Extreme fear"), color: "var(--down)" },
  { to: 45, name: L("Angst", "Fear"), color: "color-mix(in srgb, var(--down) 55%, var(--neutral))" },
  { to: 55, name: "Neutral", color: "var(--neutral)" },
  { to: 75, name: L("Gier", "Greed"), color: "color-mix(in srgb, var(--up) 55%, var(--neutral))" },
  { to: 100, name: L("Extreme Gier", "Extreme greed"), color: "var(--up)" },
];
const fngZone = (v) => FNG_ZONES.find((z) => v <= z.to) || FNG_ZONES[FNG_ZONES.length - 1];

// Kurzfassung oben auf einer Seite („Auf einen Blick“): Überschrift, ein bis zwei Sätze und eine Einordnung.
// tone: buy | hold | sell | wait | neutral (Farbe des Randes wie die Empfehlung im Lagebild der Detailseite).
// el ist ein leerer <section class="card brief" hidden>; ohne Daten bleibt er verborgen.
function renderBrief(el, b) {
  if (!el) return;
  if (!b) { el.hidden = true; return; }
  el.className = "card brief t-" + (b.tone || "neutral");
  el.innerHTML = `<div class="brief-k">${L("Auf einen Blick", "At a glance")}</div>`
    + `<div class="brief-title">${b.title}</div>`
    + (b.text ? `<div class="brief-text">${b.text}</div>` : "")
    + (b.action ? `<div class="brief-act"><b>${L("Einordnung:", "Takeaway:")}</b> ${b.action}</div>` : "");
  el.hidden = false;
}
